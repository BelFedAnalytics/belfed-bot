from pathlib import Path


EDGE_SOURCE = (
    Path(__file__).resolve().parent.parent
    / "supabase"
    / "functions"
    / "bot-claim-trial"
    / "index.ts"
)


def test_signed_web_intent_is_resolved_before_trial_claim():
    source = EDGE_SOURCE.read_text()

    intent_lookup = source.index('.from("trial_intents")')
    trial_claim = source.index('admin.rpc("claim_trial_by_telegram"')

    assert intent_lookup < trial_claim
    assert "lang = intent.lang === \"en\" ? \"en\" : \"ru\";" in source
    assert "p_email: trialIntent?.email ?? null" in source
    assert "p_consent_locale: trialIntent?.consent_locale ?? null" in source


def test_successful_signed_intent_is_consumed_and_returned_context_is_canonical():
    source = EDGE_SOURCE.read_text()

    assert "consumed_by_telegram_id" in source
    assert "consumed_by_user_id" in source
    assert "profile_id: result?.user_id ?? null" in source
    assert "source," in source
