"""Offline Codex-to-canary edit proposal boundary.

This module does not call a model or publish any GitHub request. A trusted
publisher must bind its result to a sealed canary grant and submit it through
the existing authority-fenced request protocol.
"""
from __future__ import annotations

import ast
import json
import re
from datetime import datetime, timedelta, timezone

from .core.schema import OperatorError, validate_request

_NONCE = re.compile(r"[0-9a-f]{24}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_MAX_SOURCE = 12000
_APPROVED_ATTRIBUTES = frozenset({
    "lower", "split", "join", "strip", "isascii",
    "assertEqual", "assertRaises", "assertRaisesRegex",
    "assertTrue", "assertFalse", "assertIn", "assertIsInstance",
})
_APPROVED_CALLS = frozenset({"canonical_label", "str", "len", "ValueError", "TypeError"})
_BANNED_NODES = (
    ast.AsyncFunctionDef, ast.Await, ast.Import, ast.ImportFrom,
    ast.Lambda, ast.Global, ast.Nonlocal, ast.With, ast.AsyncWith,
    ast.Try, ast.Delete,
    ast.Yield, ast.YieldFrom, ast.ListComp, ast.SetComp,
    ast.DictComp, ast.GeneratorExp, ast.NamedExpr,
)


class CodexCanaryProposalRejected(OperatorError):
    """The candidate cannot be represented as a scoped synthetic request."""


def canary_model_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "implementation": {"type": "string", "maxLength": _MAX_SOURCE},
            "tests": {"type": "string", "maxLength": _MAX_SOURCE},
        },
        "required": ["implementation", "tests"],
        "additionalProperties": False,
    }


def canary_task_prompt(*, nonce: str, task: int) -> str:
    if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce) or type(task) is not int or task not in (1, 2):
        raise CodexCanaryProposalRejected("invalid bounded canary identity")
    extra = (
        "Implement lower-case ASCII input with trimmed, collapsed whitespace joined by hyphens."
        if task == 1 else
        "Retain task-one behavior and reject non-ASCII input with ValueError."
    )
    return (
        "Return JSON with exactly two fields: implementation and tests. "
        "This is synthetic, non-production code. Produce Python source with "
        "one canonical_label(text) function, no imports and no side effects. "
        + extra + " The tests must use unittest, import canonical_label from "
        + "canary_live_" + nonce + ", and define a unittest.TestCase with "
        "test_* methods for expected behavior. Python AST syntax is "
        "strictly restricted: do not use with, try/except, decorators, "
        "comprehensions, nested imports, module-level code, or helper classes. "
        "Implementation must be exactly one plain canonical_label(text) function "
        "without imports. The tests must contain exactly import unittest, "
        "from canary_live_" + nonce + " import canonical_label, and one "
        "TestLabel(unittest.TestCase) with only test_* methods. To verify "
        "ValueError, use self.assertRaises(ValueError, canonical_label, 'caf\\u00e9') "
        "as a direct call, NOT with self.assertRaises(...). Use only "
        "self.assertEqual and self.assertRaises assertions. Do not make network "
        "calls, read local files, or use subprocesses. Return complete source "
        "code for both files without Markdown fences."
    )


def _source_ast(text: str, *, tests: bool, module: str) -> None:
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > _MAX_SOURCE:
        raise CodexCanaryProposalRejected("model source is empty or exceeds the bounded budget")
    try:
        tree = ast.parse(text)
        compile(tree, "<unexecuted-canary-source>", "exec")
    except (SyntaxError, ValueError, TypeError) as exc:
        raise CodexCanaryProposalRejected("model source is not valid Python") from None
    if tests:
        imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        if len(imports) != 2:
            raise CodexCanaryProposalRejected("tests must import only unittest and the scoped module")
        if not (isinstance(imports[0], ast.Import)
                and len(imports[0].names) == 1 and imports[0].names[0].name == "unittest"
                and imports[0].names[0].asname is None
                and isinstance(imports[1], ast.ImportFrom)
                and imports[1].module == module and imports[1].level == 0
                and len(imports[1].names) == 1 and imports[1].names[0].name == "canonical_label"
                and imports[1].names[0].asname is None):
            raise CodexCanaryProposalRejected("tests imported an unapproved capability")
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
        if (len(classes) != 1 or classes[0].decorator_list or classes[0].keywords
                or len(classes[0].bases) != 1
                or not isinstance(classes[0].bases[0], ast.Attribute)
                or not isinstance(classes[0].bases[0].value, ast.Name)
                or classes[0].bases[0].value.id != "unittest"
                or classes[0].bases[0].attr != "TestCase"
                or not classes[0].name.startswith("Test")):
            raise CodexCanaryProposalRejected("tests require one plain unittest.TestCase")
        if (len(tree.body) != 3 or not classes[0].body or not all(
                isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
                and n.args.args and n.args.args[0].arg == "self"
                and not n.decorator_list
                for n in classes[0].body)):
            raise CodexCanaryProposalRejected("tests must be ordinary test methods")
    else:
        if (len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef)
                or tree.body[0].name != "canonical_label" or tree.body[0].decorator_list
                or len(tree.body[0].args.args) != 1
                or tree.body[0].args.args[0].arg != "text"):
            raise CodexCanaryProposalRejected("implementation must define only canonical_label(text)")
    for node in ast.walk(tree):
        if isinstance(node, _BANNED_NODES):
            # Only the exact two validated top-level test imports are accepted.
            # A nested import inside a test method cannot borrow their authority.
            if tests and node in tree.body and isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            raise CodexCanaryProposalRejected("model source contains an excluded syntax capability")
        if isinstance(node, ast.Attribute):
            if (node.attr not in _APPROVED_ATTRIBUTES and
                    not (tests and node.attr == "TestCase" and isinstance(node.value, ast.Name)
                         and node.value.id == "unittest")):
                raise CodexCanaryProposalRejected("model source contains an unapproved attribute")
        if isinstance(node, ast.Call):
            if not (
                isinstance(node.func, ast.Attribute) and node.func.attr in _APPROVED_ATTRIBUTES
                or isinstance(node.func, ast.Name) and node.func.id in _APPROVED_CALLS
            ):
                raise CodexCanaryProposalRejected("model source contains an unapproved function call")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise CodexCanaryProposalRejected("model source contains protected object access")


