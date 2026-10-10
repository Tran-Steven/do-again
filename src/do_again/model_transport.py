"""Opt-in, non-browser model transport using the signed-in Codex CLI.

This adapter only obtains structured model output. It cannot publish GitHub
requests, grant execution authority, or replace the protected broker.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

_MAX_PROMPT_BYTES = 32 * 1024
_MAX_SCHEMA_BYTES = 8 * 1024
_MAX_ANSWER_BYTES = 64 * 1024
_SAFE_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")


class CodexTransportBlocked(RuntimeError):
    """A prerequisite is missing or the requested model effect is disallowed."""


class CodexTransportUncertain(CodexTransportBlocked):
    """An attempted model call has no trustworthy final result; never auto-retry."""


def discover_codex() -> Path | None:
    isolated_root = (Path.home() / ".do_again" / "codex-tools").resolve()
    isolated = isolated_root / "node_modules" / ".bin" / "codex"
    if isolated.exists():
        resolved = isolated.resolve()
        if resolved.is_file() and resolved.is_relative_to(isolated_root):
            return isolated
    candidate = shutil.which("codex")
    if candidate is None:
        return None
    resolved = Path(candidate).expanduser().resolve()
    if not resolved.is_file():
        return None
    return Path(candidate)


def _isolated_environment(home: Path) -> dict[str, str]:
    if not home.is_absolute() or not home.is_dir():
        raise CodexTransportBlocked("operator home is unavailable")
    return {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "en_US.UTF-8",
        "CODEX_HOME": str(home / ".codex"),
    }


def login_ready(binary: Path | None = None, *, home: Path | None = None) -> bool:
    selected = binary if binary is not None else discover_codex()
    if selected is None:
        return False
    operator_home = (home or Path.home()).expanduser().resolve()
    try:
        result = subprocess.run(
            [str(selected), "login", "status"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_isolated_environment(operator_home),
            timeout=12,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    summary = (result.stdout + " " + result.stderr).lower()
    return result.returncode == 0 and (
        "logged in" in summary or "authenticated" in summary
    ) and not ("not logged in" in summary or "not authenticated" in summary)


def _response_schema_valid(schema: Any) -> bool:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return False
    properties = schema.get("properties")
    required = schema.get("required")
    if (not isinstance(properties, dict) or not properties or len(properties) > 12
            or not isinstance(required, list) or set(required) != set(properties)
            or schema.get("additionalProperties") is not False):
        return False
    for key, value in properties.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key)
                or not isinstance(value, dict) or value.get("type") != "string"
                or set(value) != {"type", "maxLength"}
                or type(value["maxLength"]) is not int
                or not 1 <= value["maxLength"] <= 16000):
            return False
    return True


def _validate_response(answer: Any, schema: dict[str, Any]) -> dict[str, str]:
    if not isinstance(answer, dict) or set(answer) != set(schema["properties"]):
        raise CodexTransportUncertain("Codex output shape did not match the fixed schema")
    for name, rules in schema["properties"].items():
        value = answer[name]
        if not isinstance(value, str) or len(value) > rules["maxLength"]:
            raise CodexTransportUncertain("Codex output field failed schema validation")
    return answer


def _safe_codex_failure_category(stdout: str, stderr: str) -> str:
    """Only expose stable error categories, never CLI output or auth secrets."""
    message = (str(stdout) + " " + str(stderr)).lower()[:10000]
    if any(token in message for token in ("usage:", "unexpected argument", "unrecognized option", "unknown option")):
        return "argument_validation"
    if any(token in message for token in ("not logged in", "auth required", "authentication expired", "unauthorized", "401")):
        return "authentication"
    if any(token in message for token in ("rate limit", "usage limit", "quota exceeded", "429")):
        return "quota_or_rate_limit"
    if any(token in message for token in ("connection refused", "dns", "network unreachable", "tls", "timed out")):
        return "connectivity"
    if any(token in message for token in ("not supported", "unknown model", "model not found")):
        return "model_unavailable"
    if "sandbox" in message or "permission denied" in message:
        return "sandbox_or_filesystem"
    return "unclassified"


def generate_structured(
    prompt: str,
    schema: dict[str, Any],
    *,
    allow_model_call: bool = False,
    binary: Path | None = None,
    home: Path | None = None,
    model: str | None = None,
    timeout_seconds: int = 120,
) -> dict[str, str]:
    """One read-only Codex invocation; never retries an ambiguous invocation."""
    if not allow_model_call:
        raise CodexTransportBlocked("model usage requires explicit call authorization")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
        raise CodexTransportBlocked("model prompt is empty or exceeds the bounded input budget")
    if not _response_schema_valid(schema):
        raise CodexTransportBlocked("model output schema is not the approved flat string-object shape")
    # Structured Outputs accepts only a subset of JSON Schema keywords.
    # Keep maxLength as a strict local policy, but omit it from the provider
    # schema so model variations cannot reject the request up front.
    provider_schema = {
        "type": "object",
        "properties": {key: {"type": "string"} for key in schema["properties"]},
        "required": list(schema["required"]),
        "additionalProperties": False,
    }
    serialized_schema = json.dumps(provider_schema, sort_keys=True, separators=(",", ":"))
    if len(serialized_schema.encode("utf-8")) > _MAX_SCHEMA_BYTES:
        raise CodexTransportBlocked("model output schema exceeds its size limit")
    if model is not None and (not isinstance(model, str) or not _SAFE_MODEL.fullmatch(model)):
        raise CodexTransportBlocked("invalid fixed model identifier")
    if type(timeout_seconds) is not int or not 15 <= timeout_seconds <= 300:
        raise CodexTransportBlocked("invalid model timeout")
    selected = binary if binary is not None else discover_codex()
    if selected is None:
        raise CodexTransportBlocked("Codex CLI is not installed")
    operator_home = (home or Path.home()).expanduser().resolve()
    if not login_ready(selected, home=operator_home):
        raise CodexTransportBlocked("Codex CLI requires an authorized ChatGPT login")
    from .model_quota import CodexQuotaUnavailable, require_model_capacity
    try:
        require_model_capacity(selected, operator_home)
    except CodexQuotaUnavailable as exc:
        raise CodexTransportBlocked(str(exc)) from None
    with tempfile.TemporaryDirectory(prefix="do-again-codex-") as directory:
        work = Path(directory)
        schema_path = work / "schema.json"
        output_path = work / "answer.json"
        schema_path.write_text(serialized_schema, encoding="utf-8")
        argv = [
            str(selected), "--ask-for-approval", "never", "exec",
            "--sandbox", "read-only",
            "--skip-git-repo-check",
            "--output-schema", str(schema_path),
            "--output-last-message", str(output_path),
        ]
        if model is not None:
            argv.extend(["--model", model])
        argv.append("-")
        try:
            result = subprocess.run(
                argv, input=prompt, cwd=work,
                env=_isolated_environment(operator_home),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, check=False, timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CodexTransportUncertain(
                "Codex execution did not return a final response; do not retry automatically"
            ) from None
        if result.returncode != 0:
            category = _safe_codex_failure_category(result.stdout, result.stderr)
            raise CodexTransportUncertain(
                "Codex did not complete successfully (category=" + category +
                "); do not retry automatically"
            )
        try:
            if output_path.is_symlink() or not output_path.is_file():
                raise ValueError("answer file absent or aliased")
            if output_path.stat().st_size > _MAX_ANSWER_BYTES:
                raise ValueError("answer too large")
            answer = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise CodexTransportUncertain(
                "Codex did not produce valid bounded JSON; do not retry automatically"
            ) from None
        return _validate_response(answer, schema)
