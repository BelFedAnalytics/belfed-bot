"""Parsing of paid-advertising deep-link payloads (Telegram Ads).

Paid campaigns point at the bot with a structured payload:

    https://t.me/BelfedBot?start=p-c_t-1_k-1_g-u_c-signals_stock

Why labelled segments instead of positions
------------------------------------------
The first iteration of this module was positional
(``<agency>_<pool>_<text>_<creative>_<channel>``). That collapsed as soon as the
agency added dimensions: targeting mode (Target Users / Target Channels) and
placement (channel feed / search / bots) are optional, so the index of a segment
no longer identifies it. Their own examples were mutually contradictory --
``ch_cr_t1_k1_users_namechannels`` contains both ``ch`` and ``users``, either of
which could be the targeting mode, and no parser can recover the intent because
the information simply is not in the string.

Worse than rejecting such a payload is *silently misreading* it: positional
parsing turned ``mag_pull_cr_t1_k1_users_namechannels`` into pool=pull,
text=cr, channel=k1_users_namechannels, i.e. plausible-looking garbage.

So each parameter now carries a one-letter label (``p-c``, ``t-1``, ``g-u``).
Order and arity stop mattering, and the agency can add dimensions without
coordinating a code change.

Robustness rules
----------------
*   **Nothing is ever dropped.** The raw payload is stored verbatim by the
    caller in ``trial_activation_attempts.source``; unrecognised segments are
    surfaced in ``unparsed`` so a convention drift shows up in reports instead
    of vanishing.
*   **Legacy positional payloads still parse.** Links may already have been
    generated in the agreed ``mag_<pool>_t<n>_k<n>_<channel>`` shape.
*   **Detection is deny-list based**, not allow-list: any payload that is not a
    reserved bot keyword counts as advertising. The previous allow-list on the
    ``mag`` prefix silently discarded the agency's own ``ch_...`` and
    ``pull_...`` examples, which is precisely the failure mode to avoid.
*   Telegram caps the start parameter at 1-64 characters from
    ``A-Z a-z 0-9 _ -`` (https://core.telegram.org/bots/api), which is why the
    labels and values are terse.
"""

from __future__ import annotations

import os
import re

# --- Telegram's own constraint on the /start payload ------------------------
_PAYLOAD_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_PAYLOAD_LEN = 64

# --- Labelled segment grammar ----------------------------------------------
# One letter, a hyphen, then the value: p-c, t-1, g-u, c-signals_stock
_LABEL_RE = re.compile(r"^([a-zA-Z])-(.+)$")

# label -> field name
_LABELS: dict[str, str] = {
    "a": "agency",         # agency / account
    "p": "pool",           # channel pool
    "t": "text_code",      # ad text variant
    "k": "creative_code",  # banner variant ("картинка")
    "g": "targeting",      # Target Users / Target Channels
    "s": "placement",      # channel feed / search / bots
    "c": "channel",        # channel username -- always last, may contain "_"
}

# Value dictionaries. Unknown values are kept verbatim rather than rejected:
# a report showing an unexpected code is useful, a silently dropped click is not.
_POOLS = {
    "s": "stocks",   "st": "stocks",   "cr": "crypto",
    "i": "invest",   "inv": "invest",  "c": "crypto",
}
_TARGETING = {
    "u": "users", "users": "users",
    "c": "channels", "ch": "channels", "channels": "channels",
}
_PLACEMENT = {
    "c": "channels", "ch": "channels", "channels": "channels",
    "s": "search",   "search": "search",
    "b": "bots",     "bot": "bots",    "bots": "bots",
}

# --- Reserved first segments ------------------------------------------------
# Deep-link payloads owned by other /start branches, or that intentionally fall
# through to the plain start flow. Anything else is treated as advertising.
RESERVED_PREFIXES: frozenset[str] = frozenset({
    "auth", "trial", "founding", "promo", "members", "member",
    "lang", "start", "help", "menu", "ref", "web",
})

# Legacy positional layout: <agency>_<pool>_t<n>_k<n>_<channel...>
_LEGACY_RE = re.compile(
    r"^(?P<agency>[A-Za-z0-9-]+)_(?P<pool>[A-Za-z]{1,4})_"
    r"(?P<text>t\d{1,3})_(?P<creative>k\d{1,3})_(?P<channel>.+)$"
)

_EMPTY_FIELDS = (
    "agency", "pool", "text_code", "creative_code",
    "targeting", "placement", "channel",
)

# Language markers accepted as a parameter value.
_LANG_CODES = {"ru": "ru", "en": "en"}

# Optional env override, kept for operational escape hatches.
_EXTRA_RESERVED = frozenset(
    p.strip().lower()
    for p in (os.getenv("AD_RESERVED_PREFIXES") or "").split(",")
    if p.strip()
)


