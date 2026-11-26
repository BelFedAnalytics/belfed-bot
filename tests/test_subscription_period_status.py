"""Regression tests for period-aware subscription status.

Tribute sells 1, 3, 6 and 12-month periods. The paid period is stored on the
payment row, so /status must describe the actual purchase instead of a fixed
monthly Founding price, and every pay link must remind about choosing a period.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ.setdefault("SUPABASE_URL", "https://example.invalid")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test")
os.environ.setdefault("TELEGRAM_TRADING_CHANNEL_ID", "-1000000000000")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402

NBSP = "\u00a0"


class FormatMoney(unittest.TestCase):
    def test_rub_groups_thousands(self):
        self.assertEqual(bot.format_money(9450, "RUB"), f"9{NBSP}450{NBSP}₽")
        self.assertEqual(bot.format_money("1050.00", "rub"), f"1{NBSP}050{NBSP}₽")

    def test_usd_keeps_cents_only_when_needed(self):
        self.assertEqual(bot.format_money(94.5, "USD"), "$94.50")
        self.assertEqual(bot.format_money(15, "USD"), "$15")

    def test_missing_amount_is_empty(self):
        self.assertEqual(bot.format_money(None, "RUB"), "")


class PeriodLabel(unittest.TestCase):
    def test_russian_plural_forms(self):
        self.assertEqual(bot.period_label(1, "ru"), "1 месяц")
        self.assertEqual(bot.period_label(3, "ru"), "3 месяца")
        self.assertEqual(bot.period_label(6, "ru"), "6 месяцев")
        self.assertEqual(bot.period_label(12, "ru"), "12 месяцев")

    def test_english_forms(self):
        self.assertEqual(bot.period_label(1, "en"), "1 month")
        self.assertEqual(bot.period_label(12, "en"), "12 months")

    def test_missing_period_means_one_month(self):
        self.assertEqual(bot.period_label(None, "ru"), "1 месяц")


class PlanText(unittest.TestCase):
    def test_yearly_founding_is_not_shown_as_monthly(self):
        pay = {"amount": 9450, "currency": "RUB", "period_months": 12}
        text = bot.subscription_plan_text(pay, "ru", True)
        self.assertEqual(text, f"Founding Member · 9{NBSP}450{NBSP}₽ за 12 месяцев")
        self.assertNotIn("/ мес", text)

    def test_monthly_standard_ru(self):
        pay = {"amount": 1500, "currency": "RUB", "period_months": 1}
        self.assertEqual(
            bot.subscription_plan_text(pay, "ru", False),
            f"подписка BelFed · 1{NBSP}500{NBSP}₽ / мес",
        )

    def test_yearly_founding_en(self):
        pay = {"amount": 94.5, "currency": "USD", "period_months": 12}
        self.assertEqual(
            bot.subscription_plan_text(pay, "en", True),
            "Founding Member · $94.50 for 12 months",
        )

    def test_legacy_payment_without_period_is_monthly(self):
        pay = {"amount": 10.5, "currency": "USD", "period_months": None}
        self.assertEqual(
            bot.subscription_plan_text(pay, "en", True),
            "Founding Member · $10.50 / mo",
        )

    def test_no_payment_falls_back_without_inventing_a_price(self):
        self.assertEqual(bot.subscription_plan_text(None, "ru", False), "подписка BelFed")
        self.assertIn(bot.FM_PRICE_RUB, bot.subscription_plan_text(None, "ru", True))


class StatusTexts(unittest.TestCase):
    def test_status_active_uses_plan_placeholder(self):
        for lang in ("ru", "en"):
            template = bot.T(lang, "status_active")
            self.assertIn("{plan}", template)
            self.assertNotIn("−30%", template)
            rendered = template.format(plan="X", until="26.11.2027", autorenew="Y")
            self.assertIn("X", rendered)

    def test_pay_link_messages_carry_period_note(self):
        for lang, note in (("ru", bot.PERIOD_NOTE_RU), ("en", bot.PERIOD_NOTE_EN)):
            for key in ("trial_active", "status_none", "trial_claim_used"):
                self.assertIn(note, bot.T(lang, key), f"{lang}:{key}")

    def test_period_notes_have_no_long_dashes(self):
        for note in (bot.PERIOD_NOTE_RU, bot.PERIOD_NOTE_EN):
            self.assertNotIn("\u2014", note)
            self.assertNotIn("\u2013", note)


if __name__ == "__main__":
    unittest.main()
