"""Tests for paid-advertising deep-link payload parsing.

Run with:  python3 -m unittest discover -s tests  (from the repo root)

Two failure modes matter here:

1.  **Channel names get mangled.** Telegram usernames legally contain
    underscores (``signals_stock``, ``if_market_news``). A naive ``split("_")``
    would report the channel as ``signals`` and silently misattribute every
    activation from that campaign.
2.  **Existing deep-links get hijacked.** The /start handler is a chain of
    prefix checks; if the ad detector is too greedy it will swallow
    ``trial_home_hero_en``, ``founding_ru``, ``promo_BFWB-123456``, ``auth`` or
    a hex link token and break flows that already work in production.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ad_attribution import (  # noqa: E402
    detect_lang_from_ad_payload,
    is_ad_payload,
    parse_ad_payload,
)


class AdPayloadDetectionTests(unittest.TestCase):
    def test_accepts_agency_payload(self):
        self.assertTrue(is_ad_payload("mag_cr_t1_k1_signals_stock"))

    def test_accepts_channel_without_underscores(self):
        self.assertTrue(is_ad_payload("mag_st_t2_k3_bitkogan"))

    def test_rejects_existing_named_payloads(self):
        """Regression guard: these already have their own /start branches."""
        for payload in (
            "trial",
            "trial_home_hero_en",
            "trial_t_0123456789abcdef0123456789abcdef",
            "founding",
            "founding_ru",
            "auth",
            "auth_xyz",
            "promo_BFWB-123456",
            "members_en",
            "members_bottom_en",
        ):
            with self.subTest(payload=payload):
                self.assertFalse(is_ad_payload(payload))

    def test_rejects_hex_link_token(self):
        self.assertFalse(is_ad_payload("a3f1" * 9))

    def test_rejects_unknown_agency_prefix(self):
        self.assertFalse(is_ad_payload("acme_cr_t1_k1_signals_stock"))

    def test_rejects_too_few_segments(self):
        self.assertFalse(is_ad_payload("mag_cr_t1_k1"))

    def test_rejects_empty_channel(self):
        self.assertFalse(is_ad_payload("mag_cr_t1_k1_"))

    def test_rejects_none_and_empty(self):
        self.assertFalse(is_ad_payload(None))
        self.assertFalse(is_ad_payload(""))

    def test_rejects_illegal_characters(self):
        """Telegram allows only A-Z a-z 0-9 _ - in the start parameter."""
        self.assertFalse(is_ad_payload("mag_cr_t1_k1_signals.stock"))
        self.assertFalse(is_ad_payload("mag_cr_t1_k1_signals stock"))

    def test_rejects_payload_over_64_chars(self):
        too_long = "mag_cr_t1_k1_" + ("x" * 60)
        self.assertGreater(len(too_long), 64)
        self.assertFalse(is_ad_payload(too_long))


class AdPayloadParsingTests(unittest.TestCase):
    def test_parses_agency_example(self):
        self.assertEqual(
            parse_ad_payload("mag_cr_t1_k1_signals_stock"),
            {
                "raw": "mag_cr_t1_k1_signals_stock",
                "agency": "mag",
                "pool": "cr",
                "text_code": "t1",
                "creative_code": "k1",
                "channel": "signals_stock",
            },
        )

    def test_channel_keeps_all_underscores(self):
        """The whole tail is the channel, however many underscores it holds."""
        for channel in ("signals_stock", "if_market_news", "direct_investor",
                        "a_b_c_d_e"):
            with self.subTest(channel=channel):
                parsed = parse_ad_payload(f"mag_st_t1_k1_{channel}")
                self.assertEqual(parsed["channel"], channel)

    def test_channel_case_is_preserved(self):
        parsed = parse_ad_payload("mag_st_t1_k1_RanksInvest")
        self.assertEqual(parsed["channel"], "RanksInvest")

    def test_service_segments_are_lowercased(self):
        parsed = parse_ad_payload("MAG_ST_T1_K1_bitkogan")
        self.assertEqual(parsed["agency"], "mag")
        self.assertEqual(parsed["pool"], "st")
        self.assertEqual(parsed["text_code"], "t1")
        self.assertEqual(parsed["creative_code"], "k1")

    def test_non_ad_payload_returns_empty_components(self):
        parsed = parse_ad_payload("trial_home_hero_en")
        self.assertEqual(parsed["raw"], "trial_home_hero_en")
        for key in ("agency", "pool", "text_code", "creative_code", "channel"):
            with self.subTest(key=key):
                self.assertIsNone(parsed[key])


class AdPayloadLanguageTests(unittest.TestCase):
    def test_language_from_pool_segment(self):
        parsed = parse_ad_payload("mag_ru_t1_k1_bitkogan")
        self.assertEqual(detect_lang_from_ad_payload(parsed), "ru")

    def test_language_from_creative_segment(self):
        parsed = parse_ad_payload("mag_st_t1_en_aabeta")
        self.assertEqual(detect_lang_from_ad_payload(parsed), "en")

    def test_channel_name_never_sets_language(self):
        """profinansy_ru is a channel, not a language marker for the creative."""
        parsed = parse_ad_payload("mag_st_t1_k1_profinansy_ru")
        self.assertIsNone(detect_lang_from_ad_payload(parsed))

    def test_no_marker_returns_none(self):
        parsed = parse_ad_payload("mag_cr_t1_k1_signals_stock")
        self.assertIsNone(detect_lang_from_ad_payload(parsed))


if __name__ == "__main__":
    unittest.main()
