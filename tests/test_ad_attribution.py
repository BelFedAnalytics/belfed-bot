"""Tests for paid-advertising deep-link payload parsing.

Run with:  python3 -m unittest discover -s tests  (from the repo root)

Three failure modes matter here, in descending order of damage:

1.  **Silent misattribution.** Positional parsing turned
    ``mag_pull_cr_t1_k1_users_namechannels`` into pool=pull, text=cr,
    channel=k1_users_namechannels — plausible-looking garbage that would have
    been read as a business result. Labelled parsing cannot do this.
2.  **Silent loss.** An allow-list on the ``mag`` prefix discarded the agency's
    own ``ch_...`` and ``pull_...`` payloads, recording paid clicks as organic.
3.  **Hijacking live flows.** ``cmd_start`` is a chain of prefix checks, so a
    detector that is too greedy breaks ``trial_*``, ``founding_*``, ``promo_*``,
    ``auth`` and hex link tokens.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ad_attribution import (  # noqa: E402
    MAX_PAYLOAD_LEN,
    detect_lang_from_ad_payload,
    is_ad_payload,
    parse_ad_payload,
)


class DetectionTests(unittest.TestCase):
    def test_accepts_labelled_payload(self):
        self.assertTrue(is_ad_payload("p-c_t-1_k-1_g-u_c-signals_stock"))

    def test_accepts_every_agency_example(self):
        """All four shapes the agency has proposed must be captured.

        The agency asked whether they may write deep-links at their own
        discretion. Detection must therefore not depend on a prefix we agreed
        in advance — an unrecognised payload is still a paid click.
        """
        for payload in (
            "mag_cr_t1_k1_signals_stock",
            "ch_cr_t1_k1_users_namechannels",
            "pull_cr_t1_k1_users_namechannels",
            "mag_pull_cr_t1_k1_users_namechannels",
        ):
            with self.subTest(payload=payload):
                self.assertTrue(is_ad_payload(payload))

    def test_rejects_reserved_bot_payloads(self):
        """Regression guard: these have their own /start branches."""
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
            "web_signup",
            "ref_abc",
        ):
            with self.subTest(payload=payload):
                self.assertFalse(is_ad_payload(payload))

    def test_rejects_hex_link_token(self):
        self.assertFalse(is_ad_payload("a3f1" * 8))

    def test_rejects_single_token(self):
        """No separator means no structure — never an ad payload."""
        for payload in ("hello", "mag", "signals"):
            with self.subTest(payload=payload):
                self.assertFalse(is_ad_payload(payload))

    def test_rejects_none_and_empty(self):
        self.assertFalse(is_ad_payload(None))
        self.assertFalse(is_ad_payload(""))

    def test_rejects_illegal_characters(self):
        """Telegram allows only A-Z a-z 0-9 _ - in the start parameter."""
        for payload in ("p-c_c-signals.stock", "p-c_c-signals stock",
                        "p-c_c-сигналы"):
            with self.subTest(payload=payload):
                self.assertFalse(is_ad_payload(payload))

    def test_rejects_payload_over_telegram_limit(self):
        too_long = "p-c_t-1_k-1_c-" + ("x" * 60)
        self.assertGreater(len(too_long), MAX_PAYLOAD_LEN)
        self.assertFalse(is_ad_payload(too_long))


class LabelledParsingTests(unittest.TestCase):
    def test_parses_full_labelled_payload(self):
        parsed = parse_ad_payload("a-mag_p-c_t-1_k-2_g-u_s-s_c-signals_stock")
        self.assertEqual(parsed["scheme"], "labelled")
        self.assertEqual(parsed["agency"], "mag")
        self.assertEqual(parsed["pool"], "crypto")
        self.assertEqual(parsed["text_code"], "1")
        self.assertEqual(parsed["creative_code"], "2")
        self.assertEqual(parsed["targeting"], "users")
        self.assertEqual(parsed["placement"], "search")
        self.assertEqual(parsed["channel"], "signals_stock")
        self.assertEqual(parsed["unparsed"], [])

    def test_order_does_not_matter(self):
        """The point of labelling: the agency may reorder freely."""
        a = parse_ad_payload("p-s_t-1_k-1_g-u_c-bitkogan")
        b = parse_ad_payload("g-u_k-1_t-1_p-s_c-bitkogan")
        for field in ("pool", "text_code", "creative_code", "targeting", "channel"):
            with self.subTest(field=field):
                self.assertEqual(a[field], b[field])

    def test_missing_parameters_stay_none(self):
        """Absence is information, not an error."""
        parsed = parse_ad_payload("p-s_c-bitkogan")
        self.assertEqual(parsed["pool"], "stocks")
        self.assertEqual(parsed["channel"], "bitkogan")
        for field in ("text_code", "creative_code", "targeting", "placement"):
            with self.subTest(field=field):
                self.assertIsNone(parsed[field])

    def test_channel_keeps_all_underscores(self):
        for channel in ("signals_stock", "if_market_news", "direct_investor",
                        "a_b_c_d_e"):
            with self.subTest(channel=channel):
                parsed = parse_ad_payload(f"p-s_t-1_c-{channel}")
                self.assertEqual(parsed["channel"], channel)

    def test_channel_case_is_preserved(self):
        parsed = parse_ad_payload("p-s_c-RanksInvest")
        self.assertEqual(parsed["channel"], "RanksInvest")

    def test_unknown_value_kept_verbatim(self):
        """A surprising code in a report is useful; a dropped click is not."""
        parsed = parse_ad_payload("p-zzz_c-bitkogan")
        self.assertEqual(parsed["pool"], "zzz")

    def test_unknown_label_is_surfaced_not_dropped(self):
        parsed = parse_ad_payload("p-s_x-9_c-bitkogan")
        self.assertEqual(parsed["pool"], "stocks")
        self.assertIn("x-9", parsed["unparsed"])

    def test_long_form_values_accepted(self):
        parsed = parse_ad_payload("p-inv_g-channels_s-bots_c-cbonds")
        self.assertEqual(parsed["pool"], "invest")
        self.assertEqual(parsed["targeting"], "channels")
        self.assertEqual(parsed["placement"], "bots")

    def test_realistic_payload_fits_telegram_limit(self):
        """Worst case: every parameter plus a maximum-length username."""
        payload = "a-mag_p-c_t-1_k-1_g-u_s-s_c-" + ("x" * 32)
        self.assertLessEqual(len(payload), MAX_PAYLOAD_LEN)
        self.assertTrue(is_ad_payload(payload))
        self.assertEqual(parse_ad_payload(payload)["channel"], "x" * 32)


class LegacyPositionalTests(unittest.TestCase):
    """Links may already exist in the originally agreed positional shape."""

    def test_parses_agreed_positional_payload(self):
        parsed = parse_ad_payload("mag_cr_t1_k1_signals_stock")
        self.assertEqual(parsed["scheme"], "legacy")
        self.assertEqual(parsed["agency"], "mag")
        self.assertEqual(parsed["pool"], "crypto")
        self.assertEqual(parsed["text_code"], "t1")
        self.assertEqual(parsed["creative_code"], "k1")
        self.assertEqual(parsed["channel"], "signals_stock")


class AmbiguousPayloadTests(unittest.TestCase):
    """The agency's exploratory examples are mutually contradictory.

    ``ch_cr_t1_k1_users_namechannels`` carries both ``ch`` and ``users``, either
    of which could be the targeting mode. The correct behaviour is to capture
    the click and expose the ambiguity — never to invent a reading.
    """

    def test_ambiguous_payload_is_captured_not_dropped(self):
        for payload in ("ch_cr_t1_k1_users_namechannels",
                        "pull_cr_t1_k1_users_namechannels"):
            with self.subTest(payload=payload):
                parsed = parse_ad_payload(payload)
                self.assertEqual(parsed["raw"], payload)
                self.assertTrue(parsed["unparsed"] or parsed["scheme"] != "unknown")

    def test_ambiguous_payload_never_silently_misparses_channel(self):
        """The old positional parser reported channel=k1_users_namechannels."""
        parsed = parse_ad_payload("mag_pull_cr_t1_k1_users_namechannels")
        self.assertNotEqual(parsed["channel"], "k1_users_namechannels")

    def test_unrecognised_segments_are_all_preserved(self):
        parsed = parse_ad_payload("ch_cr_t1_k1_users_namechannels")
        if parsed["scheme"] == "unknown":
            self.assertEqual(
                parsed["unparsed"],
                ["ch", "cr", "t1", "k1", "users", "namechannels"],
            )


class LanguageTests(unittest.TestCase):
    def test_language_from_pool_label(self):
        parsed = parse_ad_payload("p-ru_c-bitkogan")
        self.assertEqual(detect_lang_from_ad_payload(parsed), "ru")

    def test_language_from_creative_label(self):
        parsed = parse_ad_payload("p-s_k-en_c-aabeta")
        self.assertEqual(detect_lang_from_ad_payload(parsed), "en")

    def test_channel_name_never_sets_language(self):
        """profinansy_ru is a channel, not a language marker for the creative."""
        parsed = parse_ad_payload("p-s_t-1_k-1_c-profinansy_ru")
        self.assertIsNone(detect_lang_from_ad_payload(parsed))

    def test_no_marker_returns_none(self):
        parsed = parse_ad_payload("p-c_t-1_k-1_c-signals_stock")
        self.assertIsNone(detect_lang_from_ad_payload(parsed))


if __name__ == "__main__":
    unittest.main()
