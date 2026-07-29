"""Parsing of paid-advertising deep-link payloads (Telegram Ads).

The media agency runs Telegram Ads campaigns that point at the bot with a
deep-link carrying a structured payload:

    https://t.me/BelfedBot?start=mag_cr_t1_k1_signals_stock

Layout is a fixed number of *service* segments followed by the channel
username, which may itself contain underscores:

    <agency>_<pool>_<text>_<creative>_<channel...>
      mag  _  cr  _  t1  _   k1     _ signals_stock

Because channel usernames such as ``signals_stock``, ``if_market_news`` or
``direct_investor`` contain underscores, the payload must NOT be split on every
separator. We split with a bounded ``maxsplit`` so everything after the fourth
separator is treated as the channel name verbatim.

Design notes
------------
*   The **raw payload is always preserved** by the caller and written to
    ``trial_activation_attempts.source`` / ``profiles.trial_source`` unchanged.
    This module only produces a *convenience* breakdown for logging; reporting
    re-parses the raw value in SQL, so the schema can change retroactively
    without losing historical attribution.
*   Telegram limits the start parameter to 1-64 characters from
    ``A-Z a-z 0-9 _ -`` (https://core.telegram.org/bots/api). Payloads that
    violate this cannot reach us in the first place, but we validate anyway so
    a malformed payload degrades to "not an ad payload" instead of raising.
*   Language is detected **only from the service segments**, never from the
    channel segment: channels like ``profinansy_ru`` would otherwise force RU
    onto an English-speaking user who clicked an EN creative.
"""

from __future__ import annotations

import os
import re

# Number of service segments before the channel username.
# MUST stay in sync with the agency's payload template. If the agency ever needs
# another dimension, extend an existing code (t1 -> t12) rather than adding a
# segment — changing this constant silently re-interprets historical payloads.
SERVICE_SEGMENTS = 4

# Agency prefixes that mark a payload as paid advertising. Comma-separated
# override via env so a new agency can be onboarded without a code deploy.
_DEFAULT_AD_PREFIXES = "mag"
AD_PREFIXES: frozenset[str] = frozenset(
    p.strip().lower()
    for p in (os.getenv("AD_PAYLOAD_PREFIXES") or _DEFAULT_AD_PREFIXES).split(",")
    if p.strip()
)

# Telegram's own constraint on the /start payload.
_PAYLOAD_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Language markers accepted inside the service segments.
_LANG_CODES = {"ru": "ru", "en": "en"}


def is_ad_payload(arg: str | None) -> bool:
    """True for well-formed paid-ad deep-link payloads.

    Requires a known agency prefix, Telegram-legal characters and at least one
    segment after the service block (the channel). Everything else — named
    payloads (``trial_home_hero``, ``founding_en``), hex link tokens, ``auth`` —
    returns False so existing /start branches keep their behaviour.
    """
    if not arg or not _PAYLOAD_RE.match(arg):
        return False
    parts = arg.split("_")
    if len(parts) < SERVICE_SEGMENTS + 1:
        return False
    if parts[0].lower() not in AD_PREFIXES:
        return False
    # Channel segment must be non-empty (guards against "mag_cr_t1_k1_").
    return bool("_".join(parts[SERVICE_SEGMENTS:]).strip())


def parse_ad_payload(arg: str) -> dict[str, str | None]:
    """Break an ad payload into its components.

    Returns a dict with the raw payload always present. Non-ad payloads yield
    every component as ``None`` so callers can log uniformly.

    >>> parse_ad_payload("mag_cr_t1_k1_signals_stock") == {
    ...     "raw": "mag_cr_t1_k1_signals_stock", "agency": "mag", "pool": "cr",
    ...     "text_code": "t1", "creative_code": "k1", "channel": "signals_stock"}
    True
    """
    empty: dict[str, str | None] = {
        "raw": arg,
        "agency": None,
        "pool": None,
        "text_code": None,
        "creative_code": None,
        "channel": None,
    }
    if not is_ad_payload(arg):
        return empty

    # maxsplit keeps underscores inside the channel username intact.
    agency, pool, text_code, creative_code, channel = arg.split("_", SERVICE_SEGMENTS)
    return {
        "raw": arg,
        "agency": agency.lower(),
        "pool": pool.lower(),
        "text_code": text_code.lower(),
        "creative_code": creative_code.lower(),
        "channel": channel,  # case preserved — Telegram usernames are displayed as-is
    }


def detect_lang_from_ad_payload(parsed: dict[str, str | None]) -> str | None:
    """Language hint from the *service* segments only, or None.

    Deliberately ignores ``channel``: usernames such as ``profinansy_ru`` or a
    hypothetical ``markets_en`` are channel names, not language markers, and
    must not override what the creative targeted.
    """
    for key in ("pool", "text_code", "creative_code"):
        value = (parsed.get(key) or "").lower()
        if value in _LANG_CODES:
            return _LANG_CODES[value]
    return None
