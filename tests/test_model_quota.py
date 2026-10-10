"""Offline tests for the zero-inference Codex quota admission gate."""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch
from pathlib import Path

from do_again.model_quota import (
    CodexQuotaUnavailable, summarize_rate_limits, require_model_capacity,
)


class CodexQuotaTests(unittest.TestCase):
    def setUp(self):
        self.original = {
            "id": 2,
            "result": {
                "accountId": "private-account-identifier",
                "ordinaryUsageAllowed": False,
                "rateLimits": {
                    "limitId": "codex",
                    "rateLimitReachedType": "rate_limit_reached",
                    "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
                    "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1791672414},
                    "secondary": {"usedPercent": 100, "windowDurationMins": 10080,
                                  "resetsAt": 1792076094},
                    "spendControlReached": False,
                },
                "rateLimitResetCredits": {"availableCount": 2},
            },
        }

    def test_real_shaped_weekly_quota_blocks_without_spending_reset(self):
        parsed = summarize_rate_limits(self.original)
        self.assertIs(parsed["allowed"], False)
        self.assertEqual(parsed["reason"], "account_rejected_included_usage")
        self.assertEqual(parsed["secondary"]["usedPercent"], 100)
        self.assertEqual(parsed["primary"]["usedPercent"], 0)
        self.assertEqual(parsed["reset_credits_available"], 2)
        self.assertIs(parsed["has_credits"], False)
        self.assertEqual(parsed["resets_at_utc"], "2026-10-15T14:54:54+00:00")
        self.assertNotIn("accountId", str(parsed))
        self.assertNotIn("private-account-identifier", str(parsed))
        self.assertNotIn("balance", parsed)

    def test_observed_backend_permission_wins_over_inferred_percentage(self):
        copy_value = copy.deepcopy(self.original)
        copy_value["result"]["ordinaryUsageAllowed"] = True
        self.assertTrue(summarize_rate_limits(copy_value)["allowed"])
        copy_value["result"]["ordinaryUsageAllowed"] = False
        copy_value["result"]["rateLimits"]["secondary"]["usedPercent"] = 0
        self.assertFalse(summarize_rate_limits(copy_value)["allowed"])

    def test_unknown_backend_and_empty_limits_fail_closed(self):
        for source in (None, {}, {"error": {"code": -32601}}, {"result": {}}):
            with self.subTest(value=source), self.assertRaises(CodexQuotaUnavailable):
                summarize_rate_limits(source)
        copy_value = copy.deepcopy(self.original)
        copy_value["result"]["ordinaryUsageAllowed"] = None
        copy_value["result"]["rateLimits"]["rateLimitReachedType"] = None
        copy_value["result"]["rateLimits"]["secondary"]["usedPercent"] = 50
        self.assertIsNone(summarize_rate_limits(copy_value)["allowed"])

    def test_unavailable_remaining_window_respects_exhaustion(self):
        row = copy.deepcopy(self.original)
        row["result"]["ordinaryUsageAllowed"] = None
        row["result"]["rateLimits"]["rateLimitReachedType"] = None
        row["result"]["rateLimits"]["primary"]["usedPercent"] = 100
        parsed = summarize_rate_limits(row)
        self.assertFalse(parsed["allowed"])
        self.assertEqual(parsed["resets_at_utc"], "2026-10-15T14:54:54+00:00")

    def test_capacity_guard_rejects_weekly_quota_and_no_model_call(self):
        original = summarize_rate_limits(self.original)
        with patch("do_again.model_quota.query_codex_rate_limits", return_value=original) as rpc:
            with self.assertRaisesRegex(CodexQuotaUnavailable, "banked_resets=2") as raised:
                require_model_capacity(Path("/isolated/codex"), Path("/isolated"))
        rpc.assert_called_once()
        self.assertIn("2026-10-15", str(raised.exception))
        self.assertNotIn("private-account-identifier", str(raised.exception))

    def test_capacity_guard_does_not_consume_or_claim_usage(self):
        allowed = summarize_rate_limits(self.original)
        allowed["allowed"] = True
        with patch("do_again.model_quota.query_codex_rate_limits", return_value=allowed):
            self.assertIs(require_model_capacity(Path("/codex"), Path("/operator")), allowed)

    def test_malformed_reset_credit_count_never_inferred(self):
        row = copy.deepcopy(self.original)
        row["result"]["rateLimitResetCredits"]["availableCount"] = True
        self.assertIsNone(summarize_rate_limits(row)["reset_credits_available"])
        row["result"]["rateLimitResetCredits"]["availableCount"] = -4
        self.assertIsNone(summarize_rate_limits(row)["reset_credits_available"])


if __name__ == "__main__":
    unittest.main()
