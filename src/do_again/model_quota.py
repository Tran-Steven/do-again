"""Read-only signed-in Codex capacity gate.

Queries the supported Codex app-server account/rateLimits/read interface
without an inference call, browser login, credit purchase, or reset action.
Never publishes account IDs, tokens, raw RPC replies, or email addresses.
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class CodexQuotaUnavailable(RuntimeError):
    """Quota could not be established safely; inference admission stays closed."""


def _quota_window(window: Any) -> dict | None:
    if not isinstance(window, dict):
        return None
    result = {}
    for name in ("usedPercent", "windowDurationMins", "resetsAt"):
        value = window.get(name)
        if type(value) in (int, float) and value >= 0:
            result[name] = value
    return result


def summarize_rate_limits(response: Any) -> dict:
    """Normalize a Codex rate-limit RPC reply without recording account metadata."""
    if not isinstance(response, dict) or not isinstance(response.get("result"), dict):
        raise CodexQuotaUnavailable("Codex account rate limits were unavailable")
    result = response["result"]
    snapshot = result.get("rateLimits")
    if not isinstance(snapshot, dict):
        raise CodexQuotaUnavailable("Codex account returned no rate-limit snapshot")
    ordinary = result.get("ordinaryUsageAllowed")
    if type(ordinary) is not bool:
        ordinary = None
    credits = snapshot.get("credits")
    if not isinstance(credits, dict):
        credits = {}
    reset = result.get("rateLimitResetCredits")
    if not isinstance(reset, dict):
        reset = {}
    reset_count = reset.get("availableCount")
    if type(reset_count) is not int or reset_count < 0:
        reset_count = None
    primary = _quota_window(snapshot.get("primary"))
    secondary = _quota_window(snapshot.get("secondary"))
    all_windows = [w for w in (primary, secondary) if w is not None]
    exhausted = [w for w in all_windows if w.get("usedPercent", -1) >= 100]
    # Any exhausted included window may block usage. If multiple windows
    # block, the longest reset time is the earliest all can clear.
    reset_epoch = max((w["resetsAt"] for w in exhausted if "resetsAt" in w), default=None)
    reset_at = datetime.fromtimestamp(reset_epoch, timezone.utc).isoformat() if reset_epoch else None
    reached = snapshot.get("rateLimitReachedType")
    if not isinstance(reached, str):
        reached = None
    if ordinary is False:
        allowed = False
        reason = "account_rejected_included_usage"
    elif ordinary is True:
        allowed = True
        reason = "account_allowed_included_usage"
    elif exhausted or reached is not None:
        allowed = False
        reason = "rate_limit_window_exhausted"
    else:
        allowed = None
        reason = "account_permission_unknown"
    return {
        "allowed": allowed,
        "reason": reason,
        "primary": primary,
        "secondary": secondary,
        "rate_limit_reached_type": reached,
        "resets_at_utc": reset_at,
        "has_credits": credits.get("hasCredits") if type(credits.get("hasCredits")) is bool else None,
        "reset_credits_available": reset_count,
        "spend_control_reached": snapshot.get("spendControlReached") if type(snapshot.get("spendControlReached")) is bool else None,
    }


def query_codex_rate_limits(binary: Path, home: Path, *, timeout_seconds: int = 18) -> dict:
    """Exactly one read-only status RPC, with bounded process and no raw logs."""
    if os.name != "posix":
        raise CodexQuotaUnavailable("Codex capacity RPC requires the isolated POSIX operator")
    if type(timeout_seconds) is not int or not 3 <= timeout_seconds <= 30:
        raise CodexQuotaUnavailable("Codex capacity read timeout is invalid")
    if (not isinstance(binary, Path) or not binary.is_file()
            or not isinstance(home, Path) or not home.is_absolute() or not home.is_dir()):
        raise CodexQuotaUnavailable("Codex operator identity is unavailable")
    env = {
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "en_US.UTF-8",
    }
    try:
        proc = subprocess.Popen(
            [str(binary), "app-server"], cwd=str(home),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, bufsize=0,
        )
    except OSError:
        raise CodexQuotaUnavailable("Codex account-status subprocess unavailable") from None
    pending = b""
    deadline = time.monotonic() + timeout_seconds

    def send(value: dict) -> None:
        data = (json.dumps(value, separators=(",", ":")) + "\n").encode()
        proc.stdin.write(data)
        proc.stdin.flush()

    def receive(request_id: int) -> dict:
        nonlocal pending
        fd = proc.stdout.fileno()
        while time.monotonic() < deadline:
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if len(line) > 1024 * 1024:
                    raise CodexQuotaUnavailable("Codex quota response exceeded size budget")
                try:
                    item = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(item, dict) and item.get("id") == request_id:
                    return item
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([fd], [], [], min(remaining, 1.0))
            if ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                pending += chunk
                if len(pending) > 1024 * 1024:
                    raise CodexQuotaUnavailable("Codex quota output exceeded size budget")
            if proc.poll() is not None:
                break
        raise CodexQuotaUnavailable("Codex quota read ended without an authenticated response")

    try:
        send({
            "id": 1, "method": "initialize",
            "params": {
                "clientInfo": {"name": "do-again-capacity-gate",
                               "title": "Do Again Capacity Gate", "version": "1.0"},
                "capabilities": {"explicitGatewayOauth": True},
            },
        })
        initialized = receive(1)
        if not isinstance(initialized.get("result"), dict):
            raise CodexQuotaUnavailable("Codex app-server did not initialize")
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read"})
        return summarize_rate_limits(receive(2))
    except (OSError, BrokenPipeError, ValueError) as exc:
        raise CodexQuotaUnavailable("Codex quota probe failed closed") from None
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        if proc.stdin is not None:
            proc.stdin.close()
        if proc.stdout is not None:
            proc.stdout.close()


def require_model_capacity(binary: Path, home: Path) -> dict:
    """Deny when an observed quota block exists; never consume credits/resets."""
    capacity = query_codex_rate_limits(binary, home)
    if capacity["allowed"] is not True:
        when = capacity.get("resets_at_utc") or "unknown"
        available = capacity.get("reset_credits_available")
        raise CodexQuotaUnavailable(
            "Codex model usage blocked (reason=" + capacity["reason"]
            + "; reset_at_utc=" + when
            + "; banked_resets=" + str(available)
            + "); no model call attempted"
        )
    return capacity
