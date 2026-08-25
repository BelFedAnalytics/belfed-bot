"""Regression tests for subscription auto-renew detection.

Tribute subscriptions renew at the provider even though our local row does not
contain a payment_method_id. Treating that nullable local field as the only
signal made the bot report "auto-renew off" for active Tribute members.
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


class SubscriptionAutoRenew(unittest.TestCase):
    def test_active_tribute_subscription_renews_without_local_payment_method(self):
        sub = {
            "provider": "tribute",
            "status": "active",
            "payment_method_id": None,
            "provider_subscription_id": "229504",
            "cancel_at_period_end": False,
        }
        self.assertTrue(bot.is_autorenew_enabled(sub))

    def test_cancelled_tribute_subscription_does_not_renew(self):
        sub = {
            "provider": "tribute",
            "status": "active",
            "payment_method_id": None,
            "provider_subscription_id": "229504",
            "cancel_at_period_end": True,
        }
        self.assertFalse(bot.is_autorenew_enabled(sub))

    def test_yookassa_requires_saved_payment_method(self):
        base = {
            "provider": "yookassa",
            "status": "active",
            "provider_subscription_id": None,
            "cancel_at_period_end": False,
        }
        self.assertFalse(bot.is_autorenew_enabled({**base, "payment_method_id": None}))
        self.assertTrue(bot.is_autorenew_enabled({**base, "payment_method_id": "pm_123"}))

    def test_telegram_stars_requires_provider_subscription(self):
        base = {
            "provider": "telegram_stars",
            "status": "active",
            "payment_method_id": None,
            "cancel_at_period_end": False,
        }
        self.assertFalse(bot.is_autorenew_enabled({**base, "provider_subscription_id": None}))
        self.assertTrue(
            bot.is_autorenew_enabled({**base, "provider_subscription_id": "stars_sub_123"})
        )

    def test_inactive_subscription_never_renews(self):
        sub = {
            "provider": "tribute",
            "status": "expired",
            "payment_method_id": None,
            "provider_subscription_id": "229504",
            "cancel_at_period_end": False,
        }
        self.assertFalse(bot.is_autorenew_enabled(sub))


if __name__ == "__main__":
    unittest.main()
