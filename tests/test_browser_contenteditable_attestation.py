"""Exact ChatGPT contenteditable line-break attestation without live browser Sends.

Node executes only the read-only composer readiness expression extracted from
runtime.py. No Chrome session, model call, browser write, or service operation.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

RUNTIME = Path(__file__).resolve().parents[1] / "src/do_again/browser/runtime.py"
NODE = shutil.which("node")
NODE_SCRIPT = r"""
const source = process.argv[1];
const fixture = JSON.parse(process.argv[2]);
const makeEditor = obj => {
  const el = {
    isContentEditable: obj.isContentEditable,
    innerText: obj.innerText,
    textContent: obj.textContent,
    getBoundingClientRect: () => ({width: 120, height: 40}),
  };
  if (obj.value !== undefined) el.value = obj.value;
  return el;
};
const editor = makeEditor(fixture.editor);
const button = {
  disabled: fixture.disabled || false,
  getAttribute: key => key === 'aria-disabled' ? fixture.ariaDisabled || null : null,
  getBoundingClientRect: () => ({width: 50, height: 30}),
};
const document = {activeElement: fixture.editorFocused === false ? null : editor,
  querySelectorAll: selector =>
  selector.startsWith('#prompt-textarea')
    ? (fixture.extraEditors ? [editor, makeEditor(fixture.editor)] : [editor])
    : (fixture.extraButtons ? [button, button] : fixture.buttonMissing ? [] : [button])};
const getComputedStyle = () => ({display: 'block', visibility: 'visible'});
const result = Function('document', 'getComputedStyle', 'return ' + source)(
  document, getComputedStyle);
process.stdout.write(JSON.stringify(result));
"""


@unittest.skipUnless(NODE, "Node.js needed for exact DOM expression tests")
class ContenteditableSendReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = RUNTIME.read_text(encoding="utf-8")
        start = 'readiness = r"""'
        a = src.index(start, src.index("def send_message(")) + len(start)
        b = src.index('""".replace(\'EXPECTED\'', a)
        cls.expression = src[a:b]
        cls.expected = (
            "DO_AGAIN_RECEIPTS_READY request_ids=alpha,beta,gamma,delta. "
            "Inspect these receipts on operator-control in order.\n"
            "- alpha state=succeeded path=automation/do_again/receipts/alpha.json\n"
            "- beta state=succeeded path=automation/do_again/receipts/beta.json\n"
            "- gamma state=succeeded path=automation/do_again/receipts/gamma.json\n"
            "- delta state=succeeded path=automation/do_again/receipts/delta.json\n"
            "After inspecting the receipts, include this exact acknowledgment "
            "on its own line: DO_AGAIN_RECEIPT_ACK token=" + "a" * 32
        )

    def evaluate(self, *, expected=None, rendered=None, flattened=None, **kwargs):
        expected = self.expected if expected is None else expected
        expr = self.expression.replace("EXPECTED", json.dumps(expected))
        fixture = {
            "editor": {
                "isContentEditable": True,
                "innerText": (
                    expected.replace("\n", "\n\n") if rendered is None
                    else rendered
                ),
                "textContent": (
                    expected.replace("\n", "") if flattened is None
                    else flattened
                ),
            },
            **kwargs,
        }
        proc = subprocess.run(
            [NODE, "-e", NODE_SCRIPT, expr, json.dumps(fixture)],
            check=True, capture_output=True, text=True, timeout=10,
        )
        return json.loads(proc.stdout)

    def test_exact_dom_structural_newline_representation_is_accepted(self):
        # Five physical lines => +5 in innerText and -5 in textContent,
        # exactly the on-device redacted diagnostic shape.
        self.assertEqual(self.expected.count("\n"), 5)
        self.assertTrue(self.evaluate())

    def test_exact_contenteditable_editor_is_also_accepted(self):
        self.assertTrue(self.evaluate(rendered=self.expected,
                                      flattened=self.expected))

    def test_other_whitespace_variants_remain_blocked(self):
        self.assertFalse(self.evaluate(rendered=self.expected.replace("\n", " "),
                                       flattened=self.expected.replace("\n", "")))
        self.assertFalse(self.evaluate(rendered=self.expected.replace("\n", "\n\n ") ,
                                       flattened=self.expected.replace("\n", "")))
        self.assertFalse(self.evaluate(rendered=self.expected.replace("\n", "\r\n"),
                                       flattened=self.expected.replace("\n", "")))

    def test_unfocused_composer_blocks_all_send_paths(self):
        self.assertFalse(self.evaluate(editorFocused=False))

    def test_changed_receipt_identity_or_acknowledgment_is_blocked(self):
        changed_id = self.expected.replace("alpha", "ALPHA", 1)
        self.assertFalse(self.evaluate(rendered=changed_id.replace("\n", "\n\n"),
                                       flattened=changed_id.replace("\n", "")))
        changed_ack = self.expected.replace("a" * 32, "b" * 32)
        self.assertFalse(self.evaluate(rendered=changed_ack.replace("\n", "\n\n"),
                                       flattened=changed_ack.replace("\n", "")))

    def test_invalid_button_and_editor_topology_is_blocked(self):
        for params in (
            {"buttonMissing": True},
            {"disabled": True},
            {"ariaDisabled": "true"},
            {"extraButtons": True},
            {"extraEditors": True},
        ):
            with self.subTest(params=params):
                self.assertFalse(self.evaluate(**params))

    def test_plain_textarea_must_pass_literal_value_integrity(self):
        raw = self.expected.replace("\n", "\n\n")
        self.assertFalse(self.evaluate_textarea(raw))
        self.assertTrue(self.evaluate_textarea(self.expected))

    def evaluate_textarea(self, value):
        expr = self.expression.replace("EXPECTED", json.dumps(self.expected))
        fixture = {
            "editor": {"isContentEditable": False, "value": value,
                       "innerText": value, "textContent": value}
        }
        proc = subprocess.run(
            [NODE, "-e", NODE_SCRIPT, expr, json.dumps(fixture)],
            check=True, capture_output=True, text=True, timeout=10,
        )
        return json.loads(proc.stdout)


if __name__ == "__main__":
    unittest.main()
