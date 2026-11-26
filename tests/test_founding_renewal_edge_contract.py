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

    def test_version_is_v9(self):
        self.assertIn('version: "v9"', SOURCE)

    def test_autorenew_notice_states_actual_charge_and_period(self):
        # Tribute sells 1, 3, 6 and 12-month periods: the notice must not
        # promise a fixed monthly charge to a member who paid for a year.
        self.assertIn('.from("payments")', SOURCE)
        self.assertIn("period_months", SOURCE)
        self.assertNotIn("Founding-цена 1050 ₽/мес зафиксирована", SOURCE)
        self.assertNotIn("Один платёж 1050 ₽", SOURCE)
        self.assertNotIn("669⭐", SOURCE)

    def test_manual_notice_carries_period_note(self):
        self.assertIn("PERIOD_NOTE_RU +", SOURCE)
        self.assertIn("PERIOD_NOTE_EN +", SOURCE)

    def test_no_long_dashes_in_source(self):
        self.assertNotIn("\u2014", SOURCE)
        self.assertNotIn("\u2013", SOURCE)

    def test_reminder_view_exposes_cancellation_state(self):
        self.assertIn("s.cancel_at_period_end", MIGRATION)


if __name__ == "__main__":
    unittest.main()