def prepare_canary_edit(
    proposal: dict,
    *,
    nonce: str,
    task: int,
    expected_head: str,
    issued_at: datetime | None = None,
) -> dict:
    """Translate model output into a request object, with no browser or GitHub effect."""
    if (not isinstance(nonce, str) or not _NONCE.fullmatch(nonce)
            or type(task) is not int or task not in (1, 2)
            or not isinstance(expected_head, str) or not _SHA.fullmatch(expected_head)):
        raise CodexCanaryProposalRejected("canary scope or exact source head is invalid")
    if not isinstance(proposal, dict) or set(proposal) != {"implementation", "tests"}:
        raise CodexCanaryProposalRejected("model output must have exactly the two approved fields")
    module = "canary_live_" + nonce
    test_file = "tests/test_live_canary_" + nonce + ".py"
    implementation, tests = proposal["implementation"], proposal["tests"]
    _source_ast(implementation, tests=False, module=module)
    _source_ast(tests, tests=True, module=module)
    now = issued_at if issued_at is not None else datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise CodexCanaryProposalRejected("canary issued-at value must be timezone-aware")
    now = now.astimezone(timezone.utc)
    paths = [module + ".py", test_file]
    writer = (
        "from pathlib import Path\n"
        "source_path = Path(" + repr(paths[0]) + ")\n"
        "test_path = Path(" + repr(paths[1]) + ")\n"
        "source_code = " + repr(implementation) + "\n"
        "test_code = " + repr(tests) + "\n"
        "compile(source_code, str(source_path), 'exec')\n"
        "compile(test_code, str(test_path), 'exec')\n"
        "test_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "source_path.write_text(source_code, encoding='utf-8')\n"
        "test_path.write_text(test_code, encoding='utf-8')\n"
    )
    result = {
        "schema_version": 1,
        "request_id": "canary-" + nonce + "-" + str(task) + "-edit",
        "operation": "scratch_script",
        "issued_at_utc": now.isoformat(),
        "expires_at_utc": (now + timedelta(minutes=20)).isoformat(),
        "args": {"language": "python", "content": writer, "cwd": "."},
        "expected": {"repo_head": expected_head},
        "limits": {"timeout_seconds": 120},
    }
    try:
        return validate_request(result, max_ttl_seconds=3600)
    except OperatorError as exc:
        raise CodexCanaryProposalRejected("candidate did not satisfy standard request schema") from exc


def validate_prepared_edit(request: dict, *, nonce: str, task: int, expected_head: str) -> dict:
    """Reconstruct an edit from its two literal sources; never execute the writer.

    An approved request ID cannot authorize arbitrary Python. Only the exact
    deterministic writer produced by prepare_canary_edit is admissible.
    """
    if not isinstance(request, dict) or not isinstance(request.get("args"), dict):
        raise CodexCanaryProposalRejected("canary edit must be an approved request object")
    code = request["args"].get("content")
    if not isinstance(code, str) or len(code.encode("utf-8")) > 65536:
        raise CodexCanaryProposalRejected("canary writer exceeds its fixed budget")
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, TypeError):
        raise CodexCanaryProposalRejected("canary writer could not be parsed") from None
    values = {}
    for item in tree.body:
        if not isinstance(item, ast.Assign) or len(item.targets) != 1:
            continue
        target = item.targets[0]
        if (isinstance(target, ast.Name) and target.id in {"source_code", "test_code"}):
            if target.id in values or not isinstance(item.value, ast.Constant) or not isinstance(item.value.value, str):
                raise CodexCanaryProposalRejected("canary writer source assignments are not fixed literals")
            values[target.id] = item.value.value
    if set(values) != {"source_code", "test_code"}:
        raise CodexCanaryProposalRejected("canary writer has no exact implementation and tests")
    issued_at = request.get("issued_at_utc")
    if not isinstance(issued_at, str):
        raise CodexCanaryProposalRejected("canary request has no timestamp")
    try:
        issued = datetime.fromisoformat(issued_at)
    except ValueError:
        raise CodexCanaryProposalRejected("canary request timestamp is invalid") from None
    candidate = prepare_canary_edit(
        {"implementation": values["source_code"], "tests": values["test_code"]},
        nonce=nonce, task=task, expected_head=expected_head, issued_at=issued,
    )
    if request != candidate:
        raise CodexCanaryProposalRejected("canary request differs from the canonical bounded writer")
    return candidate
