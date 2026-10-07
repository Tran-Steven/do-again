from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again.browser import cdp
from do_again.browser.runtime import (
    BrowserAuthRequired,
    BrowserError,
    _browser_command,
    _rollover_project_chat,
    activate_project,
    active_projects,
    browser_paths,
    browser_status,
    deactivate_project,
    ensure_browser_running,
    ensure_project_chat,
    load_config,
    notify_receipt,
    project_record,
    register_project,
    save_config,
    setup_browser,
    stop_if_unused,
    wait_for_authenticated,
)


class BrowserRuntimeTests(unittest.TestCase):
    def test_headless_command_uses_dedicated_profile_and_cdp(self) -> None:
        binary = Path("browser-bin")
        profile = Path("profile-dir")
        argv = _browser_command(
            binary,
            profile,
            9333,
            "headless",
        )
        self.assertIn("--headless=new", argv)
        self.assertIn("--remote-debugging-port=9333", argv)
        self.assertIn("--remote-debugging-address=127.0.0.1", argv)
        self.assertIn(f"--user-data-dir={profile}", argv)
        self.assertIn("--remote-allow-origins=http://127.0.0.1:9333", argv)
        self.assertNotIn("--no-startup-window", argv)

    def test_background_command_does_not_force_true_headless(self) -> None:
        argv = _browser_command(
            Path("browser-bin"),
            Path("profile-dir"),
            9223,
            "background",
        )
        self.assertIn("--no-startup-window", argv)
        self.assertIn("--start-minimized", argv)
        self.assertNotIn("--headless=new", argv)

    def test_browser_paths_are_isolated_from_normal_browser_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"DO_AGAIN_HOME": tmp}):
                paths = browser_paths()
                self.assertEqual(paths.profile, Path(tmp).resolve() / "browser/profile")
                self.assertNotIn("Application Support/Google/Chrome", str(paths.profile))

    def test_setup_auto_prefers_verified_headless(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_binary = Path(tmp) / "chrome"
            fake_binary.write_text("", encoding="utf-8")
            fake_target = cdp.Target("target", "https://chatgpt.com/", "", "ws://127.0.0.1/x")
            launches: list[str] = []

            def fake_launch(mode, **kwargs):
                launches.append(mode)
                return {"pid": 100 + len(launches), "port": 9223, "mode": mode}

            with (
                patch.dict(os.environ, {"DO_AGAIN_HOME": tmp}),
                patch("do_again.browser.runtime.discover_browser", return_value=fake_binary),
                patch("do_again.browser.runtime.stop_browser"),
                patch("do_again.browser.runtime.launch_browser", side_effect=fake_launch),
                patch(
                    "do_again.browser.runtime.wait_for_authenticated",
                    return_value=(fake_target, {"prompt": True, "url": "https://chatgpt.com/"}),
                ),
                patch(
                    "do_again.browser.runtime.browser_status",
                    return_value={
                        "running": True,
                        "authenticated": True,
                        "mode": "headless",
                        "session_ready": True,
                    },
                ),
            ):
                value = setup_browser(mode="auto", run_iteration_test=False)

            self.assertEqual(launches, ["visible", "headless", "headless"])
            self.assertEqual(value["resolved_mode"], "headless")
            with patch.dict(os.environ, {"DO_AGAIN_HOME": tmp}):
                saved = load_config()
            self.assertEqual(saved["resolved_mode"], "headless")
            self.assertTrue(saved["authenticated"])

    def test_setup_auto_falls_back_to_background_when_headless_auth_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_binary = Path(tmp) / "chrome"
            fake_binary.write_text("", encoding="utf-8")
            fake_target = cdp.Target("target", "https://chatgpt.com/", "", "ws://127.0.0.1/x")
            launches: list[str] = []
            auth_calls = 0

            def fake_launch(mode, **kwargs):
                launches.append(mode)
                return {"pid": 100 + len(launches), "port": 9223, "mode": mode}

            def fake_auth(*args, **kwargs):
                nonlocal auth_calls
                auth_calls += 1
                if auth_calls == 2:
                    raise BrowserAuthRequired("headless session rejected")
                return fake_target, {"prompt": True, "url": "https://chatgpt.com/"}

            with (
                patch.dict(os.environ, {"DO_AGAIN_HOME": tmp}),
                patch("do_again.browser.runtime.discover_browser", return_value=fake_binary),
                patch("do_again.browser.runtime.stop_browser"),
                patch("do_again.browser.runtime.launch_browser", side_effect=fake_launch),
                patch("do_again.browser.runtime.wait_for_authenticated", side_effect=fake_auth),
                patch(
                    "do_again.browser.runtime.browser_status",
                    return_value={
                        "running": True,
                        "authenticated": True,
                        "mode": "background",
                        "session_ready": True,
                    },
                ),
            ):
                value = setup_browser(mode="auto", run_iteration_test=False)

            self.assertEqual(launches, ["visible", "headless", "background"])
            self.assertEqual(value["resolved_mode"], "background")
            with patch.dict(os.environ, {"DO_AGAIN_HOME": tmp}):
                saved = load_config()
            self.assertEqual(saved["resolved_mode"], "background")

    def test_existing_project_chat_is_reused_without_new_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            fake_target = cdp.Target(
                "target",
                "https://chatgpt.com/c/existing",
                "",
                "ws://127.0.0.1/x",
            )
            with patch.dict(os.environ, {"DO_AGAIN_HOME": str(Path(tmp) / "home")}):
                register_project(
                    repo,
                    remote_url="https://github.com/example/repo.git",
                    control_branch="operator-control",
                    chat_url="https://chatgpt.com/c/existing",
                )
                with (
                    patch(
                        "do_again.browser.runtime.ensure_browser_running",
                        return_value={"port": 9223},
                    ),
                    patch(
                        "do_again.browser.runtime._find_chatgpt_target",
                        return_value=fake_target,
                    ),
                    patch(
                        "do_again.browser.runtime.wait_for_authenticated",
                        return_value=(fake_target, {"prompt": True}),
                    ),
                    patch("do_again.browser.runtime.send_message") as send_mock,
                ):
                    value = ensure_project_chat(
                        repo,
                        remote_url="https://github.com/example/repo.git",
                        control_branch="operator-control",
                    )

            self.assertEqual(value["chat_url"], "https://chatgpt.com/c/existing")
            send_mock.assert_not_called()

    def test_project_bootstrap_creates_one_bound_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            fake_target = cdp.Target(
                "target",
                "https://chatgpt.com/",
                "",
                "ws://127.0.0.1/x",
            )
            with (
                patch.dict(os.environ, {"DO_AGAIN_HOME": str(Path(tmp) / "home")}),
                patch(
                    "do_again.browser.runtime.ensure_browser_running",
                    return_value={"port": 9223},
                ),
                patch("do_again.browser.runtime._find_chatgpt_target", return_value=None),
                patch("do_again.browser.runtime.cdp.create_target", return_value=fake_target),
                patch(
                    "do_again.browser.runtime.wait_for_authenticated",
                    return_value=(fake_target, {"prompt": True}),
                ),
                patch(
                    "do_again.browser.runtime.send_message",
                    return_value={
                        "response": "DO_AGAIN_PROJECT_READY",
                        "chat_url": "https://chatgpt.com/c/new-project",
                    },
                ) as send_mock,
            ):
                value = ensure_project_chat(
                    repo,
                    remote_url="https://github.com/example/repo.git",
                    control_branch="operator-control",
                )
                saved = project_record(repo)

            self.assertEqual(value["chat_url"], "https://chatgpt.com/c/new-project")
            self.assertEqual(saved["chat_url"], "https://chatgpt.com/c/new-project")
            send_mock.assert_called_once()

    def test_notify_receipt_reuses_bound_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            fake_target = cdp.Target(
                "target",
                "https://chatgpt.com/c/project",
                "",
                "ws://127.0.0.1/x",
            )
            with patch.dict(os.environ, {"DO_AGAIN_HOME": str(Path(tmp) / "home")}):
                register_project(
                    repo,
                    control_branch="operator-control",
                    chat_url="https://chatgpt.com/c/project",
                )
                with (
                    patch(
                        "do_again.browser.runtime.ensure_browser_running",
                        return_value={"port": 9223},
                    ),
                    patch(
                        "do_again.browser.runtime._find_chatgpt_target",
                        return_value=fake_target,
                    ),
                    patch(
                        "do_again.browser.runtime.wait_for_authenticated",
                        return_value=(fake_target, {"prompt": True}),
                    ),
                    patch("do_again.browser.runtime._page_contains", return_value=False),
                    patch("do_again.browser.runtime._assistant_snapshot", return_value={"busy": False}),
                    patch("do_again.browser.runtime._context_limit_warning", return_value=""),
                    patch(
                        "do_again.browser.runtime.send_message",
                        return_value={
                            "response": "continuing",
                            "chat_url": "https://chatgpt.com/c/project",
                        },
                    ) as send_mock,
                ):
                    notify_receipt(
                        repo,
                        {"request_id": "req-1", "state": "succeeded"},
                    )

            sent = send_mock.call_args.args[1]
            self.assertIn("DO_AGAIN_RECEIPT_READY", sent)
            self.assertIn("req-1", sent)



    def test_first_setup_auto_waits_for_login_then_switches_headless(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_binary = Path(tmp) / "chrome"
            fake_binary.write_text("", encoding="utf-8")
            fake_target = cdp.Target(
                "target", "https://chatgpt.com/", "", "ws://127.0.0.1/x"
            )
            launches: list[str] = []

            def fake_launch(mode, **kwargs):
                launches.append(mode)
                return {"pid": 100 + len(launches), "port": 9223, "mode": mode}

            auth_results = [
                BrowserAuthRequired("login required"),
                (fake_target, {"prompt": True, "url": "https://chatgpt.com/"}),
                (fake_target, {"prompt": True, "url": "https://chatgpt.com/"}),
                (fake_target, {"prompt": True, "url": "https://chatgpt.com/"}),
            ]

            with (
                patch.dict(os.environ, {"DO_AGAIN_HOME": tmp}),
                patch("do_again.browser.runtime.discover_browser", return_value=fake_binary),
                patch("do_again.browser.runtime.stop_browser"),
                patch("do_again.browser.runtime.launch_browser", side_effect=fake_launch),
                patch("do_again.browser.runtime.wait_for_authenticated", side_effect=auth_results),
                patch(
                    "do_again.browser.runtime.browser_status",
                    return_value={
                        "running": True,
                        "authenticated": True,
                        "mode": "headless",
                        "session_ready": True,
                    },
                ),
            ):
                value = setup_browser(
                    mode="auto",
                    run_iteration_test=False,
                )

            self.assertEqual(launches, ["visible", "headless", "headless"])
            self.assertEqual(value["resolved_mode"], "headless")


    def test_closed_login_browser_fails_immediately(self) -> None:
        target = cdp.Target(
            "target",
            "https://chatgpt.com/",
            "",
            "ws://127.0.0.1/x",
        )
        with (
            patch(
                "do_again.browser.runtime._ensure_chatgpt_target",
                return_value=target,
            ),
            patch(
                "do_again.browser.runtime.cdp.targets",
                side_effect=cdp.CdpError("closed"),
            ),
            patch(
                "do_again.browser.runtime.cdp.endpoint_ready",
                return_value=False,
            ),
        ):
            with self.assertRaises(BrowserError) as error:
                wait_for_authenticated(9223, timeout=30.0)
        self.assertIn("closed before ChatGPT authentication completed", str(error.exception))

    def test_auth_expiry_is_reported_in_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"DO_AGAIN_HOME": tmp}):
                config = load_config()
                config["authenticated"] = True
                save_config(config)
                from do_again.browser.runtime import save_state
                save_state({"pid": 1234, "port": 9223, "mode": "headless"})
                with (
                    patch("do_again.browser.runtime._pid_alive", return_value=True),
                    patch("do_again.browser.runtime.cdp.endpoint_ready", return_value=True),
                    patch("do_again.browser.runtime._owns_endpoint", return_value=True),
                    patch(
                        "do_again.browser.runtime.wait_for_authenticated",
                        side_effect=BrowserAuthRequired("sign in again"),
                    ),
                ):
                    value = browser_status(verify_session=True)
            self.assertTrue(value["running"])
            self.assertFalse(value["session_ready"])
            self.assertTrue(value["auth_required"])
            self.assertIn("sign in again", value["error"])

    def test_multiple_projects_share_browser_until_last_project_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_a = Path(tmp) / "a"
            repo_b = Path(tmp) / "b"
            repo_a.mkdir()
            repo_b.mkdir()
            with (
                patch.dict(os.environ, {"DO_AGAIN_HOME": str(Path(tmp) / "home")}),
                patch("do_again.browser.runtime.stop_browser") as stop_mock,
            ):
                activate_project(repo_a, daemon_pid=os.getpid())
                activate_project(repo_b, daemon_pid=os.getpid())
                self.assertEqual(len(active_projects()), 2)
                deactivate_project(repo_a)
                self.assertFalse(stop_if_unused())
                stop_mock.assert_not_called()
                deactivate_project(repo_b)
                self.assertTrue(stop_if_unused())
                stop_mock.assert_called_once_with(force=True)

    def test_rollover_persists_new_chat_and_closes_old_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            old_target = cdp.Target(
                "old", "https://chatgpt.com/c/old", "", "ws://127.0.0.1/old"
            )
            new_target = cdp.Target(
                "new", "https://chatgpt.com/", "", "ws://127.0.0.1/new"
            )
            with patch.dict(os.environ, {"DO_AGAIN_HOME": str(Path(tmp) / "home")}):
                record = register_project(
                    repo,
                    remote_url="https://github.com/example/repo.git",
                    control_branch="operator-control",
                    chat_url="https://chatgpt.com/c/old",
                )
                with (
                    patch("do_again.browser.runtime.cdp.create_target", return_value=new_target),
                    patch(
                        "do_again.browser.runtime.wait_for_authenticated",
                        return_value=(new_target, {"prompt": True}),
                    ),
                    patch(
                        "do_again.browser.runtime.send_message",
                        return_value={
                            "response": "DO_AGAIN_PROJECT_READY",
                            "chat_url": "https://chatgpt.com/c/new",
                        },
                    ),
                    patch("do_again.browser.runtime.cdp.close_target") as close_mock,
                ):
                    target, updated = _rollover_project_chat(
                        repo, record, port=9223, old_target=old_target
                    )
                saved = project_record(repo)

            self.assertEqual(target, new_target)
            self.assertEqual(updated["chat_url"], "https://chatgpt.com/c/new")
            self.assertEqual(saved["previous_chat_url"], "https://chatgpt.com/c/old")
            self.assertEqual(saved["rollover_count"], 1)
            close_mock.assert_called_once_with(9223, "old")

    def test_runtime_headless_failure_falls_back_to_background(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_target = cdp.Target(
                "target", "https://chatgpt.com/", "", "ws://127.0.0.1/x"
            )
            with patch.dict(os.environ, {"DO_AGAIN_HOME": tmp}):
                config = load_config()
                config["preferred_mode"] = "auto"
                config["resolved_mode"] = "headless"
                config["authenticated"] = True
                save_config(config)
                statuses = [
                    {"running": True, "port": 9223, "mode": "headless"},
                    {"running": True, "port": 9223, "mode": "headless"},
                    {"running": True, "port": 9223, "mode": "background"},
                ]
                with (
                    patch("do_again.browser.runtime.browser_status", side_effect=statuses),
                    patch("do_again.browser.runtime.stop_browser") as stop_mock,
                    patch(
                        "do_again.browser.runtime.launch_browser",
                        return_value={"pid": 22, "port": 9223, "mode": "background"},
                    ) as launch_mock,
                    patch(
                        "do_again.browser.runtime.wait_for_authenticated",
                        side_effect=[
                            BrowserAuthRequired("headless rejected"),
                            (fake_target, {"url": "https://chatgpt.com/"}),
                        ],
                    ),
                ):
                    value = ensure_browser_running(verify_auth=True)

                self.assertEqual(value["mode"], "background")
                self.assertTrue(value["authenticated"])
                self.assertEqual(load_config()["resolved_mode"], "background")
                stop_mock.assert_called_once_with(force=True)
                launch_mock.assert_called_once()

    def test_duplicate_receipt_marker_is_not_sent_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            target = cdp.Target(
                "target",
                "https://chatgpt.com/c/project",
                "",
                "ws://127.0.0.1/x",
            )
            with patch.dict(os.environ, {"DO_AGAIN_HOME": str(Path(tmp) / "home")}):
                register_project(
                    repo,
                    remote_url="https://github.com/example/repo.git",
                    control_branch="operator-control",
                    chat_url="https://chatgpt.com/c/project",
                )
                with (
                    patch("do_again.browser.runtime.ensure_browser_running", return_value={"port": 9223}),
                    patch("do_again.browser.runtime._find_chatgpt_target", return_value=target),
                    patch("do_again.browser.runtime.wait_for_authenticated", return_value=(target, {"prompt": True})),
                    patch("do_again.browser.runtime._page_contains", return_value=True),
                    patch("do_again.browser.runtime._assistant_snapshot", return_value={"busy": False}),
                    patch("do_again.browser.runtime._context_limit_warning", return_value=""),
                    patch("do_again.browser.runtime.send_message") as send_mock,
                ):
                    value = notify_receipt(repo, {"request_id": "req-dup", "state": "succeeded"})
            self.assertEqual(value["response"], "already_delivered")
            send_mock.assert_not_called()

    def test_context_limit_rolls_project_to_new_chat_before_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            old_target = cdp.Target(
                "old",
                "https://chatgpt.com/c/old",
                "",
                "ws://127.0.0.1/old",
            )
            new_target = cdp.Target(
                "new",
                "https://chatgpt.com/c/new",
                "",
                "ws://127.0.0.1/new",
            )
            with patch.dict(os.environ, {"DO_AGAIN_HOME": str(Path(tmp) / "home")}):
                record = register_project(
                    repo,
                    remote_url="https://github.com/example/repo.git",
                    control_branch="operator-control",
                    chat_url="https://chatgpt.com/c/old",
                )
                new_record = dict(record, chat_url="https://chatgpt.com/c/new")
                with (
                    patch("do_again.browser.runtime.ensure_browser_running", return_value={"port": 9223}),
                    patch("do_again.browser.runtime._find_chatgpt_target", return_value=old_target),
                    patch("do_again.browser.runtime.wait_for_authenticated", side_effect=lambda *args, **kwargs: (kwargs["target"], {"prompt": True})),
                    patch("do_again.browser.runtime._page_contains", return_value=False),
                    patch("do_again.browser.runtime._assistant_snapshot", return_value={"busy": False}),
                    patch("do_again.browser.runtime._context_limit_warning", return_value="maximum length"),
                    patch("do_again.browser.runtime._assistant_snapshot", return_value={"busy": False}),
                    patch(
                        "do_again.browser.runtime._rollover_project_chat",
                        return_value=(new_target, new_record),
                    ) as rollover_mock,
                    patch(
                        "do_again.browser.runtime.send_message",
                        return_value={
                            "response": "continuing",
                            "chat_url": "https://chatgpt.com/c/new",
                        },
                    ) as send_mock,
                ):
                    value = notify_receipt(repo, {"request_id": "req-roll", "state": "succeeded"})
            rollover_mock.assert_called_once()
            self.assertEqual(send_mock.call_args.args[0], new_target)
            self.assertEqual(value["chat_url"], "https://chatgpt.com/c/new")

    def test_stale_project_lease_is_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            with patch.dict(os.environ, {"DO_AGAIN_HOME": str(Path(tmp) / "home")}):
                value = register_project(repo)
                path = browser_paths().projects / f"{value['key']}.json"
                value["active"] = True
                value["daemon_pid"] = 999999999
                path.write_text(json.dumps(value), encoding="utf-8")
                with patch("do_again.browser.runtime._pid_alive", return_value=False):
                    self.assertEqual(active_projects(), [])
                saved = json.loads(path.read_text(encoding="utf-8"))
                self.assertFalse(saved["active"])


if __name__ == "__main__":
    unittest.main()
