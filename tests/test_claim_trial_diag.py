"""Focused tests for claim-trial failure diagnostics.

Covers the 2026-07-17 production incident: overloaded
public.claim_trial_by_telegram makes PostgREST 5-arg RPC resolution
ambiguous (PGRST203), surfacing as HTTP 500 from bot-claim-trial. These
tests pin the sanitizer (no secret/PII leakage) and the failure classifier
(incident is greppable via a stable tag).

Run: python3 -m unittest tests.test_claim_trial_diag -v
"""

import os
import unittest

# bot.py reads required env at import time; provide dummies so it imports
# without touching the network or real config.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-key")
os.environ.setdefault("TELEGRAM_TRADING_CHANNEL_ID", "-1000000000000")

import bot  # noqa: E402


# Real-shape PostgREST body for the ambiguous overload (secrets stripped).
AMBIGUOUS_BODY = (
    '{"code":"PGRST203","details":null,'
    '"hint":"Try renaming the parameters or the function itself in the database '
    'so function overloading can be resolved",'
    '"message":"Could not choose the best candidate function between: '
    'public.claim_trial_by_telegram(p_telegram_id => text, p_telegram_username => text, '
    'p_trial_days => integer, p_source => text, p_lang => text), '
    'public.claim_trial_by_telegram(p_telegram_id => text, p_telegram_username => text, '
    'p_trial_days => integer, p_source => text, p_lang => text, p_email => text, '
    'p_consent => boolean, ...)"}'
)


class TestClassifyClaimTrialFailure(unittest.TestCase):
    def test_ambiguous_overload_by_code(self):
        self.assertEqual(
            bot.classify_claim_trial_failure(500, AMBIGUOUS_BODY),
            "ambiguous_rpc_overload",
        )

    def test_ambiguous_overload_by_message_only(self):
        body = '{"message":"Could not choose the best candidate function between: ..."}'
        self.assertEqual(
            bot.classify_claim_trial_failure(500, body),
            "ambiguous_rpc_overload",
        )

    def test_ambiguous_detection_is_case_insensitive(self):
        self.assertEqual(
            bot.classify_claim_trial_failure(500, "pgrst203"),
            "ambiguous_rpc_overload",
        )

    def test_rpc_not_found(self):
        body = '{"code":"PGRST202","message":"Could not find the function ..."}'
        self.assertEqual(bot.classify_claim_trial_failure(404, body), "rpc_not_found")

    def test_other_postgrest_error(self):
        body = '{"code":"PGRST301","message":"JWT expired"}'
        self.assertEqual(bot.classify_claim_trial_failure(500, body), "postgrest_error")

    def test_generic_5xx_without_body(self):
        self.assertEqual(bot.classify_claim_trial_failure(502, ""), "upstream_5xx")
        self.assertEqual(bot.classify_claim_trial_failure(500, None), "upstream_5xx")

    def test_generic_4xx(self):
        self.assertEqual(bot.classify_claim_trial_failure(400, "bad request"), "upstream_4xx")

    def test_unknown_when_no_signal(self):
        self.assertEqual(bot.classify_claim_trial_failure(None, None), "unknown")

    def test_ambiguous_takes_priority_over_status(self):
        # Even a 200-ish status must still be classified by body signature.
        self.assertEqual(
            bot.classify_claim_trial_failure(300, AMBIGUOUS_BODY),
            "ambiguous_rpc_overload",
        )


class TestSanitizeDiag(unittest.TestCase):
    def test_empty_inputs(self):
        self.assertEqual(bot.sanitize_diag(None), "")
        self.assertEqual(bot.sanitize_diag(""), "")

    def test_redacts_jwt_like_service_key(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.abc123DEF456ghi"
        out = bot.sanitize_diag(f'{{"error":"denied","token":"{jwt}"}}')
        self.assertNotIn(jwt, out)
        self.assertIn("[REDACTED_JWT]", out)

    def test_redacts_bearer_header(self):
        out = bot.sanitize_diag("Authorization: Bearer sk-supersecret-value-123")
        self.assertNotIn("sk-supersecret-value-123", out)
        self.assertIn("[REDACTED]", out)

    def test_redacts_apikey_and_bot_secret(self):
        out = bot.sanitize_diag('x-bot-secret: my-shared-secret apikey=abcd1234')
        self.assertNotIn("my-shared-secret", out)
        self.assertNotIn("abcd1234", out)

    def test_truncates_to_limit(self):
        self.assertEqual(len(bot.sanitize_diag("x" * 1000, limit=50)), 50)

    def test_collapses_whitespace(self):
        self.assertEqual(bot.sanitize_diag("a\n\n  b\tc"), "a b c")

    def test_ambiguous_body_survives_sanitization(self):
        # The incident signature must remain classifiable after sanitizing,
        # since we sanitize before logging.
        cleaned = bot.sanitize_diag(AMBIGUOUS_BODY, limit=1000)
        self.assertEqual(
            bot.classify_claim_trial_failure(500, cleaned),
            "ambiguous_rpc_overload",
        )
        self.assertIn("claim_trial_by_telegram", cleaned)


if __name__ == "__main__":
    unittest.main()
