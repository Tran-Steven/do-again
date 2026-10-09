from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from do_again.service import liveness


class CIWaitMetadataValidationTests(unittest.TestCase):
    def setUp(self):
        self.valid = {
            "repository": "Tran-Steven/do-again",
            "run_id": 37716899421,
            "head_sha": "A" * 40,
        }

    def test_invalid_ci_metadata_never_reaches_gh(self):
        invalid = [
            {"repository": "Tran-Steven/do-again/../../other"},
            {"repository": "../other"},
            {"repository": "./.."},
            {"repository": "Tran-Steven//do-again"},
            {"repository": "Tran-Steven/do-again?per_page=1"},
            {"repository": "Tran-Steven do-again"},
            {"repository": 123},
            {"repository": ""},
            {"run_id": True},
            {"run_id": 0},
            {"run_id": -3},
            {"run_id": "37716899421"},
            {"head_sha": "deadbeef"},
            {"head_sha": "g" * 40},
            {"head_sha": None},
            {"head_sha": 123},
        ]
        with patch.object(liveness.subprocess, "run") as run:
            for change in invalid:
                with self.subTest(change=change):
                    result = liveness._github_actions_run(self.valid | change)
                    self.assertEqual(result["state"], "invalid")
            run.assert_not_called()

    def test_valid_metadata_uses_exact_run_and_normalized_head(self):
        proc = Mock(
            returncode=0,
            stdout=json.dumps({
                "status": "completed",
                "conclusion": "success",
                "head_sha": "a" * 40,
            }),
        )
        with patch.object(liveness.subprocess, "run", return_value=proc) as run:
            result = liveness._github_actions_run(self.valid)
        self.assertEqual(result["state"], "terminal")
        self.assertEqual(result["conclusion"], "success")
        self.assertEqual(result["head_sha"], "a" * 40)
        args = run.call_args.args[0]
        self.assertEqual(args[0:3], ["gh", "api", "repos/Tran-Steven/do-again/actions/runs/37716899421"])

    def test_non_object_api_payload_fails_closed(self):
        for payload in ("[]", "null", "true", '"completed"', "42"):
            with self.subTest(payload=payload):
                with patch.object(
                    liveness.subprocess,
                    "run",
                    return_value=Mock(returncode=0, stdout=payload),
                ):
                    result = liveness._github_actions_run(self.valid)
                self.assertEqual(result, {
                    "state": "unavailable",
                    "error": "github_actions_invalid_response",
                })


if __name__ == "__main__":
    unittest.main()
