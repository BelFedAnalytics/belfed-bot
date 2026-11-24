"""Tests for trial-attribution forwarding into the bot-claim-trial edge function.

These verify the minimal contract added to `claim_trial_via_edge`:
  * a trial deep-link that carries an intent token forwards an `attribution_key`
    (the token) so the edge function can resolve web-origin attribution;
  * a direct trial (telegram_direct, no token) omits both `intent_token` and
    `attribution_key` — the pre-existing direct-trial contract is unchanged;
  * malformed/expired tokens do not change transport behaviour: the bot forwards
    whatever token it was given (validity is decided server-side) and returns the
    edge response as-is (or None on 5xx).

The test never asserts on logs beyond confirming no PII leaks (see
`test_no_pii_in_logs`): telegram_id, username, email and the token must not be
written to the log record produced by `claim_trial_via_edge`.
"""

import asyncio
import logging
import os

# bot.py reads a handful of env vars at import time; provide harmless test values
# before importing so the module loads in isolation (no network, no real bot).
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("SUPABASE_URL", "https://obujqvqqmyfcfflhqvud.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-key")
os.environ.setdefault("TELEGRAM_TRADING_CHANNEL_ID", "-1001234567890")
os.environ.setdefault("BOT_SHARED_SECRET", "test-shared-secret")

import bot  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.text = str(self._payload)

    def json(self):
        return self._payload


class _FakeClient:
    """Async-context-manager stand-in for httpx.AsyncClient that records the
    single POST body and returns a preconfigured response."""

    last_json = None
    last_url = None
    last_headers = None

    def __init__(self, response):
        self._response = response

    def __call__(self, *args, **kwargs):
        # httpx.AsyncClient(timeout=...) -> instance used as async ctx manager
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        type(self).last_url = url
        type(self).last_headers = headers
        type(self).last_json = json
        return self._response


def _run_claim(monkeypatch_response, **kwargs):
    """Run claim_trial_via_edge with httpx patched to a fake client; return
    (result, captured_json)."""
    fake = _FakeClient(monkeypatch_response)
    orig = bot.httpx.AsyncClient
    bot.httpx.AsyncClient = fake  # type: ignore[assignment]
    _FakeClient.last_json = None
    try:
        result = asyncio.run(bot.claim_trial_via_edge(**kwargs))
    finally:
        bot.httpx.AsyncClient = orig  # type: ignore[assignment]
    return result, _FakeClient.last_json


def test_deeplink_token_forwards_attribution_key():
    """A deep-link trial with an intent token forwards attribution_key == token."""
    token = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    result, body = _run_claim(
        _FakeResponse(200, {"ok": True, "invite_link": "https://t.me/+abc"}),
        telegram_id=42,
        username="alice",
        source="trial_web",
        lang="en",
        intent_token=token,
    )
    assert result == {"ok": True, "invite_link": "https://t.me/+abc"}
    assert body["attribution_key"] == token
    assert body["intent_token"] == token
    assert body["source"] == "trial_web"
    assert body["lang"] == "en"


def test_direct_trial_omits_attribution_key():
    """telegram_direct (no token) must not send intent_token or attribution_key."""
    result, body = _run_claim(
        _FakeResponse(200, {"ok": True}),
        telegram_id=7,
        username="bob",
        source="telegram_direct",
        lang="ru",
        intent_token=None,
    )
    assert result == {"ok": True}
    assert "attribution_key" not in body
    assert "intent_token" not in body
    # existing fields still present — backward compatible
    assert body["source"] == "telegram_direct"
    assert body["lang"] == "ru"
    assert body["telegram_id"] == "7"


def test_named_deeplink_without_token_omits_attribution_key():
    """Named deep-links (e.g. founding_en) carry a source label but no token,
    so attribution stays organic."""
    _, body = _run_claim(
        _FakeResponse(200, {"ok": True}),
        telegram_id=99,
        username=None,
        source="founding_en",
        lang="en",
    )
    assert "attribution_key" not in body
    assert "intent_token" not in body
    assert body["source"] == "founding_en"
    assert body["telegram_username"] == ""


def test_expired_or_invalid_token_behaviour_unchanged():
    """An expired/invalid token is still forwarded (validity is decided by the
    edge function); the 4xx error body is returned unchanged, and the attribution
    key is present exactly as for any tokened request."""
    token = "deadbeefdeadbeefdeadbeefdeadbeef"
    result, body = _run_claim(
        _FakeResponse(400, {"ok": False, "error": "intent_expired"}),
        telegram_id=1,
        username="carol",
        source="trial_web",
        lang="en",
        intent_token=token,
    )
    assert result == {"ok": False, "error": "intent_expired"}
    assert body["attribution_key"] == token


def test_5xx_returns_none_unchanged():
    """5xx from the edge function still yields None (pre-existing behaviour)."""
    result, _ = _run_claim(
        _FakeResponse(503, {"error": "upstream"}),
        telegram_id=1,
        username="dave",
        source="trial_web",
        lang="en",
        intent_token="c0ffeec0ffeec0ffeec0ffeec0ffeec0",
    )
    assert result is None


def test_no_pii_in_logs(caplog=None):
    """claim_trial_via_edge must not log telegram_id, username or the token."""
    token = "feedface00feedface00feedface0011"
    records = []

    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())
    bot.log.addHandler(handler)
    bot.log.setLevel(logging.DEBUG)
    try:
        _run_claim(
            _FakeResponse(200, {"ok": True}),
            telegram_id=555,
            username="secret_user",
            source="trial_web",
            lang="en",
            intent_token=token,
        )
    finally:
        bot.log.removeHandler(handler)

    joined = "\n".join(records)
    assert token not in joined
    assert "secret_user" not in joined
    assert "555" not in joined


if __name__ == "__main__":
    # Allow `python3 test_attribution.py` without pytest installed.
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    raise SystemExit(1 if failed else 0)
