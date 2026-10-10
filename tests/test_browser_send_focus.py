"""Read-only focus checks before the single ChatGPT Send gesture.

No test dispatches a browser message, creates a target, or accesses Chrome.
"""
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from do_again.browser import cdp, runtime
from do_again.browser.errors import BrowserError, BrowserPreDispatchBlocked


class ChatSendFocusTests(unittest.TestCase):
    def setUp(self):
        self.target = cdp.Target("test-tab", "https://chatgpt.com/", "",
                                 "ws://127.0.0.1:9223/devtools/page/test-tab")

    def test_auto_background_blocks_before_any_focus_or_message_effect(self):
        commit = Mock()
        with patch.object(runtime, "load_config", return_value={"preferred_mode": "auto", "allow_visible_fallback": False}), patch.object(runtime, "load_state", return_value={"mode": "background"}), patch.object(cdp, "evaluate") as evaluate, patch.object(cdp, "target_call") as activate, patch.object(cdp, "insert_text") as insert, patch.object(cdp, "click_send") as click:
            with self.assertRaisesRegex(BrowserPreDispatchBlocked, "no browser submission was attempted"):
                runtime.send_message(self.target, "UNSENT", before_dispatch=commit)
        evaluate.assert_not_called()
        activate.assert_not_called()
        insert.assert_not_called()
        click.assert_not_called()
        commit.assert_not_called()

    def test_already_focused_page_requires_no_activation(self):
        with patch.object(cdp, "evaluate", return_value={
                "focused": True, "visibility": "visible"}) as evaluate, patch.object(
                cdp, "target_call") as call:
            runtime._require_send_target_focus(self.target)
        evaluate.assert_called_once()
        call.assert_not_called()

    def test_background_tab_activates_once_before_gesture(self):
        with patch.object(cdp, "evaluate", side_effect=[
                {"focused": False, "visibility": "hidden"},
                {"focused": True, "visibility": "visible"}]) as evaluate, patch.object(
                cdp, "target_call") as call:
            runtime._require_send_target_focus(self.target)
        call.assert_called_once_with(
            self.target, "Page.bringToFront", {}, timeout=10.0)
        self.assertEqual(evaluate.call_count, 2)

    def test_activated_but_unfocused_target_fails_closed(self):
        with patch.object(cdp, "evaluate", side_effect=[
                {"focused": False, "visibility": "hidden"},
                {"focused": False, "visibility": "visible"}]), patch.object(
                cdp, "target_call") as call:
            with self.assertRaisesRegex(BrowserPreDispatchBlocked, "no browser submission was attempted"):
                runtime._require_send_target_focus(self.target)
        call.assert_called_once()

    def test_emulated_focus_or_invalid_observation_is_not_trusted(self):
        for value in (None, {}, {"focused": "true", "visibility": "visible"},
                      {"focused": True, "visibility": "hidden"}):
            with self.subTest(value=value), patch.object(cdp, "evaluate",
                    return_value=value), patch.object(cdp, "target_call"):
                with self.assertRaises(BrowserPreDispatchBlocked):
                    runtime._require_send_target_focus(self.target)

    def test_send_message_cannot_modify_composer_or_commit_dispatch_without_focus(self):
        commit = Mock()
        with patch.object(cdp, "evaluate", return_value={
                    "focused": False, "visibility": "hidden"}), patch.object(
                    cdp, "target_call") as bring, patch.object(
                    cdp, "insert_text") as insert, patch.object(
                    cdp, "click_send") as click, patch.object(
                    runtime, "_assistant_snapshot") as snapshot:
            with self.assertRaisesRegex(BrowserPreDispatchBlocked, "no browser submission was attempted"):
                runtime.send_message(self.target, "DO_AGAIN_TEST", before_dispatch=commit)
        bring.assert_called_once_with(
            self.target, "Page.bringToFront", {}, timeout=10.0)
        commit.assert_not_called()
        snapshot.assert_not_called()
        insert.assert_not_called()
        click.assert_not_called()

    def test_bring_to_front_error_is_not_retried_or_dispatched(self):
        commit = Mock()
        with patch.object(cdp, "evaluate", return_value={
                    "focused": False, "visibility": "hidden"}), patch.object(
                    cdp, "target_call", side_effect=BrowserError("CDP lost")) as call, patch.object(
                    cdp, "click_send") as click:
            with self.assertRaisesRegex(BrowserError, "CDP lost"):
                runtime.send_message(self.target, "test", before_dispatch=commit)
        call.assert_called_once()
        commit.assert_not_called()
        click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
