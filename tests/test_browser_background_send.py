import unittest
from unittest.mock import Mock, patch

from do_again.browser import cdp, runtime
from do_again.browser.errors import BrowserPreDispatchBlocked, BrowserSubmissionUncertain


class BackgroundChatSendTests(unittest.TestCase):
    def setUp(self):
        self.target = cdp.Target(
            "background-test", "https://chatgpt.com/c/background-test", "",
            "ws://127.0.0.1:9223/devtools/page/background-test",
        )
        self.config = {
            "preferred_mode": "auto",
            "allow_visible_fallback": False,
        }
        self.state = {"mode": "background"}

    def test_background_hidden_chat_can_send_one_gesture_without_window_activation(self):
        effects = []
        observations = [
            {"focused": False, "visibility": "hidden",
             "url": "https://chatgpt.com/c/background-test",
             "ready": "complete", "editor_visible": True},
            "ready", True, True,
        ]

        def call(target, operation, arguments, **kwargs):
            effects.append((operation, arguments))
            self.assertNotEqual(operation, "Page.bringToFront")

        def enter(target):
            effects.append(("enter", {}))

        def durable():
            effects.append(("dispatch_intent", {}))

        with patch.object(runtime, "load_config", return_value=self.config), \
             patch.object(runtime, "load_state", return_value=self.state), \
             patch.object(runtime, "_assistant_snapshot", return_value={"busy": False}), \
             patch.object(runtime, "_context_limit_warning", return_value=""), \
             patch.object(cdp, "target_call", side_effect=call), \
             patch.object(cdp, "evaluate", side_effect=observations) as evaluate, \
             patch.object(cdp, "insert_text") as insert, \
             patch.object(cdp, "press_enter", side_effect=enter) as gesture, \
             patch.object(runtime.time, "sleep"):
            result = runtime.send_message(
                self.target, "SCOPED_BACKGROUND_TEST", background_focus=True,
                wait_for_response=False, before_dispatch=durable,
            )

        self.assertEqual(result["response"], "submitted")
        self.assertEqual(evaluate.call_count, 4)
        insert.assert_called_once_with(self.target, "SCOPED_BACKGROUND_TEST")
        gesture.assert_called_once_with(self.target)
        self.assertEqual(
            [entry[0] for entry in effects],
            ["Emulation.setFocusEmulationEnabled", "dispatch_intent",
             "enter", "Emulation.setFocusEmulationEnabled"],
        )
        self.assertTrue(effects[0][1]["enabled"])
        self.assertFalse(effects[-1][1]["enabled"])

    def test_explicit_background_mode_emulates_focus_without_activating_gui(self):
        # Live regression: the v1 worker switched from auto to explicit
        # background to keep its authenticated GUI Chrome session. The
        # previous guard enabled emulation only for auto, blocking every Send.
        self.config["preferred_mode"] = "background"
        self.assertIs(self.config["allow_visible_fallback"], False)
        calls = []
        states = [
            {"focused": False, "visibility": "hidden",
             "url": "https://chatgpt.com/c/background-test",
             "ready": "complete", "editor_visible": True},
            "ready", True, True,
        ]
        def command(target, operation, arguments, **kwargs):
            self.assertNotEqual(operation, "Page.bringToFront")
            calls.append((operation, arguments))
        with patch.object(runtime, "load_config", return_value=self.config), \
             patch.object(runtime, "load_state", return_value=self.state), \
             patch.object(runtime, "_assistant_snapshot", return_value={"busy": False}), \
             patch.object(runtime, "_context_limit_warning", return_value=""), \
             patch.object(cdp, "target_call", side_effect=command), \
             patch.object(cdp, "evaluate", side_effect=states), \
             patch.object(cdp, "insert_text") as insert, \
             patch.object(cdp, "press_enter") as enter, \
             patch.object(cdp, "click_send") as click, \
             patch.object(runtime.time, "sleep"):
            result = runtime.send_message(
                self.target, "EXPLICIT_BACKGROUND", wait_for_response=False,
                background_focus=True,
            )
        self.assertEqual(result["response"], "submitted")
        insert.assert_called_once_with(self.target, "EXPLICIT_BACKGROUND")
        enter.assert_called_once_with(self.target)
        click.assert_not_called()
        self.assertEqual(
            [name for name, _ in calls],
            ["Emulation.setFocusEmulationEnabled",
             "Emulation.setFocusEmulationEnabled"],
        )
        self.assertEqual([c[1]["enabled"] for c in calls], [True, False])

    def test_explicit_background_without_virtual_focus_remains_unsendable(self):
        self.config["preferred_mode"] = "background"
        with patch.object(runtime, "load_config", return_value=self.config), \
             patch.object(runtime, "load_state", return_value=self.state), \
             patch.object(cdp, "evaluate", return_value={
                 "focused": False, "visibility": "hidden",
             }), \
             patch.object(cdp, "target_call") as events, \
             patch.object(cdp, "insert_text") as insert, \
             patch.object(cdp, "click_send") as click:
            with self.assertRaises(BrowserPreDispatchBlocked):
                runtime.send_message(self.target, "DO_NOT_SEND")
        events.assert_not_called()
        insert.assert_not_called()
        click.assert_not_called()

    def test_explicit_background_with_visible_fallback_does_not_grant_virtual_focus(self):
        self.config.update(preferred_mode="background", allow_visible_fallback=True)
        with patch.object(runtime, "load_config", return_value=self.config), \
             patch.object(runtime, "load_state", return_value=self.state), \
             patch.object(cdp, "evaluate", return_value={
                 "focused": False, "visibility": "hidden",
             }), \
             patch.object(cdp, "target_call") as events, \
             patch.object(cdp, "insert_text") as insert, \
             patch.object(cdp, "click_send") as click:
            with self.assertRaises(BrowserPreDispatchBlocked):
                runtime.send_message(self.target, "DO_NOT_SEND", background_focus=True)
        events.assert_not_called()
        insert.assert_not_called()
        click.assert_not_called()

    def test_virtual_focus_rejects_other_origins_or_unready_composer(self):
        for invalid in (
            {"focused": False, "visibility": "hidden",
             "url": "https://chatgpt.com/c/background-test",
             "ready": "loading", "editor_visible": True},
            {"focused": False, "visibility": "hidden",
             "url": "https://chatgpt.com/c/background-test",
             "ready": "complete", "editor_visible": False},
            {"focused": False, "visibility": "hidden",
             "url": "https://example.com/c/background-test",
             "ready": "complete", "editor_visible": True},
            {"focused": "false", "visibility": "hidden",
             "url": "https://chatgpt.com/c/background-test",
             "ready": "complete", "editor_visible": True},
            {"focused": False, "visibility": "hidden",
             "ready": "complete", "editor_visible": True},
        ):
            with self.subTest(invalid=invalid), \
                 patch.object(runtime, "load_config", return_value=self.config), \
                 patch.object(runtime, "load_state", return_value=self.state), \
                 patch.object(cdp, "evaluate", return_value=invalid), \
                 patch.object(cdp, "target_call") as calls, \
                 patch.object(cdp, "click_send") as gesture, \
                 patch.object(cdp, "insert_text") as insert:
                with self.assertRaises(BrowserPreDispatchBlocked):
                    runtime.send_message(
                        self.target, "DO_NOT_SEND", background_focus=True,
                        before_dispatch=Mock(),
                    )
                self.assertEqual(
                    [entry.args[2]["enabled"] for entry in calls.call_args_list],
                    [True, False],
                )
                gesture.assert_not_called()
                insert.assert_not_called()

    def test_after_single_keyboard_gesture_failure_remains_uncertain_and_restores_focus(self):
        calls = []
        responses = [
            {"focused": False, "visibility": "hidden",
             "url": "https://chatgpt.com/c/background-test",
             "ready": "complete", "editor_visible": True},
            "ready", True,
        ]
        with patch.object(runtime, "load_config", return_value=self.config), \
             patch.object(runtime, "load_state", return_value=self.state), \
             patch.object(runtime, "_assistant_snapshot", return_value={"busy": False}), \
             patch.object(runtime, "_context_limit_warning", return_value=""), \
             patch.object(cdp, "evaluate", side_effect=responses), \
             patch.object(cdp, "target_call", side_effect=lambda t, op, arg, **kw: calls.append((op, arg))), \
             patch.object(cdp, "insert_text"), \
             patch.object(cdp, "press_enter", side_effect=cdp.CdpTimeoutError("gesture lost")) as enter, \
             patch.object(cdp, "click_send") as click:
            with self.assertRaises(BrowserSubmissionUncertain):
                runtime.send_message(
                    self.target, "DO_NOT_REPLAY", background_focus=True,
                    wait_for_response=False, before_dispatch=lambda: None,
                )
        enter.assert_called_once()
        click.assert_not_called()
        self.assertEqual(
            [event[1]["enabled"] for event in calls],
            [True, False],
        )

    def test_unapproved_direct_background_send_still_fails_without_focus_effect(self):
        with patch.object(runtime, "load_config", return_value=self.config), \
             patch.object(runtime, "load_state", return_value=self.state), \
             patch.object(cdp, "target_call") as calls, \
             patch.object(cdp, "click_send") as click:
            with self.assertRaises(BrowserPreDispatchBlocked):
                runtime.send_message(self.target, "NO_GRANT")
        calls.assert_not_called()
        click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
