"""Tests for the /digest period selector.

The command accepts an explicit period as an argument and otherwise shows three
buttons. The callback data of those buttons is a contract with the bot-digest
edge function, which appends the same `digest:<kind>` buttons under a delivered
recap, so a typo in either place silently breaks the picker.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

# bot.py reads required config at import time; provide throwaway values so the
# module can be imported in CI. No network calls happen at import.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ.setdefault("SUPABASE_URL", "https://example.invalid")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test")
os.environ.setdefault("TELEGRAM_TRADING_CHANNEL_ID", "-1000000000000")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402

# Periods accepted by build_digest_for_telegram and by the edge function.
VALID_KINDS = {"day", "yesterday", "week"}


class DigestKindFromArg(unittest.TestCase):
    def test_no_argument_means_show_the_picker(self):
        for arg in ("", "   ", None):
            self.assertIsNone(bot._digest_kind_from_arg(arg))

    def test_explicit_periods_both_languages(self):
        cases = {
            "week": "week", "w": "week", "7d": "week",
            "неделя": "week", "Неделю": "week",
            "yesterday": "yesterday", "yest": "yesterday", "y": "yesterday",
            "вчера": "yesterday", "ВЧЕРА": "yesterday",
            "today": "day", "day": "day", "сегодня": "day",
        }
        for arg, expected in cases.items():
            with self.subTest(arg=arg):
                self.assertEqual(bot._digest_kind_from_arg(arg), expected)

    def test_unknown_argument_falls_back_to_today(self):
        for arg in ("month", "мусор", "42"):
            self.assertEqual(bot._digest_kind_from_arg(arg), "day")

    def test_every_resolved_kind_is_supported_downstream(self):
        for arg in ("week", "вчера", "today", "junk"):
            self.assertIn(bot._digest_kind_from_arg(arg), VALID_KINDS)


class DigestPickerMarkup(unittest.TestCase):
    def test_three_buttons_with_valid_callback_data(self):
        for lang in ("ru", "en"):
            with self.subTest(lang=lang):
                rows = bot._digest_picker_markup(lang).inline_keyboard
                self.assertEqual(len(rows), 1)
                self.assertEqual(len(rows[0]), 3)
                kinds = []
                for btn in rows[0]:
                    self.assertTrue(btn.callback_data.startswith("digest:"))
                    kind = btn.callback_data.split(":", 1)[1]
                    self.assertIn(kind, VALID_KINDS)
                    self.assertTrue(btn.text.strip())
                    kinds.append(kind)
                self.assertEqual(sorted(kinds), sorted(VALID_KINDS))

    def test_labels_differ_between_languages(self):
        ru = [b.text for b in bot._digest_picker_markup("ru").inline_keyboard[0]]
        en = [b.text for b in bot._digest_picker_markup("en").inline_keyboard[0]]
        self.assertNotEqual(ru, en)

    def test_unknown_language_falls_back_to_russian(self):
        ru = [b.text for b in bot._digest_picker_markup("ru").inline_keyboard[0]]
        other = [b.text for b in bot._digest_picker_markup("de").inline_keyboard[0]]
        self.assertEqual(ru, other)


if __name__ == "__main__":
    unittest.main()
