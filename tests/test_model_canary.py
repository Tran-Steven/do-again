"""Offline synthetic Codex model-to-canary request integration tests."""
from __future__ import annotations

import json
import os
import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from do_again.model_canary import (
    CodexCanaryProposalRejected,
    canary_model_schema,
    canary_task_prompt,
    prepare_canary_edit,
)
from do_again.core.schema import validate_request


class CodexCanaryPreparationTests(unittest.TestCase):
    nonce = "a" * 24
    sha = "b" * 40

    def setUp(self):
        self.impl = (
            "def canonical_label(text):\n"
            "    return '-'.join(text.strip().lower().split())\n"
        )
        self.tests = (
            "import unittest\n"
            "from canary_live_" + self.nonce + " import canonical_label\n"
            "class TestCanonicalLabel(unittest.TestCase):\n"
            "    def test_normalize(self):\n"
            "        self.assertEqual(canonical_label(' HI  there '), 'hi-there')\n"
        )
        self.proposal = {"implementation": self.impl, "tests": self.tests}

    def build(self, **kwargs):
        return prepare_canary_edit(
            kwargs.pop("proposal", self.proposal),
            nonce=kwargs.pop("nonce", self.nonce),
            task=kwargs.pop("task", 1),
            expected_head=kwargs.pop("expected_head", self.sha),
            issued_at=kwargs.pop("issued_at", datetime.now(timezone.utc)),
            **kwargs,
        )

    def test_model_schema_is_supported_flat_bounded_object(self):
        schema = canary_model_schema()
        self.assertEqual(set(schema["properties"]), {"implementation", "tests"})
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))

    def test_prompt_contains_only_isolated_synthetic_task(self):
        for task in (1, 2):
            prompt = canary_task_prompt(nonce=self.nonce, task=task)
            self.assertIn("unittest", prompt)
            self.assertIn("canonical_label", prompt)
            self.assertIn(self.nonce, prompt)
            self.assertNotIn("production-ready", prompt)
        self.assertIn("non-ASCII", canary_task_prompt(nonce=self.nonce, task=2))

    def test_generates_exact_brokered_stage_request(self):
        result = self.build()
        self.assertEqual(result["request_id"], "canary-" + self.nonce + "-1-edit")
        self.assertEqual(result["operation"], "scratch_script")
        self.assertEqual(result["args"]["language"], "python")
        self.assertEqual(result["args"]["cwd"], ".")
        self.assertEqual(result["expected"], {"repo_head": self.sha})
        self.assertEqual(result["limits"], {"timeout_seconds": 120})
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(validate_request(result, max_ttl_seconds=3600), result)
        self.assertNotIn("sudo", result["args"]["content"])
        self.assertNotIn("git push", result["args"]["content"])

    def test_trusted_writer_writes_only_two_named_files_when_explicitly_executed(self):
        generated = self.build()["args"]["content"]
        with tempfile.TemporaryDirectory() as temp:
            previous = Path.cwd()
            try:
                os.chdir(temp)
                exec(compile(generated, "<trusted-test-fixture>", "exec"), {})
                source = Path("canary_live_" + self.nonce + ".py")
                test = Path("tests/test_live_canary_" + self.nonce + ".py")
                self.assertEqual(source.read_text(), self.impl)
                self.assertEqual(test.read_text(), self.tests)
                self.assertEqual(
                    sorted(str(p) for p in Path(".").rglob("*") if p.is_file()),
                    sorted([str(source), str(test)]),
                )
            finally:
                os.chdir(previous)

    def test_second_task_is_scoped_and_retains_expected_head(self):
        candidate = self.build(task=2)
        self.assertEqual(candidate["request_id"], "canary-" + self.nonce + "-2-edit")
        self.assertEqual(candidate["expected"]["repo_head"], self.sha)

    def test_malformed_scopes_and_output_fail_before_publishing(self):
        for changes in (
            {"nonce": "../escape"},
            {"nonce": None},
            {"expected_head": "main"},
            {"expected_head": None},
            {"task": 3},
            {"task": True},
            {"proposal": {"implementation": self.impl}},
            {"proposal": {"implementation": self.impl, "tests": self.tests, "target": ".github/workflows"}},
            {"proposal": {"implementation": "bad syntax: !", "tests": self.tests}},
            {"proposal": {"implementation": self.impl, "tests": self.tests + "\nimport os"}},
        ):
            with self.subTest(changes=changes), self.assertRaises(CodexCanaryProposalRejected):
                self.build(**changes)

    def test_rejects_imports_side_effects_and_python_escape_primitives(self):
        for source in (
            "import os\n" + self.impl,
            "def canonical_label(text):\n    open('/tmp/pwn', 'w')\n    return text\n",
            "def canonical_label(text):\n    return __import__('os').system('id')\n",
            "def canonical_label(text):\n    return text.__class__\n",
            "def canonical_label(text):\n    return getattr(text, 'lower')()\n",
            "def canonical_label(text):\n    with open('x') as f: return f.read()\n",
            "def canonical_label(text):\n    return [x for x in text]\n",
        ):
            with self.subTest(source=source), self.assertRaises(CodexCanaryProposalRejected):
                self.build(proposal={"implementation": source, "tests": self.tests})

    def test_nested_test_imports_cannot_avoid_top_level_import_allowlist(self):
        nested = self.tests.replace(
            "        self.assertEqual(canonical_label(' HI  there '), 'hi-there')",
            "        import os\\n"
            "        self.assertEqual(canonical_label(' HI  there '), 'hi-there')",
        )
        with self.assertRaises(CodexCanaryProposalRejected):
            self.build(proposal={"implementation": self.impl, "tests": nested})

    def test_empty_unittest_case_cannot_claim_success(self):
        empty = (
            "import unittest\\n"
            "from canary_live_" + self.nonce + " import canonical_label\\n"
            "class TestEmpty(unittest.TestCase):\\n"
            "    pass\\n"
        )
        with self.assertRaises(CodexCanaryProposalRejected):
            self.build(proposal={"implementation": self.impl, "tests": empty})

    def test_rejects_wrong_module_and_extra_test_classes(self):
        bad = self.tests.replace("canary_live_" + self.nonce, "other_module")
        with self.assertRaises(CodexCanaryProposalRejected):
            self.build(proposal={"implementation": self.impl, "tests": bad})
        bad = self.tests + "\nclass Other(unittest.TestCase): pass\n"
        with self.assertRaises(CodexCanaryProposalRejected):
            self.build(proposal={"implementation": self.impl, "tests": bad})

    def test_task_two_source_is_compiled_not_executed_by_preparation(self):
        second = (
            "def canonical_label(text):\n"
            "    if not text.isascii():\n"
            "        raise ValueError('non ASCII')\n"
            "    return '-'.join(text.strip().lower().split())\n"
        )
        case = self.tests.replace(
            "    def test_normalize(self):",
            "    def test_ascii_guard(self):\n"
            "        self.assertRaises(ValueError, canonical_label, 'é')\n"
            "    def test_normalize(self):",
        )
        result = self.build(proposal={"implementation": second, "tests": case}, task=2)
        self.assertIn("source_code = ", result["args"]["content"])
        self.assertIn("test_code = ", result["args"]["content"])
        self.assertNotIn("import os", result["args"]["content"])


