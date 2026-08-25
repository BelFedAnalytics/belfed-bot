"""Static regression contract for the Founding renewal reminder."""

from __future__ import annotations

import unittest
from pathlib import Path


SOURCE = (
    Path(__file__).resolve().parent.parent
    / "supabase"
    / "functions"
    / "telegram-founding-renewal-reminder"
    / "index.ts"
).read_text(encoding="utf-8")

MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "supabase"
    / "migrations"
    / "20260825_founding_renewal_include_cancel_state.sql"
).read_text(encoding="utf-8")


class FoundingTributeAutoRenewContract(unittest.TestCase):
    def test_tribute_autorenew_requires_not_cancelled(self):
        self.assertIn('row.provider === "tribute"', SOURCE)
        self.assertIn("!row.cancel_at_period_end", SOURCE)

    def test_all_provider_rules_are_explicit(self):
        self.assertIn('row.provider === "yookassa"', SOURCE)
        self.assertIn('row.provider === "telegram_stars"', SOURCE)
        self.assertIn(
            "yookassaAuto || starsAuto || tributeAuto",
            SOURCE,
        )

    def test_active_tribute_copy_does_not_claim_saved_local_method(self):
        self.assertIn(
            "Подписка продлится автоматически в эту дату.",
            SOURCE,
        )
        self.assertNotIn(
            "Платёжный метод сохранён — списание 1050 ₽",
            SOURCE,
        )

    def test_version_is_v8(self):
        self.assertIn('version: "v8"', SOURCE)

    def test_reminder_view_exposes_cancellation_state(self):
        self.assertIn("s.cancel_at_period_end", MIGRATION)


if __name__ == "__main__":
    unittest.main()
