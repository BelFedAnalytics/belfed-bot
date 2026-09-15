"""Compliance tests for email-collection privacy notices (RU + EN).

Covers the first reversible legal/compliance remediation batch:
 - every in-bot email prompt shows a privacy notice with the locale-correct
   policy link before the email is entered (152-ФЗ ст.9, ст.18.1);
 - marketing/advertising is kept separate from service processing — no prompt
   implies a newsletter / win-back subscription, and each states that marketing
   needs separate consent (38-ФЗ ст.18);
 - service-flow wording and keys are unchanged.

Pure-string / constant assertions only: no network, DB or Telegram calls, so
the suite is safe to run in CI without secrets. Run with:
    python -m unittest test_email_privacy_compliance -v
"""
import os
import pathlib
import unittest

# bot.py reads required config at import time — supply harmless dummy values.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")
os.environ.setdefault("TELEGRAM_TRADING_CHANNEL_ID", "-1000000000000")

import bot  # noqa: E402

BOT_SRC = pathlib.Path(bot.__file__).read_text(encoding="utf-8")

# In-bot email prompts served from the message dictionaries via bot.T().
EMAIL_PROMPT_KEYS = ("ask_email", "ask_email_link")

# Marketing product names that must never appear in user-facing copy — their
# presence would imply an implicit newsletter / win-back subscription. (These
# may still live in code comments describing the backend dependency.) Note we
# deliberately do NOT forbid the word "рассылк"/"marketing" itself, because the
# notices legitimately state email is NOT used for advertising mailings.
MARKETING_TERMS = ("newsletter", "win-back", "winback")


class PolicyLinkConstants(unittest.TestCase):
    def test_locale_specific_and_distinct(self):
        self.assertIn("belfed.ru", bot.PRIVACY_URL_RU)
        self.assertIn("belfed.com", bot.PRIVACY_URL_EN)
        self.assertNotEqual(bot.PRIVACY_URL_RU, bot.PRIVACY_URL_EN)

    def test_point_at_a_privacy_page(self):
        self.assertIn("privacy", bot.PRIVACY_URL_RU.lower())
        self.assertIn("privacy", bot.PRIVACY_URL_EN.lower())


class EmailPromptPrivacyNotice(unittest.TestCase):
    """Each email prompt must carry a privacy notice + correct policy link."""

    def _prompt(self, lang, key):
        return bot.T(lang, key)

    def test_ru_prompts_have_ru_policy_link(self):
        for key in EMAIL_PROMPT_KEYS:
            text = self._prompt("ru", key)
            self.assertIn(bot.PRIVACY_URL_RU, text, key)
            # must not leak the other locale's link
            self.assertNotIn(bot.PRIVACY_URL_EN, text, key)
            self.assertIn("онфиденциальност", text, key)  # "Политика конфиденциальности"

    def test_en_prompts_have_en_policy_link(self):
        for key in EMAIL_PROMPT_KEYS:
            text = self._prompt("en", key)
            self.assertIn(bot.PRIVACY_URL_EN, text, key)
            self.assertNotIn(bot.PRIVACY_URL_RU, text, key)
            self.assertIn("privacy", text.lower(), key)

    def test_notice_precedes_the_example_email(self):
        # The policy link must appear before we ask the user to send the email,
        # i.e. the notice is shown *before* collection, not after.
        for lang in ("ru", "en"):
            for key in EMAIL_PROMPT_KEYS:
                text = self._prompt(lang, key)
                url = bot.PRIVACY_URL_RU if lang == "ru" else bot.PRIVACY_URL_EN
                self.assertLess(
                    text.index(url), text.index("ivan@example.com"),
                    f"policy link must precede the send-email instruction ({lang},{key})",
                )


