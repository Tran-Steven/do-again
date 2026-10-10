"""Regular ChatGPT path works without CDP, Codex usage or model API calls."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from do_again.chatgpt_handoff import prepare, observe, _github_slug
from do_again.core.schema import request_fingerprint
from do_again.service.runtime import ServiceError


class ChatGPTManualHandoffTests(unittest.TestCase):
    def setUp(self):
        self.slug=patch("do_again.chatgpt_handoff._github_slug",
                        return_value="Tran-Steven/do-again")
        self.slug_mock=self.slug.start()
        self.addCleanup(self.slug.stop)
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        control=root/"control";control.mkdir()
        self.layout=SimpleNamespace(repo=root/"project",branch="operator-control",
            remote="origin",state_dir=root/"state",control_worktree=control)
        self.nonce="a"*24
        self.prepared=prepare(self.layout,nonce=self.nonce)
        self.rid=self.prepared["request_id"]
        self.request=json.loads((self.layout.state_dir/"chatgpt_handoffs"/(self.rid+".json")).read_text())["request"]
        self.sha="b"*40
        self.receipt={
            "schema_version":1,"request_id":self.rid,
            "operation":"status","request_fingerprint":request_fingerprint(self.request),
            "state":"succeeded","result":{
                "operation":"status","request_fingerprint":request_fingerprint(self.request),
                "result":{"operator_intent":"maintenance"}
            }
        }

    def runner(self, *, request=None,receipt=None):
        request=self.request if request is None else request
        receipt=self.receipt if receipt is None else receipt
        def run(argv):
            self.assertEqual(argv[0:3],["git","-C",str(self.layout.control_worktree)])
            args=argv[3:]
            if args[0]=="fetch": out=""
            elif args[0]=="rev-parse":out=self.sha
            elif args[0]=="ls-remote":out=self.sha+"\trefs/heads/operator-control"
            elif args[0]=="show":
                spec=args[1]
                if "/requests/" in spec:out=json.dumps(request) if request is not False else ""
                elif "/receipts/" in spec:out=json.dumps(receipt) if receipt is not False else ""
                else:raise AssertionError(spec)
                if out=="":
                    return SimpleNamespace(returncode=1,stdout="",stderr="not found")
            else:raise AssertionError(args)
            return SimpleNamespace(returncode=0,stdout=out+"\n",stderr="")
        return run

    def test_prepares_exact_single_status_request_without_browser(self):
        self.assertEqual(self.prepared["transport"],"regular_chatgpt")
        self.assertFalse(self.prepared["submitted"])
        self.assertIn("regular-ChatGPT verification",self.prepared["prompt"])
        self.assertEqual(self.request["operation"],"status")
        self.assertEqual(self.request["args"],{})
        self.assertIn(self.rid,self.prepared["prompt"])
        self.assertEqual(self.prepared["request_fingerprint"],request_fingerprint(self.request))
        with self.assertRaises(FileExistsError):
            prepare(self.layout,nonce=self.nonce)

    def test_remote_matching_receipt_is_not_claimed_as_native_execution(self):
        result=observe(self.layout,self.rid,runner=self.runner())
        self.assertTrue(result["completed"])
        self.assertEqual(result["state"],"receipt_verified")
        self.assertEqual(result["verification"],"git_receipt_only")
        self.assertEqual(result["remote_head"],self.sha)

    def test_never_accept_different_remote_request(self):
        wrong={**self.request,"request_id":"different-request"}
        with self.assertRaisesRegex(ServiceError,"differs from original"):
            observe(self.layout,self.rid,runner=self.runner(request=wrong))

    def test_never_accept_unbound_or_success_claim_receipt(self):
        broken={**self.receipt,"request_fingerprint":"0"*64}
        with self.assertRaisesRegex(ServiceError,"not bound"):
            observe(self.layout,self.rid,runner=self.runner(receipt=broken))
        forged={**self.receipt,"result":{"unrelated":"success"}}
        with self.assertRaisesRegex(ServiceError,"matching executed"):
            observe(self.layout,self.rid,runner=self.runner(receipt=forged))

    def test_missing_request_or_receipt_is_not_success(self):
        missing=observe(self.layout,self.rid,runner=self.runner(request=False))
        self.assertEqual(missing["state"],"request_not_published")
        self.assertFalse(missing["completed"])
        awaiting=observe(self.layout,self.rid,runner=self.runner(receipt=False))
        self.assertEqual(awaiting["state"],"awaiting_worker_receipt")
        self.assertFalse(awaiting["completed"])

    def test_failed_receipt_is_not_success(self):
        failed={**self.receipt,"state":"failed"}
        row=observe(self.layout,self.rid,runner=self.runner(receipt=failed))
        self.assertEqual(row["state"],"worker_failed")
        self.assertFalse(row["completed"])

    def test_github_remote_url_formats_are_strict(self):
        self.slug.stop()
        for remote in ("https://github.com/Tran-Steven/do-again.git",
                       "git@github.com:Tran-Steven/do-again.git",
                       "ssh://git@github.com/Tran-Steven/do-again"):
            with self.subTest(remote=remote):
                with patch("do_again.chatgpt_handoff.subprocess.run",
                           return_value=SimpleNamespace(returncode=0,stdout=remote,stderr="")):
                    self.assertEqual(_github_slug(self.layout),"Tran-Steven/do-again")
        with patch("do_again.chatgpt_handoff.subprocess.run",
                   return_value=SimpleNamespace(returncode=0,
                       stdout="https://evil.example/owner/repo.git",stderr="")):
            with self.assertRaisesRegex(ServiceError,"requires a GitHub repository"):
                _github_slug(self.layout)
        self.slug_mock=self.slug.start()

    def test_bad_private_state_and_invalid_ids_fail_closed(self):
        with self.assertRaises(ServiceError):
            observe(self.layout,"other-verify-id",runner=self.runner())
        path=self.layout.state_dir/"chatgpt_handoffs"/(self.rid+".json")
        record=json.loads(path.read_text())
        record["control_branch"]="main"
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ServiceError,"identity changed"):
            observe(self.layout,self.rid,runner=self.runner())

    def test_chatgpt_prompt_names_actual_github_slug_not_private_mac_path(self):
        self.assertIn("Git repository Tran-Steven/do-again",self.prepared["prompt"])
        self.assertNotIn(str(self.layout.repo),self.prepared["prompt"])
        self.slug_mock.return_value="AnotherOwner/do-again"
        with self.assertRaisesRegex(ServiceError,"identity changed"):
            observe(self.layout,self.rid,runner=self.runner())

    def test_preparation_rejects_main_branch(self):
        self.layout.branch="main"
        with self.assertRaisesRegex(ServiceError,"separate control branch"):
            prepare(self.layout,nonce="c"*24)


if __name__=="__main__":unittest.main()
