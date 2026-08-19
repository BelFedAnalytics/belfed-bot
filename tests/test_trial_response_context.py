import os
import sys
from pathlib import Path

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ.setdefault("SUPABASE_URL", "https://example.invalid")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test")
os.environ.setdefault("TELEGRAM_TRADING_CHANNEL_ID", "-1000000000000")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot  # noqa: E402


def test_trial_response_context_prefers_canonical_edge_values():
    lang, source = bot.resolve_trial_response_context(
        requested_lang="ru",
        requested_source="trial_web",
        response={"lang": "en", "source": "web_signup"},
    )

    assert lang == "en"
    assert source == "web_signup"


def test_trial_response_context_rejects_invalid_edge_language():
    lang, source = bot.resolve_trial_response_context(
        requested_lang="en",
        requested_source="trial_web",
        response={"lang": "fr", "source": ""},
    )

    assert lang == "en"
    assert source == "trial_web"