class CodexCanaryCliTests(unittest.TestCase):
    nonce = "c" * 24
    head = "d" * 40

    def test_draft_requires_opt_in_before_any_model_call(self):
        from do_again.cli import _model_action
        with patch("do_again.model_transport.generate_structured") as generate:
            with patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(_model_action("canary-draft", nonce=self.nonce,
                    task=1, expected_head=self.head), 1)
        generate.assert_not_called()

    def test_draft_returns_candidate_but_does_not_dispatch_any_effect(self):
        from do_again.cli import _model_action
        implementation = "def canonical_label(text):\n    return '-'.join(text.strip().lower().split())\n"
        tests = (
            "import unittest\n"
            "from canary_live_" + self.nonce + " import canonical_label\n"
            "class TestLabel(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertEqual(canonical_label('ABC X'), 'abc-x')\n"
        )
        with patch("do_again.model_transport.discover_codex", return_value=Path("/isolated/codex")), patch(
                "do_again.model_transport.generate_structured",
                return_value={"implementation": implementation, "tests": tests}) as generate, patch(
                "sys.stdout", new_callable=io.StringIO) as output:
            result = _model_action("canary-draft", allow_model_call=True, nonce=self.nonce,
                task=1, expected_head=self.head)
        self.assertEqual(result, 0)
        self.assertEqual(generate.call_count, 1)
        call = generate.call_args
        self.assertTrue(call.kwargs["allow_model_call"])
        decoded = json.loads(output.getvalue())
        self.assertEqual(decoded["state"], "proposal_only")
        self.assertIs(decoded["published"], False)
        self.assertIs(decoded["executed"], False)
        self.assertEqual(decoded["request"]["request_id"], "canary-" + self.nonce + "-1-edit")
        self.assertEqual(decoded["request"]["expected"]["repo_head"], self.head)


if __name__ == "__main__":
    unittest.main()
