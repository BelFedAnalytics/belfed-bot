"""Regression tests for bot.parse_ts.

Context: `datetime.fromisoformat()` on Python < 3.11 accepts fractional seconds
only when they are exactly 3 or 6 digits long (bpo-35829). PostgREST trims
trailing zeros, so it happily emits 1, 2, 4 or 5 digits — e.g.
`2026-07-30T13:07:34.61693+00:00`. On 2026-07-27 that made an active
subscriber's expiry unparseable on the 3.10.12 production VPS: `parse_ts`
swallowed the ValueError, returned None, `has_access()` read that as "no
expiry" and the bot told a paying user "Подписки нет".

The bug was invisible to CI because CI ran 3.11, where every fractional length
parses. These tests therefore only have value while the matrix in
.github/workflows/test.yml includes the exact Python version running in
production. Do not drop 3.10 from that matrix while the VPS is on 3.10.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

# bot.py reads required config at import time; provide throwaway values so the
# module can be imported in CI. No network calls happen at import — the
# Application is only built inside main(), under `if __name__ == "__main__"`.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ.setdefault("SUPABASE_URL", "https://example.invalid")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test")
os.environ.setdefault("TELEGRAM_TRADING_CHANNEL_ID", "-1000000000000")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402


class ParseTsFractionalSeconds(unittest.TestCase):
    """Every fractional-second length Postgres can emit must parse."""

    EXPECTED = datetime(2026, 7, 30, 13, 7, 34, tzinfo=timezone.utc)

    def test_fractional_digit_lengths(self):
        # (input, expected microsecond component)
        cases = [
            ("2026-07-30T13:07:34.6+00:00", 600000),
            ("2026-07-30T13:07:34.61+00:00", 610000),
            ("2026-07-30T13:07:34.616+00:00", 616000),
            ("2026-07-30T13:07:34.6169+00:00", 616900),
            # 5 digits — the exact shape that broke production.
            ("2026-07-30T13:07:34.61693+00:00", 616930),
            ("2026-07-30T13:07:34.616930+00:00", 616930),
        ]
        for raw, micros in cases:
            with self.subTest(raw=raw):
                got = bot.parse_ts(raw)
                self.assertIsNotNone(
                    got, f"parse_ts returned None for {raw!r} — regression of the 2026-07-27 bug"
                )
                self.assertEqual(got, self.EXPECTED.replace(microsecond=micros))
                self.assertEqual(got.utcoffset().total_seconds(), 0)

    def test_more_than_six_digits_is_truncated_not_rejected(self):
        got = bot.parse_ts("2026-07-30T13:07:34.6169301234+00:00")
        self.assertIsNotNone(got)
        self.assertEqual(got.microsecond, 616930)

    def test_no_fractional_part(self):
        got = bot.parse_ts("2026-07-30T13:07:34+00:00")
        self.assertEqual(got, self.EXPECTED)

    def test_zulu_suffix(self):
        for raw in ("2026-07-30T13:07:34.61693Z", "2026-07-30T13:07:34Z"):
            with self.subTest(raw=raw):
                got = bot.parse_ts(raw)
                self.assertIsNotNone(got)
                self.assertEqual(got.utcoffset().total_seconds(), 0)

    def test_postgres_space_separator(self):
        got = bot.parse_ts("2026-07-30 13:07:34.61693+00")
        self.assertIsNotNone(got, "Postgres' space-separated form must parse")
        self.assertEqual(got.microsecond, 616930)
        self.assertEqual(got.utcoffset().total_seconds(), 0)

    def test_short_and_compact_utc_offsets(self):
        """Python 3.10 rejects `+00` and `+0000`; Postgres emits `+00`."""
        for raw in (
            "2026-07-30T13:07:34.61693+00",
            "2026-07-30T13:07:34.61693+0000",
            "2026-07-30 13:07:34.61693+00",
            "2026-07-30T13:07:34+00",
        ):
            with self.subTest(raw=raw):
                got = bot.parse_ts(raw)
                self.assertIsNotNone(got, f"{raw!r} must parse on every supported Python")
                self.assertEqual(got.utcoffset().total_seconds(), 0)

    def test_compact_non_utc_offset(self):
        for raw, offset in (
            ("2026-07-30T16:07:34.61693+03", 3 * 3600),
            ("2026-07-30T16:07:34.61693+0300", 3 * 3600),
            ("2026-07-30T08:07:34.61693-05", -5 * 3600),
        ):
            with self.subTest(raw=raw):
                got = bot.parse_ts(raw)
                self.assertIsNotNone(got)
                self.assertEqual(got.utcoffset().total_seconds(), offset)
                self.assertEqual(got, self.EXPECTED.replace(microsecond=616930))

    def test_minutes_only_time(self):
        got = bot.parse_ts("2026-07-30T13:07+00:00")
        self.assertEqual(got, self.EXPECTED.replace(second=0))

    def test_date_only_is_not_mangled(self):
        """Guard against the offset regex eating the `-30` of a bare date."""
        got = bot.parse_ts("2026-07-30")
        self.assertIsNotNone(got)
        self.assertEqual((got.year, got.month, got.day), (2026, 7, 30))
        self.assertEqual((got.hour, got.minute, got.second), (0, 0, 0))
        self.assertIsNone(got.tzinfo)

    def test_surrounding_whitespace_tolerated(self):
        got = bot.parse_ts("  2026-07-30T13:07:34.61693+00:00\n")
        self.assertEqual(got, self.EXPECTED.replace(microsecond=616930))

    def test_non_utc_offset_is_preserved(self):
        got = bot.parse_ts("2026-07-30T16:07:34.61693+03:00")
        self.assertIsNotNone(got)
        self.assertEqual(got.utcoffset().total_seconds(), 3 * 3600)
        # Same instant as the UTC fixture.
        self.assertEqual(got, self.EXPECTED.replace(microsecond=616930))

    def test_naive_timestamp_still_parses(self):
        got = bot.parse_ts("2026-07-30T13:07:34.61693")
        self.assertIsNotNone(got)
        self.assertIsNone(got.tzinfo)


class ParseTsRejectsBadInput(unittest.TestCase):
    """Falsy and malformed input must return None rather than raise."""

    def test_returns_none(self):
        for raw in (
            None,
            "",
            "   ",
            "not a date",
            "2026-13-45T99:99:99+00:00",
            "0",
            "2026-07-30T13:07:34.61693+",
            "30.07.2026",
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(bot.parse_ts(raw))

    def test_non_string_input_returns_none(self):
        for raw in (0, 1753880854, 3.14, [], {}, object()):
            with self.subTest(raw=type(raw).__name__):
                self.assertIsNone(bot.parse_ts(raw))


class HasAccessUsesParsedExpiry(unittest.TestCase):
    """Guard the actual failure path, not just the parser in isolation.

    A profile with an active subscription and a 5-digit-fraction expiry in the
    future must grant access. This is what returned False in production.
    """

    def _profile(self, expires_at: str, status: str = "active"):
        return {
            "subscription_status": status,
            "subscription_plan": "month",
            "subscription_expires_at": expires_at,
            "trial_started_at": None,
        }

    def test_active_subscription_with_five_digit_fraction_has_access(self):
        future = datetime.now(timezone.utc).replace(microsecond=0)
        future = future.replace(year=future.year + 1)
        raw = future.strftime("%Y-%m-%dT%H:%M:%S") + ".61693+00:00"
        self.assertTrue(
            bot.has_access(self._profile(raw)),
            "active subscriber with a 5-digit fractional expiry must have access",
        )

    def test_expired_subscription_has_no_access(self):
        past = datetime.now(timezone.utc).replace(microsecond=0)
        past = past.replace(year=past.year - 1)
        raw = past.strftime("%Y-%m-%dT%H:%M:%S") + ".61693+00:00"
        self.assertFalse(bot.has_access(self._profile(raw)))

    def test_missing_expiry_has_no_access(self):
        self.assertFalse(bot.has_access(self._profile(None)))


if __name__ == "__main__":
    unittest.main()
