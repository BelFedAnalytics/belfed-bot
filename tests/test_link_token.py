"""Tests for web-first Telegram link-token detection.

Run with:  python3 -m unittest discover -s tests  (from the repo root)

Regression: the /start handler used to treat any payload >= 16 chars as a link
token. `members_bottom_en` (17 chars) was therefore misclassified as a token and
answered with "token invalid", while real hex tokens worked by accident. The
detector must accept the site's hex tokens and reject every named payload.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from link_token import looks_like_link_token  # noqa: E402


class LinkTokenTests(unittest.TestCase):
    def test_accepts_real_hex_tokens(self):
        # telegram-link-start issues 18-byte hex tokens = 36 chars.
        self.assertTrue(looks_like_link_token("a" * 36))
        self.assertTrue(looks_like_link_token("0123456789abcdef0123456789abcdef0123"))
        self.assertTrue(looks_like_link_token("f" * 32))
        self.assertTrue(looks_like_link_token("f" * 64))

    def test_rejects_named_deeplink_payloads(self):
        # These are the exact static CTAs that must NOT be treated as tokens.
        for payload in (
            "members_en",
            "members_bottom_en",
            "trial_link",
            "trial_home_hero_en",
            "auth",
            "auth_xyz",
            "promo_BFWB-123456",
        ):
            self.assertFalse(looks_like_link_token(payload), payload)

    def test_rejects_non_hex_and_bad_lengths(self):
        self.assertFalse(looks_like_link_token(""))
        self.assertFalse(looks_like_link_token(None))
        self.assertFalse(looks_like_link_token("g" * 36))          # non-hex char
        self.assertFalse(looks_like_link_token("A" * 36))          # uppercase (generator emits lowercase)
        self.assertFalse(looks_like_link_token("abc123"))          # too short
        self.assertFalse(looks_like_link_token("f" * 65))          # too long
        self.assertFalse(looks_like_link_token("dead beef" * 4))   # contains space


if __name__ == "__main__":
    unittest.main()