def is_ad_payload(arg: str | None) -> bool:
    """True for payloads that should be attributed to a paid campaign.

    Deny-list rather than allow-list: the agency changes its naming without
    telling us, so an unrecognised-but-structured payload must still be
    captured. Reserved keywords owned by other /start branches are excluded, as
    are bare single-token payloads (``members``) and hex link tokens, which are
    handled earlier in the handler chain.
    """
    if not arg or not _PAYLOAD_RE.match(arg):
        return False

    parts = arg.split("_")
    if len(parts) < 2:
        # Single token: never an ad payload. Keeps "members", "trial",
        # "founding" and 32-char hex link tokens out.
        return False

    head = parts[0].lower()
    if head in RESERVED_PREFIXES or head in _EXTRA_RESERVED:
        return False

    # A hex link token cannot contain "_", so len(parts) >= 2 already excludes
    # it; guard anyway in case the token format ever changes.
    if re.fullmatch(r"[0-9a-f]{32,}", arg):
        return False

    return True


def parse_ad_payload(arg: str) -> dict[str, object]:
    """Decompose an ad payload into named components.

    Returns a dict that always contains ``raw``, every field in
    ``_EMPTY_FIELDS``, a ``scheme`` marker and an ``unparsed`` list. Fields the
    payload does not carry stay ``None`` -- absence is information, not an
    error.
    """
    result: dict[str, object] = {"raw": arg, "scheme": "unknown", "unparsed": []}
    for field in _EMPTY_FIELDS:
        result[field] = None

    if not is_ad_payload(arg):
        return result

    if _parse_labelled(arg, result):
        result["scheme"] = "labelled"
        return result

    if _parse_legacy(arg, result):
        result["scheme"] = "legacy"
        return result

    # Structured but unrecognised: keep every segment visible so the drift is
    # obvious in reporting rather than being mistaken for organic traffic.
    result["unparsed"] = arg.split("_")
    return result


def _parse_labelled(arg: str, result: dict[str, object]) -> bool:
    """Parse ``p-c_t-1_g-u_c-signals_stock``. True if any label was found.

    ``c-`` (channel) consumes the remainder of the payload, because channel
    usernames legally contain underscores (``signals_stock``,
    ``if_market_news``). Everything else is a single segment.
    """
    segments = arg.split("_")
    unparsed: list[str] = []
    found = False
    index = 0

    while index < len(segments):
        match = _LABEL_RE.match(segments[index])
        if not match:
            unparsed.append(segments[index])
            index += 1
            continue

        label, value = match.group(1).lower(), match.group(2)
        field = _LABELS.get(label)
        if field is None:
            unparsed.append(segments[index])
            index += 1
            continue

        found = True
        if field == "channel":
            # Rest of the payload is the username, separators included.
            result["channel"] = "_".join([value, *segments[index + 1:]])
            index = len(segments)
            continue

        result[field] = _normalise(field, value)
        index += 1

    if not found:
        return False
    result["unparsed"] = unparsed
    return True


def _parse_legacy(arg: str, result: dict[str, object]) -> bool:
    """Parse the original positional layout, for links already in the wild.

    Refuses the match when the trailing channel begins with a targeting or
    placement word. ``ch_cr_t1_k1_users_namechannels`` fits the positional shape
    arithmetically, but its tail is ``users_namechannels``: ``users`` is the
    targeting mode, not part of the username. Reporting the channel as
    ``users_namechannels`` would merge every campaign on that channel into a
    phantom entry, so an explicit "unparsed" is the safer outcome.
    """
    match = _LEGACY_RE.match(arg)
    if not match:
        return False

    tail_head = match.group("channel").split("_")[0].lower()
    if tail_head in _TARGETING or tail_head in _PLACEMENT:
        return False

    result["agency"] = match.group("agency").lower()
    result["pool"] = _normalise("pool", match.group("pool"))
    result["text_code"] = match.group("text").lower()
    result["creative_code"] = match.group("creative").lower()
    result["channel"] = match.group("channel")
    return True


def _normalise(field: str, value: str) -> str:
    """Map a short code to a canonical name, or keep it verbatim if unknown."""
    lowered = value.lower()
    if field == "pool":
        return _POOLS.get(lowered, lowered)
    if field == "targeting":
        return _TARGETING.get(lowered, lowered)
    if field == "placement":
        return _PLACEMENT.get(lowered, lowered)
    if field == "channel":
        return value  # case preserved -- usernames are displayed as typed
    return lowered


def detect_lang_from_ad_payload(parsed: dict[str, object]) -> str | None:
    """Language hint carried by the payload, or None.

    Reads the pool, text and creative codes only. The channel segment is
    deliberately ignored: a username such as ``profinansy_ru`` is a channel
    name, and letting it set the language would override what the creative
    actually targeted.
    """
    for key in ("pool", "text_code", "creative_code"):
        value = str(parsed.get(key) or "").lower()
        if value in _LANG_CODES:
            return _LANG_CODES[value]
    return None