class ServicePurposeWording(unittest.TestCase):
    """Wording must match the real purpose of each collection point."""

    def test_payment_prompt_is_about_receipt_not_marketing(self):
        for lang in ("ru", "en"):
            text = bot.T(lang, "ask_email")
            if lang == "ru":
                self.assertIn("чек", text)          # фискальный чек
            else:
                self.assertIn("receipt", text.lower())

    def test_link_prompt_is_about_account_not_marketing(self):
        self.assertIn("аккаунт", bot.T("ru", "ask_email_link").lower())
        self.assertIn("account", bot.T("en", "ask_email_link").lower())

    def test_prompts_state_marketing_needs_separate_consent(self):
        self.assertIn("отдельного согласия", bot.T("ru", "ask_email"))
        self.assertIn("отдельного согласия", bot.T("ru", "ask_email_link"))
        self.assertIn("separate consent", bot.T("en", "ask_email"))
        self.assertIn("separate consent", bot.T("en", "ask_email_link"))


class NoImplicitMarketingConsent(unittest.TestCase):
    """Entering an email must not read as opting into marketing."""

    def test_no_marketing_terms_in_user_facing_texts(self):
        for book_name in ("TEXTS_RU", "TEXTS_EN"):
            book = getattr(bot, book_name)
            for key, value in book.items():
                if not isinstance(value, str):
                    continue
                low = value.lower()
                for term in MARKETING_TERMS:
                    self.assertNotIn(
                        term, low,
                        f"user-facing text {book_name}[{key!r}] must not mention "
                        f"marketing term {term!r}",
                    )

    def test_no_prebaked_marketing_optin_default(self):
        # There must be no button/label that silently subscribes the user to
        # marketing (no default opt-in). Scan interactive labels.
        for book_name in ("TEXTS_RU", "TEXTS_EN"):
            book = getattr(bot, book_name)
            for key, value in book.items():
                if key.startswith("btn_") and isinstance(value, str):
                    self.assertNotIn("рассыл", value.lower(), key)
                    self.assertNotIn("newsletter", value.lower(), key)


class AllCollectionPointsCovered(unittest.TestCase):
    """The inline founding & Stars-upgrade prompts live in handler code, not in
    TEXTS. Assert the locale policy links are wired into every collection point
    by counting references in the source (payment, link, founding, stars ×2
    locales)."""

    def test_ru_policy_link_used_across_prompts(self):
        self.assertGreaterEqual(BOT_SRC.count("PRIVACY_URL_RU"), 4)

    def test_en_policy_link_used_across_prompts(self):
        self.assertGreaterEqual(BOT_SRC.count("PRIVACY_URL_EN"), 4)

    def test_backend_dependency_documented(self):
        # The implicit-opt-in DB trigger dependency must be documented in-code.
        self.assertIn("profiles_auto_opt_in_email", BOT_SRC)
        self.assertIn("COMPLIANCE DEPENDENCY", BOT_SRC)


class ServiceFlowUnchanged(unittest.TestCase):
    """Guard that service-flow behaviour and keys were not altered."""

    def test_service_message_keys_present(self):
        for key in ("email_saved", "email_invalid", "link_ok", "trial_claim_ok",
                    "pay_error", "email_link_reused"):
            self.assertIn(key, bot.TEXTS_RU, key)
            self.assertIn(key, bot.TEXTS_EN, key)

    def test_email_validation_helpers_unchanged(self):
        self.assertTrue(bot.is_valid_email("ivan@example.com"))
        self.assertFalse(bot.is_valid_email("not-an-email"))
        self.assertFalse(bot.is_valid_email("tg123@belfed.local"))  # ghost
        self.assertTrue(bot.is_ghost_email("tg123@belfed.local"))
        self.assertFalse(bot.is_ghost_email("ivan@example.com"))

    def test_receipt_confirmation_still_reports_saved_email(self):
        # Service acknowledgement after saving must be unchanged (no marketing).
        self.assertIn("{email}", bot.TEXTS_RU["email_saved"])
        self.assertIn("{email}", bot.TEXTS_EN["email_saved"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
