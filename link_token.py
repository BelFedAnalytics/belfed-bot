"""Helpers for recognising web-first Telegram link tokens.

The website's telegram-link-start Edge Function issues single-use link tokens as
lowercase hex strings (18 random bytes -> 36 hex chars). The /start deep-link
carries them as `/start <token>`.

Named deep-link payloads such as `members_en`, `members_bottom_en`, `trial_link`
or `auth_xyz` must NOT be mistaken for tokens: the previous heuristic
(`len(arg) >= 16`) misclassified `members_bottom_en` (17 chars) as a token,
which produced a spurious "token invalid" reply. Matching hex-only fixes that
because named payloads contain letters outside a-f and/or underscores.
"""

import re

# 18-byte hex tokens are 36 chars; allow a small range to stay robust if the
# generator's byte length changes, while still excluding short named payloads.
_TOKEN_RE = re.compile(r"^[0-9a-f]{32,64}$")


def looks_like_link_token(arg: str | None) -> bool:
    """True only for site-generated hex link tokens; False for named payloads."""
    return bool(arg) and bool(_TOKEN_RE.match(arg))
