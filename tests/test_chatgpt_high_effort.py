"""Fail-closed verification of ChatGPT's actual High thinking control."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from do_again.browser import cdp
from do_again.browser import runtime


class HighReasoningSelectionTests(unittest.TestCase):
    def setUp(self):
        self.target=cdp.Target("target","https://chatgpt.com/","","ws://127.0.0.1/fake")

    def test_medium_moves_to_high_before_any_prompt(self):
        # The live October 2026 UI exposes 0=Instant,1=Medium,2=High.
        values=iter([
            "opened", {"min":"0","max":"2","value":"1","x":1130,"y":460},
            True, True,
        ])
        with patch.object(cdp,"evaluate",side_effect=lambda *a,**kw:next(values)) as ev, \
             patch.object(cdp,"target_call",return_value={}) as call:
            runtime._ensure_high_reasoning(self.target)
        self.assertEqual(ev.call_count,4)
        self.assertEqual([c.args[1] for c in call.call_args_list],
                         ["Input.dispatchKeyEvent"]*4)
        self.assertEqual([c.args[2]["key"] for c in call.call_args_list],
                         ["End","End","Escape","Escape"])
        self.assertEqual([c.args[2]["type"] for c in call.call_args_list],
                         ["keyDown","keyUp","keyDown","keyUp"])

    def test_already_high_is_idempotent_and_does_not_click_slider(self):
        values=iter(["opened",{"min":"0","max":"2","value":"2","x":1130,"y":460},
                     True,True])
        with patch.object(cdp,"evaluate",side_effect=lambda *a,**kw:next(values)), \
             patch.object(cdp,"target_call") as call:
            runtime._ensure_high_reasoning(self.target)
        self.assertEqual([c.args[2]["key"] for c in call.call_args_list],
                         ["Escape","Escape"])

    def test_unknown_selector_fails_before_any_mouse_or_message(self):
        for state in ["missing_picker","other"]:
            with self.subTest(state=state), \
                 patch.object(cdp,"evaluate",return_value=state), \
                 patch.object(cdp,"target_call") as call:
                with self.assertRaises(runtime.BrowserPreDispatchBlocked):
                    runtime._ensure_high_reasoning(self.target)
                call.assert_not_called()

    def test_high_unavailable_or_unverified_fails_closed(self):
        for observation,verified in [
            (None,False),
            ({"min":"0","max":"3","value":"1","x":1130,"y":460},False),
            ({"min":"0","max":"2","value":"1","x":1130,"y":460},False),
        ]:
            with self.subTest(observation=observation):
                if observation is None:
                    answers=["opened"]+[None]*12+[True]
                else:
                    answers=["opened",observation]+[verified]*12+[True]
                with patch.object(cdp,"evaluate",side_effect=answers), \
                     patch.object(cdp,"target_call",return_value={}), \
                     patch.object(runtime.time,"sleep"):
                    with self.assertRaises(runtime.BrowserPreDispatchBlocked):
                        runtime._ensure_high_reasoning(self.target)

    def test_bootstrap_never_sends_when_high_cannot_be_confirmed(self):
        with patch.object(cdp,"create_target",return_value=self.target), \
             patch.object(runtime,"wait_for_authenticated",return_value=(self.target,{"prompt":True})), \
             patch.object(runtime,"_ensure_high_reasoning",
                          side_effect=runtime.BrowserPreDispatchBlocked("High unavailable")), \
             patch.object(runtime,"send_message") as send, \
             patch.object(cdp,"close_target") as close:
            with self.assertRaises(runtime.BrowserPreDispatchBlocked):
                runtime._bootstrap_new_chat(9223,"bootstrap")
        send.assert_not_called()
        close.assert_called_once_with(9223,self.target.id)

    def test_rollover_does_not_send_if_high_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)/"repo";repo.mkdir()
            record={"repo":str(repo),"chat_url":"https://chatgpt.com/c/old",
                    "remote_url":"https://github.com/example/repo.git","control_branch":"operator-control"}
            with patch.dict(runtime.os.environ,{"DO_AGAIN_HOME":str(Path(tmp)/"home")}), \
                 patch.object(runtime,"_build_rollover_checkpoint",return_value={"schema_version":1}), \
                 patch.object(runtime,"_checkpoint_transfer_view",return_value={}), \
                 patch.object(runtime,"_bootstrap_prompt",return_value="handoff"), \
                 patch.object(cdp,"create_target",return_value=self.target), \
                 patch.object(runtime,"wait_for_authenticated",return_value=(self.target,{"prompt":True})), \
                 patch.object(runtime,"_ensure_high_reasoning",
                              side_effect=runtime.BrowserPreDispatchBlocked("High unavailable")), \
                 patch.object(runtime,"send_message") as send, \
                 patch.object(cdp,"close_target") as close:
                with self.assertRaises(runtime.BrowserPreDispatchBlocked):
                    runtime._rollover_project_chat(repo,record,port=9223)
            send.assert_not_called()
            close.assert_called_once_with(9223,self.target.id)


if __name__=="__main__":
    unittest.main()
