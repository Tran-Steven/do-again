from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again.browser import runtime


class BlankChatSerializationTests(unittest.TestCase):
    def test_new_chat_and_rollover_use_one_shared_exclusion_guard(self):
        self.assertIsNot(runtime._bootstrap_new_chat,
                         runtime._bootstrap_new_chat.__wrapped__)
        self.assertIsNot(runtime._rollover_project_chat,
                         runtime._rollover_project_chat.__wrapped__)
        self.assertEqual(
            runtime._bootstrap_new_chat.__wrapped__.__name__,
            "_bootstrap_new_chat",
        )
        self.assertEqual(
            runtime._rollover_project_chat.__wrapped__.__name__,
            "_rollover_project_chat",
        )

    def test_two_workers_cannot_bootstrap_blank_chats_concurrently(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                os.environ, {"DO_AGAIN_HOME": str(Path(tmp) / "state")}):
            first_entered=threading.Event()
            release_first=threading.Event()
            second_entered=threading.Event()
            results=[]
            errors=[]

            @runtime._exclusive_blank_chat
            def work(name):
                if name=="first":
                    first_entered.set()
                    if not release_first.wait(5):
                        raise AssertionError("timed out releasing first worker")
                else:
                    second_entered.set()
                results.append(name)

            def run(name):
                try:
                    work(name)
                except Exception as exc:
                    errors.append(exc)

            first=threading.Thread(target=run,args=("first",),daemon=True)
            second=threading.Thread(target=run,args=("second",),daemon=True)
            first.start()
            self.assertTrue(first_entered.wait(4))
            second.start()
            self.assertFalse(second_entered.wait(0.15))
            release_first.set()
            first.join(5)
            second.join(5)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors,[])
            self.assertEqual(results,["first","second"])
            self.assertTrue(second_entered.is_set())
            self.assertFalse(
                (Path(tmp)/"state/browser/blank-chat-bootstrap.lock").exists(),
            )

    def test_send_lock_serializes_two_independent_project_workers(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                os.environ, {"DO_AGAIN_HOME": str(Path(tmp) / "state")}):
            first_started=threading.Event()
            allow_first=threading.Event()
            second_started=threading.Event()
            order=[]
            failures=[]

            @runtime._exclusive_message_send
            def send(project):
                if project=="do-again":
                    first_started.set()
                    if not allow_first.wait(5):
                        raise AssertionError("first project timed out")
                else:
                    second_started.set()
                order.append(project)

            def start(project):
                try:
                    send(project)
                except Exception as exc:
                    failures.append(exc)

            a=threading.Thread(target=start,args=("do-again",),daemon=True)
            b=threading.Thread(target=start,args=("jobpipe",),daemon=True)
            a.start()
            self.assertTrue(first_started.wait(4))
            b.start()
            self.assertFalse(second_started.wait(0.15))
            allow_first.set()
            a.join(5)
            b.join(5)
            self.assertEqual(failures,[])
            self.assertEqual(order,["do-again","jobpipe"])
            self.assertFalse((Path(tmp)/"state/browser/chatgpt-send.lock").exists())

    def test_failure_releases_shared_blank_chat_lock(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                os.environ, {"DO_AGAIN_HOME": str(Path(tmp) / "state")}):
            @runtime._exclusive_blank_chat
            def fail():
                raise RuntimeError("synthetic pre-Send failure")

            @runtime._exclusive_blank_chat
            def succeed():
                return "next-worker-admitted"

            with self.assertRaisesRegex(RuntimeError,"pre-Send"):
                fail()
            self.assertEqual(succeed(),"next-worker-admitted")


if __name__=="__main__":
    unittest.main()
