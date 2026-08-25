"""Static regression contract for the renewal reminder edge function."""

from __future__ import annotations

import unittest
from pathlib import Path


SOURCE = (
    Path(__file__).resolve().parent.parent
    / "supabase"
    / "functions"
    / "subscription-renewal-reminder"
    / "index.ts"
).read_text(encoding="utf-8")


class TributeAutoRenewContract(unittest.TestCase):
    def test_candidate_query_loads_cancellation_state(self):
        self.assertIn("provider_subscription_id, cancel_at_period_end", SOURCE)

    def test_active_uncancelled_tribute_is_skipped(self):
        self.assertIn(
            's.provider === "tribute" && !s.cancel_at_period_end',
            SOURCE,
        )
        self.assertIn('skipped: "tribute_auto_renew"', SOURCE)

    def test_cancelled_tribute_is_not_blanket_skipped(self):
        self.assertNotIn('s.provider === "tribute")', SOURCE)

    def test_version_is_v13(self):
        self.assertIn('version: "v13"', SOURCE)


if __name__ == "__main__":
    unittest.main()
