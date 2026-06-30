"""
BelFed Analytics — Telegram bot (production, RU + EN multilingual).

Логика триала: 14 дней полного бесплатного доступа в платный канал —
без привязки карты. Юзер кликает t.me/BelfedBot?start=trial_xxx →
бот определяет язык (или спрашивает при первом /start) → выдаёт
одноразовый invite в RU или EN closed group. Через 24 часа после
конца триала cron telegram-enforce-access кикает неоплативших.

Платная подписка: 1 500 ₽ / мес, авто-продление, карты + SBP.
Перед оплатой бот спрашивает email — нужен для фискального чека (54-ФЗ).

ENV:
  TELEGRAM_BOT_TOKEN
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
  TELEGRAM_TRADING_CHANNEL_ID    -1003773738299  (RU paid community)
  TELEGRAM_COMMUNITY_RU_ID       -1003773738299  (alias of trading; RU)
  TELEGRAM_COMMUNITY_EN_ID       -1003869302680  (EN paid community)
  BELFED_WEB_URL                 https://belfed.ru
  BELFED_WEB_URL_EN              https://belfed.com
  BOT_SHARED_SECRET              shared secret for bot-claim-trial edge fn
  BOT_CLAIM_TRIAL_URL            optional override
  TELEGRAM_PREVIEW_CHANNEL_URL   optional, public preview channel link
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import string
from datetime import datetime, timedelta, timezone

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply, BotCommand, BotCommandScopeDefault
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ChatMemberHandler,
    MessageHandler, PreCheckoutQueryHandler, ContextTypes, filters,
)

import positions  # type: ignore  # local module — see positions.py

# ---------- Config ---------------------------------------------------------
BOT_TOKEN            = os.environ["TELEGRAM_BOT_TOKEN"]
SUPABASE_URL         = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
TRADING_CHANNEL_ID   = int(os.environ["TELEGRAM_TRADING_CHANNEL_ID"])  # RU paid
_ru_id = os.environ.get("TELEGRAM_COMMUNITY_RU_ID", "").strip()
COMMUNITY_RU_ID      = int(_ru_id) if _ru_id else TRADING_CHANNEL_ID
_en_id = os.environ.get("TELEGRAM_COMMUNITY_EN_ID", "").strip()
COMMUNITY_EN_ID      = int(_en_id) if _en_id else None
WEB_URL_RU           = os.environ.get("BELFED_WEB_URL",    "https://belfed.ru").rstrip("/")
WEB_URL_EN           = os.environ.get("BELFED_WEB_URL_EN", "https://belfed.com").rstrip("/")
TRIBUTE_RU_URL       = os.environ.get("TRIBUTE_RU_URL", "https://t.me/tribute/app?startapp=sXHG")
TRIBUTE_EN_URL       = os.environ.get("TRIBUTE_EN_URL", "https://t.me/tribute/app?startapp=sXIq")
PRICE_RUB            = os.environ.get("PRICE_MONTHLY_RUB", "1500")
PRICE_USD            = os.environ.get("PRICE_MONTHLY_USD", "15")
FM_PRICE_RUB         = os.environ.get("FM_PRICE_MONTHLY_RUB", "1050")   # Founding Member -30%
FM_PRICE_USD         = os.environ.get("FM_PRICE_MONTHLY_USD", "10.50")  # Founding Member -30%
BOT_SHARED_SECRET    = os.environ.get("BOT_SHARED_SECRET", "")
BOT_CLAIM_TRIAL_URL  = os.environ.get(
    "BOT_CLAIM_TRIAL_URL",
    f"{SUPABASE_URL}/functions/v1/bot-claim-trial",
)
YOOKASSA_CREATE_URL  = os.environ.get(
    "YOOKASSA_CREATE_PAYMENT_URL",
    f"{SUPABASE_URL}/functions/v1/yookassa-create-payment",
)
AUTH_MAGIC_URL       = os.environ.get(
    "AUTH_MAGIC_URL",
    f"{SUPABASE_URL}/functions/v1/auth-magic",
)
PREVIEW_CHANNEL_URL  = os.environ.get("TELEGRAM_PREVIEW_CHANNEL_URL", "").strip()

# Telegram Stars (EN only) ----------------------------------------------
# 956 Stars ≈ $15 buyer pays / ~$12.43 creator earns at $0.01569/Star. Period must be 2592000 (30d).
STARS_PRICE          = int(os.environ.get("STARS_PRICE_MONTHLY", "956"))
STARS_PERIOD_SECONDS = 2592000  # 30 days — the only allowed value for Stars subscriptions
TELEGRAM_API_BASE    = f"https://api.telegram.org/bot{BOT_TOKEN}"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("belfed-bot")

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

def is_valid_email(s: str | None) -> bool:
    if not s:
        return False
    s = s.strip()
    if s.lower().endswith("@belfed.local"):
        return False
    return bool(EMAIL_RE.match(s))

def is_ghost_email(s: str | None) -> bool:
    return bool(s) and s.lower().endswith("@belfed.local")

# ---------- Supabase REST helpers -----------------------------------------
SB_HEADERS = {
    "apikey":        SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type":  "application/json",
}

async def sb_get(path: str, params: dict | None = None):
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{SUPABASE_URL}{path}", headers=SB_HEADERS, params=params or {})
        if r.status_code == 200:
            return r.json()
        log.warning("sb_get %s %s -> %s %s", path, params, r.status_code, r.text[:200])
        return None

async def sb_post(path: str, body: dict):
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{SUPABASE_URL}{path}", headers=SB_HEADERS, json=body)
        try: data = r.json()
        except Exception: data = None
        return r.status_code, data

# ---------- Founding Members RPCs ----------------------------------------
async def resolve_payment_price_via_rpc(user_id: str) -> dict | None:
    """Возвращает актуальную цену для конкретного юзера с учётом founding/grace.
    Source of truth: pricing_config + pending_founding_claims.
    Returns dict с ключами: ok, amount_rub, amount_usd, amount_stars,
    founding_intent, already_founding, locale, allowed_provider,
    in_grace_period, discount_pct.
    Или None при ошибке транспорта."""
    status, data = await sb_post(
        "/rest/v1/rpc/resolve_payment_price",
        {"p_user_id": user_id},
    )
    if status not in (200, 204) or not isinstance(data, dict):
        log.error("resolve_payment_price RPC failed: %s %s", status, data)
        return None
    return data

async def ensure_pending_founding_claim_via_rpc(user_id: str, locale: str,
                                                 source: str | None = None) -> dict | None:
    """Создаёт/обновляет pending_founding_claims row на 24h.
    После этого resolve_payment_price вернёт founding-цену для данного юзера.
    Returns dict с ok/reason/already_founding/quota_remaining."""
    status, data = await sb_post(
        "/rest/v1/rpc/ensure_pending_founding_claim",
        {
            "p_user_id": user_id,
            "p_locale":  locale,
            "p_source":  source or "telegram",
        },
    )
    if status not in (200, 204) or not isinstance(data, dict):
        log.error("ensure_pending_founding_claim RPC failed: %s %s", status, data)
        return None
    return data

async def claim_founding_slot_via_rpc(user_id: str, locale: str,
                                       source: str | None = None,
                                       notes: str | None = None) -> dict | None:
    """Закрепляет founding-слот за юзером (атомарно). Вызывается после успешной оплаты.
    Returns dict с ok/reason/founding_member_no."""
    status, data = await sb_post(
        "/rest/v1/rpc/claim_founding_slot",
        {
            "p_user_id": user_id,
            "p_locale":  locale,
            "p_source":  source or "telegram",
            "p_notes":   notes,
        },
    )
    if status not in (200, 204) or not isinstance(data, dict):
        log.error("claim_founding_slot RPC failed: %s %s", status, data)
        return None
    return data

# ---------- Dashboard auth (TG direct-access) -----------------------------
AUTH_ISSUE_URL = os.environ.get(
    "AUTH_ISSUE_URL",
    f"{SUPABASE_URL}/functions/v1/auth-issue",
)
DASHBOARD_AUTH_URL = os.environ.get(
    "DASHBOARD_AUTH_URL",
    "https://belfed.com/auth",
)

async def check_paid_membership(user_id: int, lang: str) -> bool:
    """Returns True if user is a member of the language-appropriate paid chat."""
    chat_id = TRADING_CHANNEL_ID if lang == "ru" else (COMMUNITY_EN_ID or TRADING_CHANNEL_ID)
    if not chat_id:
        return False
    url = f"{TELEGRAM_API_BASE}/getChatMember"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json={"chat_id": chat_id, "user_id": user_id})
            j = r.json()
        if not j.get("ok"):
            log.info("getChatMember user=%s chat=%s -> %s", user_id, chat_id, j)
            return False
        status = (j.get("result") or {}).get("status")
        return status in ("member", "administrator", "creator")
    except Exception as e:
        log.error("check_paid_membership failed: %s", e)
        return False

async def issue_dashboard_session(tg_id: int, lang: str) -> str | None:
    """Calls auth-issue edge function. Returns one-time token or None."""
    if not BOT_SHARED_SECRET:
        log.error("BOT_SHARED_SECRET missing — cannot issue dashboard session")
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                AUTH_ISSUE_URL,
                headers={
                    "x-bot-secret": BOT_SHARED_SECRET,
                    "content-type": "application/json",
                },
                json={"tg_id": tg_id, "lang": lang},
            )
            j = r.json()
        if r.status_code == 200 and j.get("ok"):
            return j.get("one_time_token")
        log.warning("auth-issue %s -> %s %s", tg_id, r.status_code, j)
        return None
    except Exception as e:
        log.error("issue_dashboard_session failed: %s", e)
        return None

async def sb_patch(path: str, params: dict, body: dict) -> int:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.patch(
            f"{SUPABASE_URL}{path}",
            headers={**SB_HEADERS, "Prefer": "return=minimal"},
            params=params, json=body,
        )
        return r.status_code

# ---------- Data access ---------------------------------------------------
async def claim_trial_via_edge(telegram_id: int, username: str | None,
                                source: str = "telegram_direct",
                                lang: str = "ru",
                                intent_token: str | None = None) -> dict | None:
    """Создаёт lite-профиль с триалом и возвращает invite-ссылку (RU или EN).

    Если передан intent_token — это одноразовый токен, выданный веб-формой trial-intent-create,
    к которому привязан email + consent timestamps. edge-функция разрешит токен и пропишет их на профиль.
    """
    if not BOT_SHARED_SECRET:
        log.error("BOT_SHARED_SECRET is not set; cannot call bot-claim-trial")
        return None
    payload = {
        "telegram_id":       str(telegram_id),
        "telegram_username": username or "",
        "source":            source,
        "lang":              lang,
    }
    if intent_token:
        payload["intent_token"] = intent_token
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                BOT_CLAIM_TRIAL_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-bot-secret": BOT_SHARED_SECRET,
                },
                json=payload,
            )
            try:
                data = r.json()
            except Exception:
                data = None
            if r.status_code >= 500:
                log.error("bot-claim-trial 5xx [lang=%s]: %s %s", lang, r.status_code, r.text[:300])
                return None
            return data
    except Exception as e:
        log.error("claim_trial_via_edge failed [lang=%s]: %s", lang, e)
        return None

async def create_stars_invoice_link(profile_id: str, telegram_id: int, lang: str,
                                     founding: bool = False,
                                     amount_stars: int | None = None) -> str | None:
    """Создаёт invoice link для оплаты через Telegram Stars (recurring subscription).
    Bot API: createInvoiceLink. currency=XTR + subscription_period=2592000 → включает auto-renew.
    payload идёт обратно в SuccessfulPayment — по нему понимаем, кому выдавать доступ.

    Если founding=True — добавляем суффикс "|founding=1" в payload, чтобы на стороне
    on_successful_payment вызвать claim_founding_slot.
    amount_stars — переопределение цены (для founding передаём 669, иначе STARS_PRICE)."""
    payload = f"stars_sub|{profile_id}|{telegram_id}"
    if founding:
        payload += "|founding=1"
    price = amount_stars if amount_stars is not None else STARS_PRICE
    body = {
        "title":       "BelFed Premium" + (" · Founding" if founding else ""),
        "description": ("Monthly subscription to BelFed | Community: trade ideas, market "
                        "reviews and analytics from leading investment houses. Cancel anytime."),
        "payload":     payload,
        "currency":    "XTR",
        "prices":      [{"label": "BelFed Premium (1 month)", "amount": price}],
        "subscription_period": STARS_PERIOD_SECONDS,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{TELEGRAM_API_BASE}/createInvoiceLink", json=body)
            data = r.json()
            if not data.get("ok"):
                log.error("createInvoiceLink failed: %s", data)
                return None
            return data.get("result")
    except Exception as e:
        log.error("create_stars_invoice_link failed: %s", e)
        return None

async def apply_stars_payment_via_rpc(
    telegram_charge_id: str,
    user_id: str,
    amount_stars: int,
    subscription_expiration_date: int | None,
    paid_at: datetime,
    raw_event: dict,
    is_recurring: bool,
) -> bool:
    """Вызывает RPC apply_stars_payment — обновляет profiles + subscriptions + payments."""
    exp_iso = None
    if subscription_expiration_date:
        exp_iso = datetime.fromtimestamp(subscription_expiration_date, tz=timezone.utc).isoformat()
    status, _ = await sb_post(
        "/rest/v1/rpc/apply_stars_payment",
        {
            "p_telegram_charge_id":           telegram_charge_id,
            "p_user_id":                      user_id,
            "p_amount_stars":                 amount_stars,
            "p_subscription_expiration_date": exp_iso,
            "p_paid_at":                      paid_at.isoformat(),
            "p_raw":                          raw_event,
            "p_is_recurring":                 is_recurring,
        },
    )
    if status not in (200, 204):
        log.error("apply_stars_payment RPC failed: %s", status)
        return False
    return True

async def create_stars_upgrade_intent(profile_id: str, telegram_id: int, lang: str) -> str | None:
    """После Stars-оплаты, если у юзера ghost-email (@belfed.local), создаём
    intent-токен и возвращаем его. Юзер откроет /upgrade-email.html?stars_token=<TOKEN>
    и привяжет реальный email + согласие к существующему профилю.
    Single-use, 24h TTL."""
    # Generate 32-char [a-z0-9] token matching the format used by trial-intent-create.
    alphabet = string.ascii_lowercase + string.digits
    token = "".join(secrets.choice(alphabet) for _ in range(32))
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{SUPABASE_URL}/rest/v1/trial_intents",
                headers={**SB_HEADERS, "Prefer": "return=minimal"},
                json={
                    "token":       token,
                    "email":       None,
                    "lang":        lang,
                    "source":      "stars_upgrade",
                    "intent_type": "stars_upgrade",
                    "profile_id":  profile_id,
                    "expires_at":  expires_at,
                },
            )
            if r.status_code not in (200, 201, 204):
                log.error("create_stars_upgrade_intent failed: %s %s", r.status_code, r.text[:200])
                return None
            return token
    except Exception as e:
        log.error("create_stars_upgrade_intent error: %s", e)
        return None

async def create_payment_via_edge(user_id: str, email: str, return_url: str) -> dict | None:
    """Бот создаёт платёж через yookassa-create-payment (x-bot-secret авторизация).
    Передаёт email — нужен для фискального чека.
    Возвращает dict с confirmation_url или None при ошибке."""
    if not BOT_SHARED_SECRET:
        log.error("BOT_SHARED_SECRET is not set; cannot call yookassa-create-payment")
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                YOOKASSA_CREATE_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-bot-secret": BOT_SHARED_SECRET,
                },
                json={
                    "plan":       "month",
                    "user_id":    user_id,
                    "email":      email,
                    "return_url": return_url,
                },
            )
            try:
                data = r.json()
            except Exception:
                data = None
            if r.status_code == 409 and isinstance(data, dict) and \
               data.get("error") == "wrong_provider_for_founding":
                # Не ошибка, а бизнес-сигнал: founding-слот локнут под другой провайдер (Stars).
                # Возвращаем тело, чтобы caller показал юзеру правильный routing.
                return data
            if r.status_code >= 400:
                log.error("yookassa-create-payment %s: %s", r.status_code, r.text[:300])
                return None
            return data
    except Exception as e:
        log.error("create_payment_via_edge failed: %s", e)
        return None

async def set_user_language(telegram_id: int, lang: str) -> bool:
    """Сохраняем выбор языка пользователя через RPC set_user_language."""
    status, _ = await sb_post(
        "/rest/v1/rpc/set_user_language",
        {"p_telegram_id": str(telegram_id), "p_lang": lang},
    )
    return status in (200, 204)

async def update_profile_email(profile_id: str, email: str) -> bool:
    """Сохраняет email в profiles.email."""
    status = await sb_patch(
        "/rest/v1/profiles",
        params={"id": f"eq.{profile_id}"},
        body={"email": email, "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    return status in (200, 204)

async def link_telegram_email_via_rpc(telegram_id: int, email: str,
                                       username: str | None = None) -> dict | None:
    """Вызывает RPC link_telegram_email — привязывает telegram_id к профилю
    по email или обновляет ghost-email на реальный.
    Возвращает jsonb или None при сетевой ошибке."""
    status, data = await sb_post(
        "/rest/v1/rpc/link_telegram_email",
        {
            "p_telegram_id":       str(telegram_id),
            "p_email":             email,
            "p_telegram_username": username or None,
        },
    )
    if status not in (200, 204):
        log.error("link_telegram_email RPC failed: %s %s", status, data)
        return None
    return data if isinstance(data, dict) else None

async def grant_early_subscriber_bonus_via_rpc(profile_id: str) -> dict | None:
    """Начисляет раннему подписчику +7 дней (идемпотентно). Возвращает:
      {granted: bool, mode?: str, new_expires_at?: ts, reason?: str}.
    None при сетевой ошибке."""
    if not profile_id:
        return None
    status, data = await sb_post(
        "/rest/v1/rpc/grant_early_subscriber_bonus",
        {"p_profile_id": profile_id},
    )
    if status not in (200, 204):
        log.error("grant_early_subscriber_bonus RPC failed: %s %s", status, data)
        return None
    return data if isinstance(data, dict) else None

async def get_profile_by_telegram(telegram_id: int) -> dict | None:
    rows = await sb_get(
        "/rest/v1/profiles",
        params={"telegram_id": f"eq.{telegram_id}",
                "select": "id,email,telegram_id,lang,subscription_status,"
                          "subscription_plan,subscription_expires_at,trial_started_at"},
    )
    return rows[0] if rows else None

async def get_subscription(user_id: str) -> dict | None:
    rows = await sb_get(
        "/rest/v1/subscriptions",
        params={"user_id": f"eq.{user_id}",
                "select": "status,plan_code,current_period_end,cancel_at_period_end,payment_method_id",
                "order": "current_period_end.desc.nullslast"},
    )
    if not rows:
        return None
    for r in rows:
        if r.get("status") == "active":
            return r
    return rows[0]

def parse_ts(s: str | None):
    if not s: return None
    try: return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception: return None

# ---------- UI тексты: RU + EN -------------------------------------------
TEXTS_RU = {
    "welcome_new": (
        "// BELFED ANALYTICS · EST. 2025\n\n"
        "Добро пожаловать, {name}.\n\n"
        "Анализируем и работаем на финансовых рынках более 8 лет. "
        "Делимся торговыми идеями, обзорами рынков и аналитикой "
        "от ведущих инвест-домов.\n\n"
        "Акции · Криптовалюты · Сырьё · Валюты\n\n"
        "🎁 14 дней бесплатного доступа — без привязки карты.\n\n"
        f"После триала: подписка {PRICE_RUB} ₽ / мес, отмена в любой момент."
    ),
    "menu":            "// BELFED ANALYTICS\n\nРады видеть вас снова, {name}.",
    "trial_active":    ("🎁 Пробный доступ\nДействует до: {until}\nОсталось: {days} дн.\n\n"
                        f"Оформить подписку {FM_PRICE_RUB} ₽ / мес (−30%): " + TRIBUTE_RU_URL),
    "status_active":   ("✅ Подписка активна\n"
                        f"План: Founding Member ({FM_PRICE_RUB} ₽ / мес, −30%)\n"
                        "Действует до: {until}\n{autorenew}"),
    "status_none":     "❌ Подписки нет.\n\n" + f"Оформить ({FM_PRICE_RUB} ₽ / мес, −30%): " + TRIBUTE_RU_URL,
    "autorenew_on":    "Автопродление: включено",
    "autorenew_off":   "Автопродление: отключено · доступ закончится в указанную дату",
    "need_link":       "Сначала привяжите аккаунт сайта: " + WEB_URL_RU + "/members.html",
    "link_already":    "✅ Аккаунт уже привязан к этому Telegram. /status — посмотреть подписку.",
    "link_ok":         "✅ Telegram привязан. Бесплатный доступ открыт на 14 дней — без привязки карты.",
    "link_bad":        "⚠️ Токен недействителен или истёк. Сгенерируйте новый на сайте.",
    "cancel_ok":       "Автопродление отключено. Доступ сохранится до {until}.",
    "cancel_none":     "У вас нет активной подписки для отмены.",
    "btn_pay":         "⭐️ Founding Member — 1050 ₽ / мес (−30%)",
    "btn_paid":        "📺 Закрытый канал",
    "btn_status":      "📋 Моя подписка",
    "btn_disclaimer":  "⚠️ Disclaimer",
    "btn_request":     "Запросить актив",
    "btn_support":     "💬 Написать в поддержку",
    "freetext_fallback": ("Чтобы написать в поддержку — отправьте команду /support "
                          "или нажмите кнопку «💬 Написать в поддержку» в меню "
                          "(вызвать меню: /start)."),
    "btn_link":        "🔗 Привязать аккаунт",
    "btn_cabinet":     "🔐 Личный кабинет",
    "cabinet_new": (
        "🔐 Личный кабинет BelFed\n────────────────────────\n\n"
        "Нажмите, чтобы войти (ссылка действует ограниченное время и одноразовая):\n{link}\n\n"
        "После входа задайте пароль в настройках кабинета — и в следующий раз сможете заходить "
        "привычным способом: по email и паролю на " + WEB_URL_RU + "."
    ),
    "cabinet_has_pw": (
        "🔐 Личный кабинет BelFed\n────────────────────────\n\n"
        "Быстрый вход (ссылка одноразовая):\n{link}\n\n"
        "Также вы всегда можете войти по email и паролю на " + WEB_URL_RU + "."
    ),
    "cabinet_fail": (
        "⚠️ Не удалось создать ссылку для входа. Попробуйте ещё раз через минуту "
        "или войдите по email и паролю на " + WEB_URL_RU + "."
    ),
    "cabinet_no_profile": (
        "Сначала активируйте доступ — /start. После этого появится вход в личный кабинет."
    ),
    "no_access":       ("Сначала зарегистрируйтесь на " + WEB_URL_RU + " и привяжите Telegram. "
                        "Получите 14 дней бесплатного доступа — без привязки карты."),
    "trial_claim_ok": (
        "🎁 14 дней бесплатного доступа открыты.\n\n"
        "Ваша персональная ссылка в закрытый канал "
        "(одноразовая, действует 24 часа):\n{invite}\n\n"
        f"После триала: подписка {PRICE_RUB} ₽ / мес. "
        "Email понадобится только при первой оплате — для чека."
    ),
    "trial_claim_already_active": (
        "✅ У вас уже есть активный доступ.\n\n"
        "Свежая ссылка в закрытый канал "
        "(одноразовая, действует 24 часа):\n{invite}"
    ),
    "trial_claim_used": (
        "⚠️ Триал на этот Telegram-аккаунт уже был активирован ранее.\n\n"
        f"Чтобы продолжить, оформите подписку {PRICE_RUB} ₽ / мес: "
        + TRIBUTE_RU_URL
    ),
    "trial_claim_error": (
        "⚠️ Не удалось открыть триал. Попробуйте ещё раз через минуту "
        "или напишите нам через " + WEB_URL_RU + "/members.html"
    ),
    "disclaimer": (
        "// DISCLAIMER\n────────────────────────\n\n"
        "Вся информация публикуется исключительно в образовательных и аналитических целях.\n\n"
        "Не является финансовой консультацией или инвестиционной рекомендацией.\n\n"
        "Торговля финансовыми инструментами сопряжена с риском. "
        "Вы самостоятельно несёте ответственность за свои торговые решения.\n\n"
        "Прошлые результаты не гарантируют доходности в будущем.\n\n"
        "belfed.ru\n────────────────────────"
    ),
    "paid_invite_msg": "📺 Ваша ссылка в закрытый канал (1 час, одноразовая):\n{link}",
    "paid_invite_fail":"⚠️ Не удалось создать ссылку. Проверьте, что бот — администратор канала.",
    "lang_pick_title": "🌐 Choose language / Выберите язык:",
    "btn_lang_ru":     "🇷🇺 Русский",
    "btn_lang_en":     "🇬🇧 English",
    "lang_saved":      "✅ Язык: Русский",
    "ask_email": (
        "✉️ Укажите email для оплаты\n\n"
        "На него придёт фискальный чек (требование 54-ФЗ).\n"
        "Отправьте email одним сообщением, например: ivan@example.com\n\n"
        "Чтобы отменить — нажмите /cancel_payment"
    ),
    "email_invalid":   "⚠️ Это не похоже на корректный email. Попробуйте ещё раз или /cancel_payment",
    "email_saved":     "✅ Email сохранён: {email}",
    # ---- email-привязка (голый /start) ----
    "ask_email_link": (
        "✉️ Укажите ваш email\n\n"
        "Я свяжу этот Telegram с вашим профилем на сайте BelFed. "
        "Если у вас ещё нет профиля — он будет создан автоматически.\n\n"
        "Отправьте email одним сообщением, например: ivan@example.com\n\n"
        "Чтобы пропустить — /skip"
    ),
    "email_link_skipped": "Хорошо, пропустим. Email можно указать позже при оплате.",
    "email_link_invalid": "⚠️ Это не похоже на корректный email. Попробуйте ещё раз или /skip",
    "email_link_ok_linked": "✅ Telegram привязан к профилю {email}. Полный функционал сервиса открыт.",
    "email_link_ok_saved": "✅ Email сохранён: {email}. Привязка к профилю готова.",
    "email_link_taken": (
        "⚠️ Этот email уже связан с другим Telegram-аккаунтом.\n\n"
        "Если это ваш профиль — напишите нам через /start support, "
        "мы поможем перепривязать аккаунт."
    ),
    "email_link_reused": "✅ Спасибо, ваш email уже сохранён. Полный функционал сервиса открыт.",
    "email_link_no_profile": (
        "⚠️ Не удалось найти ваш профиль в базе.\n\n"
        "Пожалуйста, нажмите /start ещё раз — это создаст профиль, после чего я снова попрошу email."
    ),
    "email_link_race": (
        "⚠️ Этот email только что был привязан в другой сессии.\n\n"
        "Если это были вы — нажмите /start, доступ должен уже работать. "
        "Если нет — напишите в поддержку через /start → «Связаться с поддержкой»."
    ),
    "email_link_required": "⚠️ Email не получен. Отправьте его одним сообщением, например: ivan@example.com",
    "email_link_temporary": (
        "⚠️ Временная ошибка при сохранении email. Попробуйте ещё раз через минуту.\n\n"
        "Если повторится — напишите в поддержку через /start → «Связаться с поддержкой»."
    ),
    "start_temporary": (
        "⚠️ Временная заминка при создании профиля.\n\n"
        "Пожалуйста, нажмите /start ещё раз через минуту — обычно достаточно одной попытки. "
        "Если не получится — мы уже видим проблему и поможем."
    ),
    "early_bonus_granted": (
        "\n\n🎁 Спасибо, что были с нами с самого начала.\n"
        "В знак благодарности мы добавили **+7 дней** к вашей подписке.\n"
        "Новая дата окончания: *{new_expiry}*."
    ),
    "early_bonus_lifetime": (
        "\n\n🎁 Спасибо, что были с нами с самого начала.\n"
        "У вас бессрочный доступ — бонус +7 дней зафиксирован в профиле."
    ),
    "pay_canceled":    "Оплата отменена.",
    "pay_creating":    "⏳ Готовлю страницу оплаты…",
    "pay_link": (
        f"💳 Оплата подписки — {PRICE_RUB} ₽ / мес\n\n"
        "Перейдите по ссылке для оплаты (безопасная страница YooKassa):\n{url}\n\n"
        "После оплаты вернитесь в этот чат — я пришлю персональный invite в закрытый канал."
    ),
    "pay_error":       "⚠️ Не удалось создать платёж. Попробуйте через минуту.",
    "pay_no_profile":  "Сначала активируйте бесплатный доступ — /start",
    "btn_open_pay":    "💳 Открыть страницу оплаты",
    # Telegram Stars (RU-юзеры пока не используют, но переводы оставлены на будущее)
    "btn_pay_stars":   "⭐️ Founding Member — 1050 ₽ / мес (−30%)",
    "stars_creating":  "⏳ Готовлю счёт…",
    "stars_pay_link": (
        f"💳 BelFed Premium — ${PRICE_USD} / мес\n\n"
        "Нажмите кнопку ниже, чтобы оформить подписку. Оплата проходит "
        "через Telegram, без карты и дополнительной регистрации. "
        "Подписка автоматически продлевается каждые 30 дней — отменить "
        "можно в любой момент в настройках Telegram.\n\n"
        "После оплаты я пришлю персональную ссылку в закрытый канал."
    ),
    "btn_open_stars_pay": f"💳 Оформить — ${PRICE_USD} / мес",
    "stars_invoice_hint": (
        "⏱ Ссылка на оплату действует несколько минут — это особенность Telegram Stars.\n"
        "Если Telegram показывает «срок действия истёк» — нажмите «🔄 Обновить ссылку»."
    ),
    "btn_refresh_invoice": "🔄 Обновить ссылку",
    "stars_payment_received": (
        "✅ Оплата получена! Спасибо.\n\n"
        "Подписка активна до: {until}\n"
        "Автопродление: включено\n\n"
        "Ваша персональная ссылка в закрытый канал "
        "(одноразовая, действует 1 час):\n{invite}"
    ),
    "stars_payment_no_invite": (
        "✅ Оплата получена! Подписка активна до: {until}\n\n"
        "Не удалось автоматически создать ссылку в канал. "
        "Нажмите «📺 Закрытый канал» в меню — я выдам её."
    ),
}

TEXTS_EN = {
    "welcome_new": (
        "// BELFED ANALYTICS · EST. 2025\n\n"
        "Welcome, {name}.\n\n"
        "We've been analyzing and trading financial markets for over 8 years. "
        "We share trade ideas, market reviews and research from leading "
        "investment houses.\n\n"
        "Equities · Crypto · Commodities · FX\n\n"
        "🎁 14 days of free access — no card required.\n\n"
        f"After the trial: ${PRICE_USD} / month, cancel anytime."
    ),
    "menu":            "// BELFED ANALYTICS\n\nGood to see you again, {name}.",
    "trial_active":    ("🎁 Trial access\nValid until: {until}\nDays left: {days}\n\n"
                        f"Subscribe (${FM_PRICE_USD} / mo, −30%): " + TRIBUTE_EN_URL),
    "status_active":   ("✅ Subscription active\n"
                        f"Plan: Founding Member (${FM_PRICE_USD} / mo, −30%)\n"
                        "Valid until: {until}\n{autorenew}"),
    "status_none":     "❌ No active subscription.\n\n" + f"Subscribe (${FM_PRICE_USD} / mo, −30%): " + TRIBUTE_EN_URL,
    "autorenew_on":    "Auto-renew: on",
    "autorenew_off":   "Auto-renew: off · access ends on the date above",
    "need_link":       "Please link your account first: " + WEB_URL_EN + "/members.html",
    "link_already":    "✅ Your account is already linked to this Telegram. Use /status to view subscription.",
    "link_ok":         "✅ Telegram linked. 14 days of free access — no card required.",
    "link_bad":        "⚠️ Token invalid or expired. Generate a new one on the site.",
    "cancel_ok":       "Auto-renew disabled. Access remains until {until}.",
    "cancel_none":     "You don't have an active subscription to cancel.",
    "btn_pay":         "⭐️ Founding Member — $10.50 / mo (−30%)",
    "btn_paid":        "📺 Private channel",
    "btn_status":      "📋 My subscription",
    "btn_disclaimer":  "⚠️ Disclaimer",
    "btn_request":     "Request asset",
    "btn_support":     "💬 Contact support",
    "freetext_fallback": ("To contact support — send the /support command "
                          "or tap the \"💬 Contact support\" button in the menu "
                          "(open the menu with /start)."),
    "btn_link":        "🔗 Link account",
    "btn_cabinet":     "🔐 Member dashboard",
    "cabinet_new": (
        "🔐 Your BelFed dashboard\n────────────────────────\n\n"
        "Tap to sign in (single-use link, valid for a limited time):\n{link}\n\n"
        "Once inside, set a password in your dashboard settings — next time you can sign in "
        "the usual way with your email and password at " + WEB_URL_EN + "."
    ),
    "cabinet_has_pw": (
        "🔐 Your BelFed dashboard\n────────────────────────\n\n"
        "Quick sign-in (single-use link):\n{link}\n\n"
        "You can also always sign in with your email and password at " + WEB_URL_EN + "."
    ),
    "cabinet_fail": (
        "⚠️ Couldn't create your sign-in link. Try again in a minute "
        "or sign in with your email and password at " + WEB_URL_EN + "."
    ),
    "cabinet_no_profile": (
        "Activate access first — /start. Your dashboard sign-in will appear after that."
    ),
    "no_access":       ("Please sign up at " + WEB_URL_EN + " and link Telegram first. "
                        "Get 14 days of free access — no card required."),
    "trial_claim_ok": (
        "🎁 14 days of free access unlocked.\n\n"
        "Your personal invite to the private channel "
        "(single-use, valid 24 hours):\n{invite}\n\n"
        f"After the trial: ${PRICE_USD} / month. "
        "Email only at first payment — for the receipt."
    ),
    "trial_claim_already_active": (
        "✅ You already have active access.\n\n"
        "Fresh invite to the private channel "
        "(single-use, valid 24 hours):\n{invite}"
    ),
    "gift7_ok": (
        "🎁 Gift for our early community: 7 days of access unlocked.\n\n"
        "Your personal invite to the private channel "
        "(single-use, valid 24 hours):\n{invite}\n\n"
        "After 7 days: subscribe whenever you wish. No auto-charges."
    ),
    "gift7_already_active": (
        "✅ You already have active access — no gift needed.\n\n"
        "Fresh invite link to the private channel "
        "(single-use, valid 24 hours):\n{invite}"
    ),
    "gift7_already_used": (
        "⚠️ You've already used your trial access in the past.\n\n"
        "Activate a subscription from the menu below — we'll bring you back."
    ),
    "trial_claim_used": (
        "⚠️ The trial for this Telegram account has already been used.\n\n"
        f"To continue, subscribe (${PRICE_USD} / mo): "
        + TRIBUTE_EN_URL
    ),
    "trial_claim_error": (
        "⚠️ Couldn't open the trial. Please try again in a minute "
        "or contact us via " + WEB_URL_EN + "/members.html"
    ),
    "disclaimer": (
        "// DISCLAIMER\n────────────────────────\n\n"
        "All information is published for educational and analytical purposes only.\n\n"
        "Not financial advice or an investment recommendation.\n\n"
        "Trading financial instruments involves risk. "
        "You are solely responsible for your trading decisions.\n\n"
        "Past performance does not guarantee future results.\n\n"
        "belfed.com\n────────────────────────"
    ),
    "paid_invite_msg": "📺 Your invite to the private channel (1 hour, single-use):\n{link}",
    "paid_invite_fail":"⚠️ Couldn't create invite. Make sure the bot is admin of the channel.",
    "lang_pick_title": "🌐 Choose language / Выберите язык:",
    "btn_lang_ru":     "🇷🇺 Русский",
    "btn_lang_en":     "🇬🇧 English",
    "lang_saved":      "✅ Language: English",
    "ask_email": (
        "✉️ Please enter your email\n\n"
        "We need it for the fiscal receipt (Russian tax law requirement).\n"
        "Send your email in one message, e.g.: ivan@example.com\n\n"
        "To cancel — tap /cancel_payment"
    ),
    "email_invalid":   "⚠️ That doesn't look like a valid email. Try again or /cancel_payment",
    "email_saved":     "✅ Email saved: {email}",
    # ---- email link (bare /start) ----
    "ask_email_link": (
        "✉️ Please share your email\n\n"
        "I'll link this Telegram to your BelFed site profile. "
        "If you don't have one yet — it will be created automatically.\n\n"
        "Send your email in a single message, e.g. ivan@example.com\n\n"
        "To skip — /skip"
    ),
    "email_link_skipped": "No problem — you can add an email later at checkout.",
    "email_link_invalid": "⚠️ That doesn't look like a valid email. Try again or /skip",
    "email_link_ok_linked": "✅ Telegram linked to profile {email}. Full access is unlocked.",
    "email_link_ok_saved": "✅ Email saved: {email}. Profile is fully linked.",
    "email_link_taken": (
        "⚠️ This email is already linked to another Telegram account.\n\n"
        "If it's your profile — message us via /start support and we'll help relink it."
    ),
    "email_link_reused": "✅ Thanks — your email is already on file. Full access is unlocked.",
    "email_link_no_profile": (
        "⚠️ We couldn't find your profile in the database.\n\n"
        "Please press /start again — this will create a profile, then I'll ask for your email."
    ),
    "email_link_race": (
        "⚠️ This email was just linked in another session.\n\n"
        "If that was you — press /start, access should already work. "
        "If not — contact support via /start → ‘Contact support’."
    ),
    "email_link_required": "⚠️ Email not received. Send it in a single message, e.g. ivan@example.com",
    "email_link_temporary": (
        "⚠️ Temporary error while saving email. Please try again in a minute.\n\n"
        "If it persists — contact support via /start → ‘Contact support’."
    ),
    "start_temporary": (
        "⚠️ Temporary hiccup while creating your profile.\n\n"
        "Please press /start once more in a minute — usually a single retry is enough. "
        "If it still fails, we can see the issue on our side and will help."
    ),
    "early_bonus_granted": (
        "\n\n🎁 Thanks for being with us since the beginning.\n"
        "As a token of appreciation, we've added **+7 days** to your subscription.\n"
        "New end date: *{new_expiry}*."
    ),
    "early_bonus_lifetime": (
        "\n\n🎁 Thanks for being with us since the beginning.\n"
        "You have lifetime access — the +7-day bonus is recorded in your profile."
    ),
    "pay_canceled":    "Payment canceled.",
    "pay_creating":    "⏳ Preparing payment page…",
    "pay_link": (
        f"💳 Subscription — {PRICE_RUB} RUB / month (~${PRICE_USD})\n\n"
        "Open the secure YooKassa payment page:\n{url}\n\n"
        "After payment, return to this chat — I'll send your personal invite to the private channel."
    ),
    "pay_error":       "⚠️ Couldn't create payment. Please try again in a minute.",
    "pay_no_profile":  "Activate the free trial first — /start",
    "btn_open_pay":    "💳 Open payment page",
    # Telegram Stars (EN users)
    "btn_pay_stars":   "⭐️ Founding Member — $10.50 / mo (−30%)",
    "stars_creating":  "⏳ Preparing your invoice…",
    "stars_pay_link": (
        f"💳 BelFed Premium — ${PRICE_USD} / month\n\n"
        "Tap the button below to subscribe. Payment is processed securely "
        "through Telegram — no card or extra registration required. "
        "The subscription auto-renews every 30 days and can be cancelled "
        "anytime in your Telegram settings.\n\n"
        "After payment I'll send your personal invite to the private channel."
    ),
    "btn_open_stars_pay": f"💳 Subscribe — ${PRICE_USD} / mo",
    "stars_invoice_hint": (
        "⏱ This payment link is valid only for a few minutes — that's how Telegram Stars work.\n"
        "If Telegram says “payment link expired”, tap “🔄 Refresh link” below."
    ),
    "btn_refresh_invoice": "🔄 Refresh link",
    "stars_payment_received": (
        "✅ Payment received. Thank you!\n\n"
        "Subscription active until: {until}\n"
        "Auto-renew: on\n\n"
        "Your personal invite to the private channel "
        "(single-use, valid 1 hour):\n{invite}"
    ),
    "stars_payment_no_invite": (
        "✅ Payment received. Subscription active until: {until}\n\n"
        "Couldn't create the channel invite automatically. "
        "Tap “📺 Private channel” in the menu — I'll send it."
    ),
}

def T(lang: str, key: str) -> str:
    """Достаёт строку из правильного словаря, fallback на RU."""
    book = TEXTS_EN if lang == "en" else TEXTS_RU
    return book.get(key) or TEXTS_RU.get(key, key)

# ---------- Language detection -------------------------------------------
def detect_lang_from_source(source: str | None) -> str | None:
    """trial_home_hero_en → 'en'; trial_xxx_ru → 'ru'; иначе None."""
    if not source:
        return None
    s = source.lower()
    if s.endswith("_en") or "_en_" in s:
        return "en"
    if s.endswith("_ru") or "_ru_" in s:
        return "ru"
    return None

def detect_lang_from_telegram(update: Update) -> str:
    """Fallback по language_code Telegram-клиента."""
    code = (update.effective_user.language_code or "").lower() if update.effective_user else ""
    return "en" if code.startswith("en") else "ru"

async def get_user_lang(update: Update, profile: dict | None = None) -> str:
    """Приоритет: profiles.lang → Telegram language_code → 'ru'."""
    if profile and profile.get("lang") in ("ru", "en"):
        return profile["lang"]
    return detect_lang_from_telegram(update)

# ---------- Helpers -------------------------------------------------------
async def grant_paid_invite(context: ContextTypes.DEFAULT_TYPE,
                             telegram_id: int, lang: str) -> str | None:
    """Одноразовая invite-ссылка в закрытый канал (RU или EN). Действует 1 час."""
    chat_id = COMMUNITY_EN_ID if (lang == "en" and COMMUNITY_EN_ID) else COMMUNITY_RU_ID
    try:
        try:
            await context.bot.unban_chat_member(chat_id, telegram_id, only_if_banned=True)
        except Exception:
            pass
        inv = await context.bot.create_chat_invite_link(
            chat_id,
            member_limit=1,
            expire_date=int(datetime.now().timestamp()) + 3600,
            name=f"tg:{telegram_id}",
        )
        return inv.invite_link
    except Exception as e:
        log.error("grant_paid_invite failed [lang=%s, chat=%s]: %s", lang, chat_id, e)
        return None

async def issue_cabinet_link(telegram_id: int, lang: str) -> dict | None:
    """Calls the auth-magic edge function to get a Supabase magic-link for the
    member dashboard. Returns {action_link, has_password, display_name, lang}
    or None on failure."""
    if not BOT_SHARED_SECRET:
        log.error("BOT_SHARED_SECRET is not set; cannot call auth-magic")
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                AUTH_MAGIC_URL,
                headers={
                    "Content-Type":  "application/json",
                    "x-bot-secret":  BOT_SHARED_SECRET,
                    # anon Authorization required by the functions gateway
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                    "apikey":        SUPABASE_SERVICE_KEY,
                },
                json={"tg_id": telegram_id, "lang": lang},
            )
        data = r.json()
        if r.status_code == 200 and data.get("ok"):
            return data
        log.warning("auth-magic -> %s %s", r.status_code, str(data)[:200])
        return None
    except Exception as e:
        log.error("issue_cabinet_link failed [tg=%s]: %s", telegram_id, e)
        return None

async def send_cabinet_link(target, telegram_id: int, lang: str) -> None:
    """Issues a cabinet magic-link and sends the appropriate message to the user.
    `target` is a message/callback message object with reply_text."""
    res = await issue_cabinet_link(telegram_id, lang)
    if not res:
        await target.reply_text(T(lang, "cabinet_fail"), disable_web_page_preview=True)
        return
    link = res.get("action_link") or ""
    key = "cabinet_has_pw" if res.get("has_password") else "cabinet_new"
    await target.reply_text(
        T(lang, key).format(link=link),
        disable_web_page_preview=True,
    )

def has_access(profile: dict | None) -> bool:
    if not profile: return False
    if profile.get("subscription_status") == "admin": return True
    exp = parse_ts(profile.get("subscription_expires_at"))
    return bool(exp and exp > datetime.now(timezone.utc))

def is_admin(profile: dict | None) -> bool:
    return bool(profile and profile.get("subscription_status") == "admin")

def lang_pick_keyboard(payload_prefix: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора языка. payload_prefix передаёт контекст в callback."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(TEXTS_RU["btn_lang_ru"], callback_data=f"{payload_prefix}|ru"),
        InlineKeyboardButton(TEXTS_EN["btn_lang_en"], callback_data=f"{payload_prefix}|en"),
    ]])

# ---------- Trial flow (общая для start и для callback) ------------------
async def run_trial_flow(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         user_id: int, username: str | None,
                         source: str, lang: str,
                         reply_target,
                         intent_token: str | None = None):
    """Вызывает edge-функцию и шлёт ответ + меню. reply_target — message или query.message."""
    res = await claim_trial_via_edge(user_id, username, source=source, lang=lang,
                                      intent_token=intent_token)
    if res is None:
        await reply_target.reply_text(T(lang, "trial_claim_error"))
        await send_main_menu(update, context, lang=lang, reply_to=reply_target)
        return

    if res.get("ok"):
        invite = res.get("invite_link") or "—"
        if res.get("already_active"):
            await reply_target.reply_text(
                T(lang, "trial_claim_already_active").format(invite=invite),
                disable_web_page_preview=True,
            )
        else:
            await reply_target.reply_text(
                T(lang, "trial_claim_ok").format(invite=invite),
                disable_web_page_preview=True,
            )
        # Web-dashboard follow-up: приглашаем юзера зарегистрироваться
        # на сайте, чтобы получить доступ к веб-лК (привязка TG↔веб
        # произойдёт автоматически через merge_lite_into_full).
        try:
            if lang == "ru":
                wa_url = "https://belfed.ru/members.html"
                wa_btn = "🌐 Открыть belfed.ru/members.html"
                wa_txt = (
                    "🌐 Для регистрации в личном кабинете на сайте — откройте belfed.ru/members.html\n"
                    "Для входа нужно зарегистрироваться по email.\n"
                    "Регистрация на сайте автоматически свяжется с вашим Telegram."
                )
            else:
                wa_url = "https://belfed.com/members.html"
                wa_btn = "🌐 Open belfed.com/members.html"
                wa_txt = (
                    "🌐 To set up your web dashboard — open belfed.com/members.html\n"
                    "Sign up with your email. Registration will automatically link to your Telegram."
                )
            await reply_target.reply_text(
                wa_txt,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(wa_btn, url=wa_url)]]),
                disable_web_page_preview=True,
            )
        except Exception as e:
            log.warning("trial-flow web-account follow-up failed: %s", e)

        # Founding follow-up: если юзер пришёл по founding deep-link,
        # предлагаем сразу оформить founding-подписку по скидке 30% навсегда.
        # Кнопка ведёт на тот же deep-link — сейчас у юзера уже есть
        # профиль после триала, поэтому сработает Сценарий A:
        #   ensure_pending_founding_claim → resolve_payment_price → invoice.
        if source and source.startswith("founding"):
            try:
                bot_username = (context.bot.username or "BelfedBot")
                if lang == "ru":
                    fnd_url = f"https://t.me/{bot_username}?start=founding_ru"
                    fnd_btn = "✨ Оформить Founding 1050₽/мес"
                    fnd_txt = (
                        "✨ Готовы закрепить founding-цену?\n\n"
                        f"1050₽/мес вместо {PRICE_RUB}₽/мес — скидка 30% остаётся навсегда, "
                        "даже когда стандартная цена сервиса вырастет.\n"
                        "Оплата картой через YooKassa (Мир / SBP / SberPay / T-Pay / ЮMoney)."
                    )
                else:
                    fnd_url = f"https://t.me/{bot_username}?start=founding_en"
                    fnd_btn = "✨ Claim Founding $10.50/mo"
                    fnd_txt = (
                        "✨ Ready to lock in the founding price?\n"
                        "\n"
                        f"$10.50/mo instead of ${PRICE_USD}/mo — 30% off forever, "
                        "even when our standard price changes later.\n"
                        "\n"
                        "Plus: 3 asset requests per day (priority), founding status "
                        "tied to your account, pause anytime with 60 days to come back.\n"
                        "\n"
                        "Tap the button below to open the in-bot payment screen."
                    )
                await reply_target.reply_text(
                    fnd_txt,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(fnd_btn, url=fnd_url)]]),
                    disable_web_page_preview=True,
                )
            except Exception as e:
                log.warning("trial-flow founding follow-up failed: %s", e)
    else:
        err = res.get("error")
        if err == "trial_already_used":
            await reply_target.reply_text(T(lang, "trial_claim_used"))
        else:
            log.warning("claim_trial returned error [lang=%s]: %s", lang, res)
            await reply_target.reply_text(T(lang, "trial_claim_error"))
    await send_main_menu(update, context, lang=lang, reply_to=reply_target)

# ---------- Gift7 flow (channel re-engagement) ---------------------------
async def run_gift7_flow(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          user_id: int, username: str | None,
                          lang: str, source: str, reply_target):
    """Activate a 7-day gift trial for the user.

    NOTE: this is a DELIBERATELY separate offer from the canonical 14-day BelFed
    trial (website funnel / `trial_*` deep-links / bot-claim-trial edge fn). It is
    a shorter channel re-engagement gift reached only via the `gift7` deep-link,
    so its 7-day window is intentional and must NOT be bumped to 14 when aligning
    the canonical trial copy.

    Three cases (handled by claim_trial_by_telegram RPC):
      1) No profile or profile without trial   -> create/activate 7-day trial + invite
      2) Already active/admin                  -> just re-issue fresh invite link
      3) Trial already used                    -> show "already used" message

    Architectural notes:
      * RPC creates lite profile if needed; invite is created by bot itself
        (via Bot API) using grant_paid_invite — same machinery as paid_invite.
      * Source label distinguishes channel-post gifts from website trials
        for later attribution analysis.
    """
    status, res = await sb_post(
        "/rest/v1/rpc/claim_trial_by_telegram",
        {
            "p_telegram_id":       str(user_id),
            "p_telegram_username": username or "",
            "p_trial_days":        7,
            "p_source":            source,
            "p_lang":              lang,
        },
    )
    if status not in (200, 204) or not isinstance(res, dict):
        log.error("gift7: RPC failed status=%s body=%s", status, res)
        await reply_target.reply_text(T(lang, "trial_claim_error"))
        await send_main_menu(update, context, lang=lang, reply_to=reply_target)
        return

    # Effective language: DB may override (existing profile with saved lang)
    eff_lang = (res.get("lang") or lang)
    if eff_lang not in ("ru", "en"):
        eff_lang = lang

    if res.get("ok"):
        if res.get("already_active"):
            invite = await grant_paid_invite(context, user_id, eff_lang)
            await reply_target.reply_text(
                T(eff_lang, "gift7_already_active").format(invite=invite or "—"),
                disable_web_page_preview=True,
            )
        else:
            invite = await grant_paid_invite(context, user_id, eff_lang)
            await reply_target.reply_text(
                T(eff_lang, "gift7_ok").format(invite=invite or "—"),
                disable_web_page_preview=True,
            )
    else:
        err = res.get("error")
        if err == "trial_already_used":
            await reply_target.reply_text(T(eff_lang, "gift7_already_used"))
        else:
            log.warning("gift7 RPC returned error [lang=%s]: %s", eff_lang, res)
            await reply_target.reply_text(T(eff_lang, "trial_claim_error"))
    await send_main_menu(update, context, lang=eff_lang, reply_to=reply_target)


# ---------- Promo code win-back flow ------------------------------------
async def run_promo_flow(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          user_id: int, promo_code: str, lang: str,
                          reply_target):
    """Activates a win-back promo code via bot-claim-trial (mode=promo).
    Resets trial_claim_used, extends trial_end by extra_days, issues invite."""
    if not BOT_SHARED_SECRET:
        log.error("BOT_SHARED_SECRET is not set; cannot call bot-claim-trial (promo)")
        await reply_target.reply_text(T(lang, "trial_claim_error"))
        await send_main_menu(update, context, lang=lang, reply_to=reply_target)
        return

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{SUPABASE_URL}/functions/v1/bot-claim-trial",
                headers={
                    "Content-Type":  "application/json",
                    "x-bot-secret":  BOT_SHARED_SECRET,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                    "apikey":        SUPABASE_SERVICE_KEY,
                },
                json={
                    "mode":        "promo",
                    "telegram_id": str(user_id),
                    "promo_code":  promo_code,
                    "lang":        lang,
                },
            )
        res = r.json()
    except Exception as e:
        log.error("run_promo_flow failed [code=%s]: %s", promo_code, e)
        await reply_target.reply_text(T(lang, "trial_claim_error"))
        await send_main_menu(update, context, lang=lang, reply_to=reply_target)
        return

    if r.status_code == 200 and res.get("ok"):
        invite    = res.get("invite_link") or "—"
        trial_end = res.get("trial_end", "")
        extra     = res.get("extra_days", 7)
        try:
            until_str = datetime.fromisoformat(trial_end.replace("Z", "+00:00")).strftime("%d.%m.%Y")
        except Exception:
            until_str = trial_end[:10] if trial_end else "—"

        if lang == "en":
            msg = (
                f"🎁 Your exclusive 7-day access has been activated!\n\n"
                f"Your personal invite to the private analytics channel\n"
                f"(single-use link, valid 24 hours):\n{invite}\n\n"
                f"Access valid until: {until_str}\n\n"
                f"After your bonus period you can lock in the Founding Member rate — "
                f"$10.50/mo permanently."
            )
        else:
            msg = (
                f"🎁 Ваш эксклюзивный доступ на 7 дней активирован!\n\n"
                f"Личное приглашение в закрытый аналитический канал\n"
                f"(одноразовая ссылка, действует 24 часа):\n{invite}\n\n"
                f"Доступ активен до: {until_str}\n\n"
                f"После бонусного периода вы сможете закрепить ставку Founding Member — "
                f"1 050₽/мес навсегда."
            )
        await reply_target.reply_text(msg, disable_web_page_preview=True)
    elif r.status_code == 409 and res.get("error") == "promo_already_used":
        if lang == "en":
            await reply_target.reply_text(
                "⚠️ This promo code has already been used. "
                "If you think this is a mistake — contact support via /start."
            )
        else:
            await reply_target.reply_text(
                "⚠️ Этот промокод уже был использован. "
                "Если это ошибка — напишите в поддержку через /start."
            )
    elif r.status_code == 404 and res.get("error") == "profile_not_found":
        # User has no profile — prompt them to start a trial first
        if lang == "en":
            await reply_target.reply_text(
                "⚠️ It looks like you don't have a BelFed profile yet.\n"
                "Please use /start to activate your free trial first."
            )
        else:
            await reply_target.reply_text(
                "⚠️ Похоже, у вас ещё нет профиля BelFed.\n"
                "Пожалуйста, используйте /start для активации бесплатного пробного доступа."
            )
    else:
        log.warning("run_promo_flow unexpected response [code=%s]: %s %s", promo_code, r.status_code, res)
        await reply_target.reply_text(T(lang, "trial_claim_error"))

    await send_main_menu(update, context, lang=lang, reply_to=reply_target)


# ---------- Payment flow with email collection ---------------------------
async def start_payment_with_email(query_or_message, context: ContextTypes.DEFAULT_TYPE,
                                    profile: dict, lang: str,
                                    provider: str = "yookassa",
                                    telegram_id: int | None = None):
    # TRIBUTE intercept: all payment now flows through Tribute Founding Member.
    # Canonical BelFed trial = 14 days (EN + RU must match the website funnel).
    # NOTE: the *actual* free-trial length is configured per-subscription inside
    # the Tribute admin panel (RU startapp=sXHG, EN startapp=sXIq), not here — the
    # bot only describes it. Both Tribute subscriptions must have their free-trial
    # period set to 14 days for this copy to be truthful. See PR notes / external
    # config checklist.
    _trib = TRIBUTE_EN_URL if lang == "en" else TRIBUTE_RU_URL
    _kb = InlineKeyboardMarkup([[InlineKeyboardButton(T(lang, "btn_pay"), url=_trib)]])
    _msg = ("⭐️ Оформите подписку Founding Member через Tribute.\n"
            "14 дней бесплатно · отмена в любой момент."
            if lang != "en" else
            "⭐️ Subscribe as a Founding Member via Tribute.\n"
            "14-day free trial · cancel anytime.")
    await query_or_message.reply_text(_msg, reply_markup=_kb, disable_web_page_preview=True)
    return
    """Универсальный вход в оплату (YooKassa или Stars).
    Если у юзера уже есть настоящий email — сразу создаёт платёж.
    Иначе — переводит юзера в state 'awaiting_email' и просит email.

    provider:
      'yookassa' — карта/SBP, нужен для фискального чека (54-ФЗ)
      'stars'    — Telegram Stars, формально не нужен, но собираем для
                   базы подписчиков (newsletter, win-back).
    """
    existing_email = profile.get("email")
    if is_valid_email(existing_email):
        await _finalize_payment(query_or_message, context, profile,
                                existing_email, lang, provider, telegram_id)
        return

    # Переходим в режим сбора email
    context.user_data["awaiting_email_for_payment"] = {
        "profile_id":   profile["id"],
        "lang":         lang,
        "provider":     provider,
        "telegram_id":  telegram_id,
    }
    await query_or_message.reply_text(T(lang, "ask_email"))

async def _finalize_payment(reply_target, context: ContextTypes.DEFAULT_TYPE,
                             profile: dict, email: str, lang: str,
                             provider: str, telegram_id: int | None):
    """После получения email — создаёт счёт у выбранного провайдера."""
    if provider == "stars":
        await _send_stars_invoice(reply_target, profile, lang, telegram_id)
    else:
        await create_and_send_payment(reply_target, context, profile, email, lang)

async def _send_stars_invoice(reply_target, profile: dict, lang: str,
                               telegram_id: int | None):
    """Готовит Stars invoice link и шлёт кнопку.

    Спрашивает resolve_payment_price, чтобы:
      — взять актуальную цену (founding → 669⭐, standard → 956⭐)
      — проверить allowed_provider: если юзер залокнут на yookassa (RU founding),
        откажем Stars и покажем правильный routing”.
      — передать founding-флаг в invoice payload (для claim_founding_slot в webhook).
    Пользователю показываем только USD-цену (Stars-цифру Telegram сам покажет в диалоге)."""
    await reply_target.reply_text(T(lang, "stars_creating"))
    tg_id = telegram_id or 0

    # 1) resolve actual price + provider gate
    price_info = await resolve_payment_price_via_rpc(profile["id"])
    if not price_info or not price_info.get("ok"):
        log.error("_send_stars_invoice: resolve_payment_price failed for %s: %s", profile["id"], price_info)
        await reply_target.reply_text(T(lang, "pay_error"))
        return

    allowed = price_info.get("allowed_provider")
    if allowed and allowed != "stars":
        # Юзер залокнут на yookassa (RU founding) — не выдаём Stars-invoice.
        msg = (
            "⚠️ Ваш founding-слот привязан к оплате картой (₽). "
            "Откройте рублёвую оплату через кнопку /pay."
            if lang == "ru" else
            "⚠️ Your founding slot is locked to card payment (RUB). "
            "Please use /pay to open the card-payment flow."
        )
        await reply_target.reply_text(msg)
        return

    amount_stars  = int(price_info.get("amount_stars") or STARS_PRICE)
    amount_usd    = price_info.get("amount_usd") or PRICE_USD
    founding_flag = bool(price_info.get("founding_intent") or price_info.get("already_founding"))

    invoice_url = await create_stars_invoice_link(
        profile["id"], tg_id, lang,
        founding=founding_flag,
        amount_stars=amount_stars,
    )
    if not invoice_url:
        await reply_target.reply_text(T(lang, "pay_error"))
        return

    # Динамический текст на кнопке и в сообщении: показываем USD-цену.
    btn_label = (f"💳 Subscribe · Founding — ${amount_usd}/mo"
                 if founding_flag else
                 f"💳 Subscribe — ${amount_usd}/mo")
    # Колбэк refresh несёт founding-флаг, чтобы on_button мог выбрать верную ветку.
    refresh_cb = f"stars_refresh|{'f' if founding_flag else 's'}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(btn_label, url=invoice_url)],
        [InlineKeyboardButton(T(lang, "btn_refresh_invoice"), callback_data=refresh_cb)],
    ])
    if founding_flag:
        body_core = (
            f"💳 BelFed Premium · Founding Member — ${amount_usd}/mo\n\n"
            "✨ 30% off forever — the discount stays with you even when our standard price changes.\n"
            "✨ Auto-renew can be cancelled anytime from the Telegram payment screen."
        )
    else:
        body_core = T(lang, "stars_pay_link")
    body = body_core + "\n\n" + T(lang, "stars_invoice_hint")
    await reply_target.reply_text(
        body,
        reply_markup=kb,
        disable_web_page_preview=True,
    )

async def create_and_send_payment(reply_target, context: ContextTypes.DEFAULT_TYPE,
                                   profile: dict, email: str, lang: str):
    """Создаёт YooKassa-платёж с email и шлёт пользователю кнопку оплаты.

    Если edge вернул 409 wrong_provider_for_founding — значит founding-слот этого юзера
    привязан к Stars (EN founding). Показываем правильный routing."""
    await reply_target.reply_text(T(lang, "pay_creating"))
    web_url = WEB_URL_EN if lang == "en" else WEB_URL_RU
    return_url = f"{web_url}/members.html?payment=return"
    res = await create_payment_via_edge(profile["id"], email, return_url)
    if not res:
        await reply_target.reply_text(T(lang, "pay_error"))
        return
    # Special-case: founding-слот залокнут под другой провайдер (Stars).
    if res.get("error") == "wrong_provider_for_founding":
        msg = (
            "⚠️ Ваш founding-слот привязан к оплате через Telegram Stars. "
            "Нажмите кнопку Subscribe в этом боте — она откроет подписку Stars."
            if lang == "ru" else
            "⚠️ Your founding slot is locked to Telegram Stars. "
            "Use the Subscribe button in this bot — it will open Stars subscription."
        )
        await reply_target.reply_text(msg)
        return
    if not res.get("confirmation_url"):
        await reply_target.reply_text(T(lang, "pay_error"))
        return
    url = res["confirmation_url"]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(T(lang, "btn_open_pay"), url=url)]])
    await reply_target.reply_text(
        T(lang, "pay_link").format(url=url),
        reply_markup=kb,
        disable_web_page_preview=True,
    )

# ---------- Handlers ------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args or []

    # Если /start пришёл во время сбора email — выходим из всех email-state'ов
    context.user_data.pop("awaiting_email_for_payment", None)
    context.user_data.pop("awaiting_email_for_link", None)
    context.user_data.pop("awaiting_email_for_founding", None)

    # Deep-link /start auth — direct dashboard access for paid members
    if args and (args[0] == "auth" or args[0].startswith("auth_")):
        profile = await get_profile_by_telegram(user.id)
        if profile and profile.get("lang") in ("ru", "en"):
            lang = profile["lang"]
        else:
            lang = "en" if (user.language_code or "").startswith("en") else "ru"

        is_paid = await check_paid_membership(user.id, lang)
        if not is_paid:
            txt = ("⚠️ Доступ к дашборду доступен только участникам платного канала. Активируйте подписку ниже:") if lang == "ru" else \
                  ("⚠️ Dashboard access is for paid channel members. Activate your subscription below:")
            await update.message.reply_text(txt)
            await send_main_menu(update, context, lang=lang)
            return

        ott = await issue_dashboard_session(user.id, lang)
        if not ott:
            txt = "⚠️ Не удалось создать сессию. Попробуйте через минуту." if lang == "ru" else \
                  "⚠️ Couldn't create session. Try again in a moment."
            await update.message.reply_text(txt)
            return

        url = f"{DASHBOARD_AUTH_URL}?t={ott}"
        btn_label = "📊 Открыть дашборд" if lang == "ru" else "📊 Open Dashboard"
        msg = ("Вы в платном канале — открываем дашборд.\n\nСсылка действует 5 минут.") if lang == "ru" else \
              ("You're a paid channel member — open the dashboard.\n\nLink is valid for 5 minutes.")
        await update.message.reply_text(
            msg,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(btn_label, url=url)]]),
            disable_web_page_preview=True,
        )
        return

    # Deep-link /start founding[_ru|_en] — Founding Members programme entry
    if args and (args[0] == "founding" or args[0].startswith("founding_")):
        source = args[0]
        # 1) язык: суффикс источника > профиль > telegram language_code
        lang_from_src = detect_lang_from_source(source)
        profile = await get_profile_by_telegram(user.id)
        if lang_from_src:
            lang = lang_from_src
        elif profile and profile.get("lang") in ("ru", "en"):
            lang = profile["lang"]
        else:
            lang = "en" if (user.language_code or "").startswith("en") else "ru"

        # 2) Нужен профиль — founding выдаётся только зарегистрированным юзерам.
        # Если нет TG-профиля — спрашиваем email (мог зарегиться через сайт).
        # link_telegram_email_via_rpc найдёт существующий профиль и привяжет.
        # Если email не найден — fallback на trial flow.
        if not profile:
            if lang == "ru":
                ask_text = (
                    "✨ Добро пожаловать в BelFed Founding Members.\n"
                    "\n"
                    "Если у вас уже есть аккаунт BelFed (зарегистрировались на сайте) — "
                    "пришлите ваш email одним сообщением. Мы привяжем Telegram "
                    "к вашему профилю и сразу откроем founding-цену.\n"
                    "\n"
                    "Если вы новый пользователь — нажмите «Пропустить»."
                )
                skip_label = "Пропустить — я новый пользователь"
            else:
                ask_text = (
                    "✨ Welcome to BelFed Founding Members.\n"
                    "\n"
                    "If you already have a BelFed account (signed up on the site) — "
                    "send your email in one message. We'll link your Telegram "
                    "to your existing profile and open the founding price immediately.\n"
                    "\n"
                    "If you're new here — tap Skip below."
                )
                skip_label = "Skip — I'm new here"
            context.user_data["awaiting_email_for_founding"] = {
                "lang":   lang,
                "source": source,
            }
            await update.message.reply_text(
                ask_text,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(skip_label, callback_data=f"founding_skip|{source}"),
                ]]),
            )
            return

        # 3) Отмечаем founding-intent на 24h (чтобы resolve_payment_price выдал founding-цену).
        claim = await ensure_pending_founding_claim_via_rpc(
            profile["id"], lang, source=source,
        )
        if not claim or not claim.get("ok"):
            reason = (claim or {}).get("reason", "unknown")
            log.warning("founding intent rejected for %s: %s", profile["id"], reason)
            if reason == "quota_full":
                txt = ("⚠️ Все founding-слоты заняты. Спасибо за интерес!"
                       if lang == "ru" else
                       "⚠️ All founding slots are taken. Thanks for your interest!")
            elif reason == "already_founding":
                txt = ("✅ Вы уже founding-участник. Скидка действует автоматически."
                       if lang == "ru" else
                       "✅ You're already a founding member. Discount applies automatically.")
            else:
                txt = ("⚠️ Не удалось обработать заявку. Попробуйте через минуту."
                       if lang == "ru" else
                       "⚠️ Couldn't process your request. Please try again in a minute.")
            await update.message.reply_text(txt)
            await send_main_menu(update, context, lang=lang)
            return

        # 4) Получаем реальную цену (и allowed_provider).
        price_info = await resolve_payment_price_via_rpc(profile["id"])
        if not price_info or not price_info.get("ok"):
            await update.message.reply_text(T(lang, "pay_error"))
            return

        amount_rub  = price_info.get("amount_rub")
        amount_usd  = price_info.get("amount_usd")
        discount    = price_info.get("discount_pct") or 30

        # 5) RU founding → yookassa, EN founding → Stars. Отправляем юзера по правильному flow.
        if lang == "ru":
            intro = (
                f"✨ BelFed Founding Members\n"
                f"\n"
                f"// 01\n"
                f"Скидка {int(discount)}% от стоимости сервиса — навсегда. "
                f"{amount_rub}₽/мес вместо {PRICE_RUB}₽/мес. "
                f"Скидка сохраняется при любом изменении базовой цены в будущем.\n"
                f"\n"
                f"// 02\n"
                f"3 заявки на разбор актива в день с приоритетным рассмотрением. "
                f"После закрытия программы новые пользователи будут получать 1/день — "
                f"у вас остаётся 3, навсегда.\n"
                f"\n"
                f"// 03\n"
                f"Статус Founding Member привязан к вашему аккаунту. Подписку можно "
                f"поставить на паузу в любой момент — 60 дней на возврат без потери "
                f"скидки и статуса.\n"
                f"\n"
                f"Оформляем подписку картой (Мир / SBP / SberPay / T-Pay / ЮMoney)."
            )
            await update.message.reply_text(intro)
            # Нужен email для чека: если уже есть — сразу выдаём ссылку, иначе просим.
            email = profile.get("email")
            if email and is_valid_email(email) and not is_ghost_email(email):
                await create_and_send_payment(update.message, context, profile, email, lang)
            else:
                context.user_data["awaiting_email_for_payment"] = {
                    "profile_id":  profile["id"],
                    "lang":        lang,
                    "provider":    "yookassa",
                    "telegram_id": user.id,
                }
                await update.message.reply_text(T(lang, "ask_email"))
            return
        else:
            intro = (
                f"✨ BelFed Founding Members\n"
                f"\n"
                f"// 01\n"
                f"{int(discount)}% off, forever. ${amount_usd}/mo instead of ${PRICE_USD}/mo. "
                f"The discount stays no matter how our pricing changes later.\n"
                f"\n"
                f"// 02\n"
                f"3 asset requests per day with priority review. New users will get 1/day "
                f"after the program closes — you keep 3, permanently.\n"
                f"\n"
                f"// 03\n"
                f"Founding Member status tied to your account. Pause anytime — 60 days "
                f"to come back without losing the discount or status.\n"
                f"\n"
                f"Tap Subscribe below to open the Telegram Stars payment screen."
            )
            await update.message.reply_text(intro)
            await _send_stars_invoice(update.message, profile, lang, user.id)
            return

    # Deep-link /start gift7[_ru|_en] — 7-day gift trial from channel post
    if args and (args[0] == "gift7" or args[0].startswith("gift7_")):
        source = args[0]
        lang_from_src = detect_lang_from_source(source)
        profile = await get_profile_by_telegram(user.id)
        if lang_from_src:
            lang = lang_from_src
        elif profile and profile.get("lang") in ("ru", "en"):
            lang = profile["lang"]
        else:
            lang = "en" if (user.language_code or "").startswith("en") else "ru"
        await run_gift7_flow(update, context, user.id, user.username,
                              lang=lang, source=source,
                              reply_target=update.message)
        return

    # Deep-link /start request — jump straight into the asset-class picker
    if args and args[0] in ("request", "req"):
        profile = await get_profile_by_telegram(user.id)
        lang = await get_user_lang(update, profile)
        if not profile:
            await update.message.reply_text(T(lang, "need_link"))
            await send_main_menu(update, context, lang=lang)
            return
        if not has_access(profile):
            text = ("⛔ Chart requests are available to paid subscribers only."
                    if lang == "en" else
                    "⛔ Запросы доступны только платным подписчикам.")
            await update.message.reply_text(text)
            await send_main_menu(update, context, lang=lang)
            return
        await update.message.reply_text(
            _class_picker_text(lang),
            reply_markup=_class_picker_keyboard(lang),
        )
        return

    # Deep-link /start support — jump straight into the feedback flow
    if args and args[0] in ("support", "feedback"):
        profile = await get_profile_by_telegram(user.id)
        lang = await get_user_lang(update, profile)
        used = await feedback_used_24h(user.id)
        if used >= FEEDBACK_DAILY_LIMIT:
            text = (f"⛔ Daily limit reached ({FEEDBACK_DAILY_LIMIT} / 24h). Please try later."
                    if lang == "en" else
                    f"⛔ Суточный лимит исчерпан ({FEEDBACK_DAILY_LIMIT} / 24 ч). Попробуйте позже.")
            await update.message.reply_text(text)
            return
        context.user_data["awaiting_feedback"] = {"lang": lang}
        prompt = (
            "💬 Write your message to BelFed Support in a single reply.\n"
            "\n"
            "It will be delivered to our team. We'll reply here in the bot.\n"
            "\n"
            f"Limit: {FEEDBACK_DAILY_LIMIT} messages per 24h. Send /cancel to abort."
            if lang == "en" else
            "💬 Напишите ваше сообщение для BelFed Support одним ответом.\n"
            "\n"
            "Оно придёт нам, и мы ответим вам здесь же, в боте.\n"
            "\n"
            f"Лимит: {FEEDBACK_DAILY_LIMIT} сообщения в сутки. Отправьте /cancel, чтобы отменить."
        )
        placeholder = ("Your message…" if lang == "en"
                       else "Ваше сообщение…")
        await update.message.reply_text(
            prompt,
            reply_markup=ForceReply(selective=True, input_field_placeholder=placeholder),
        )
        return

    # Deep-link /start auth — выдаём ссылку в личный кабинет (из auth.html "Get a fresh link")
    if args and args[0] == "auth":
        profile = await get_profile_by_telegram(user.id)
        lang = await get_user_lang(update, profile)
        if not profile:
            await update.message.reply_text(T(lang, "cabinet_no_profile"))
        else:
            await send_cabinet_link(update.message, user.id, lang)
        return

    # Deep-link /start trial[_xxx][_ru|_en]  или  /start trial_t_<TOKEN>
    if args and (args[0] == "trial" or args[0].startswith("trial_")):
        source = args[0]
        intent_token: str | None = None

        # Веб-форма trial-intent-create выдаёт trial_t_<32 символов>.
        # Извлекаем токен и заменяем source на 'trial_web' — реальный source/lang придёт с бэкенда.
        if source.startswith("trial_t_") and len(source) >= len("trial_t_") + 16:
            intent_token = source[len("trial_t_"):]
            source = "trial_web"

        # 1) определяем язык: суффикс источника > профиль > Telegram language_code
        lang_from_src = detect_lang_from_source(args[0])
        profile = await get_profile_by_telegram(user.id)
        if lang_from_src:
            lang = lang_from_src
        elif profile and profile.get("lang") in ("ru", "en"):
            lang = profile["lang"]
        elif intent_token:
            # язык будет подменён edge-функцией из trial_intents.lang — выбираем Telegram как fallback
            lang = detect_lang_from_telegram(update)
        else:
            # Спрашиваем язык. Сохраняем источник в user_data.
            context.user_data["pending_trial_source"] = source
            if intent_token:
                context.user_data["pending_trial_intent_token"] = intent_token
            trial_greeting = (
                "👋 Welcome to BelFed Analytics!\n"
                "Please choose your language to start your free trial.\n"
                "\n"
                "👋 Добро пожаловать в BelFed Analytics!\n"
                "Выберите язык, чтобы начать бесплатный пробный период."
            )
            await update.message.reply_text(
                trial_greeting,
                reply_markup=lang_pick_keyboard("lang_trial"),
            )
            return
        await run_trial_flow(update, context, user.id, user.username,
                              source=source, lang=lang,
                              reply_target=update.message,
                              intent_token=intent_token)
        return

    # Deep-link /start promo_BFWB-XXXXXX — win-back promo code
    if args and args[0].startswith("promo_"):
        raw_code = args[0][len("promo_"):].upper()  # strip "promo_" prefix, uppercase
        profile = await get_profile_by_telegram(user.id)
        if profile and profile.get("lang") in ("ru", "en"):
            lang = profile["lang"]
        else:
            lang = "en" if (user.language_code or "").startswith("en") else "ru"
        await run_promo_flow(update, context, user.id, raw_code, lang,
                              reply_target=update.message)
        return

    # Deep-link /start <token> (привязка через сайт)
    if args and len(args[0]) >= 16:
        token = args[0]
        status, _ = await sb_post(
            "/rest/v1/rpc/claim_telegram_link",
            {"p_token": token, "p_telegram_id": user.id, "p_username": user.username or ""},
        )
        profile = await get_profile_by_telegram(user.id)
        lang = await get_user_lang(update, profile)
        if status in (200, 201):
            if is_admin(profile):
                await update.message.reply_text("✅ Telegram linked. Admin access." if lang=="en"
                                                 else "✅ Telegram привязан. Доступ администратора.")
            else:
                await update.message.reply_text(T(lang, "link_ok"))
            if has_access(profile):
                link = await grant_paid_invite(context, user.id, lang)
                if link:
                    await update.message.reply_text(
                        T(lang, "paid_invite_msg").format(link=link),
                        disable_web_page_preview=True,
                    )
        else:
            await update.message.reply_text(T(lang, "link_bad"))
        await send_main_menu(update, context, lang=lang)
        return

    # Простой /start без аргументов
    profile = await get_profile_by_telegram(user.id)
    if profile and profile.get("lang") in ("ru", "en"):
        lang = profile["lang"]
        # Если email «ghost» — просим настоящий email и привязываем к сайт-профилю.
        # Если email реальный — сразу меню.
        if is_ghost_email(profile.get("email")) or not profile.get("email"):
            await _ask_email_for_link(update.message, context, lang)
        else:
            await update.message.reply_text(T(lang, "email_link_reused"))
            await send_main_menu(update, context, lang=lang)
        return

    # Нет профиля — приветствуем билингвально и просим выбрать язык
    # (lang_menu callback потом создаст lite и попросит email)
    greeting = (
        "👋 Welcome to BelFed Analytics!\n"
        "Please choose your language below to continue.\n"
        "\n"
        "👋 Добро пожаловать в BelFed Analytics!\n"
        "Выберите язык ниже, чтобы продолжить."
    )
    await update.message.reply_text(
        greeting,
        reply_markup=lang_pick_keyboard("lang_menu"),
    )


async def _ask_email_for_link(reply_target, context: ContextTypes.DEFAULT_TYPE, lang: str):
    """Переводит пользователя в state awaiting_email_for_link и просит email."""
    context.user_data["awaiting_email_for_link"] = {"lang": lang}
    await reply_target.reply_text(T(lang, "ask_email_link"))

async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          lang: str = "ru", reply_to=None):
    user = update.effective_user
    profile = await get_profile_by_telegram(user.id)
    web_url = WEB_URL_EN if lang == "en" else WEB_URL_RU

    rows = []
    if has_access(profile):
        rows.append([InlineKeyboardButton(T(lang, "btn_paid"), callback_data="paid_invite")])

    rows.append([InlineKeyboardButton(T(lang, "btn_status"), callback_data="sub_status")])

    # Member dashboard (cabinet) sign-in — available to any linked profile.
    if profile:
        rows.append([InlineKeyboardButton(T(lang, "btn_cabinet"), callback_data="open_cabinet")])

    if has_access(profile):
        rows.append([InlineKeyboardButton(T(lang, "btn_request"), callback_data="request_asset")])
        rows.append([InlineKeyboardButton(T(lang, "btn_support"), callback_data="feedback_open")])

    sub = await get_subscription(profile["id"]) if profile else None
    has_active_paid = sub and sub.get("status") == "active"
    if not has_active_paid and not is_admin(profile):
        if profile:
            # Оплата через Tribute (Founding Member). RU → рублёвая подписка, EN → евро.
            trib_url = TRIBUTE_EN_URL if lang == "en" else TRIBUTE_RU_URL
            rows.append([InlineKeyboardButton(T(lang, "btn_pay"), url=trib_url)])
        else:
            # Нет профиля — fallback на сайт (редкий случай, юзер открыл бота без deep-link)
            rows.append([InlineKeyboardButton(T(lang, "btn_pay"), url=(TRIBUTE_EN_URL if lang == "en" else TRIBUTE_RU_URL))])
    if not profile:
        rows.append([InlineKeyboardButton(T(lang, "btn_link"), url=f"{web_url}/members.html#link")])
    rows.append([InlineKeyboardButton(T(lang, "btn_disclaimer"), callback_data="disclaimer")])

    text = (T(lang, "menu") if profile else T(lang, "welcome_new")).format(name=user.first_name or "")

    target = reply_to or (update.callback_query.message if update.callback_query else update.message)
    await target.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))

async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    profile = await get_profile_by_telegram(user.id)
    lang = await get_user_lang(update, profile)
    if profile:
        await update.message.reply_text(T(lang, "link_already"))
    else:
        await update.message.reply_text(T(lang, "need_link"))

async def cmd_cabinet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cabinet — issue a fresh member-dashboard sign-in link via Supabase magic-link."""
    user = update.effective_user
    profile = await get_profile_by_telegram(user.id)
    lang = await get_user_lang(update, profile)
    if not profile:
        await update.message.reply_text(T(lang, "cabinet_no_profile"))
        return
    await send_cabinet_link(update.message, user.id, lang)

async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/support — запускает feedback flow (тоже самое что кнопка feedback_open)."""
    user = update.effective_user
    profile = await get_profile_by_telegram(user.id)
    lang = await get_user_lang(update, profile)
    used = await feedback_used_24h(user.id)
    if used >= FEEDBACK_DAILY_LIMIT:
        text = (f"⛔ Daily limit reached ({FEEDBACK_DAILY_LIMIT} / 24h). Please try later."
                if lang == "en" else
                f"⛔ Суточный лимит исчерпан ({FEEDBACK_DAILY_LIMIT} / 24 ч). Попробуйте позже.")
        await update.message.reply_text(text)
        return
    context.user_data["awaiting_feedback"] = {"lang": lang}
    prompt = (
        "💬 Write your message to BelFed Support in a single reply.\n"
        "\n"
        "It will be delivered to our team. We'll reply here in the bot.\n"
        "\n"
        f"Limit: {FEEDBACK_DAILY_LIMIT} messages per 24h. Send /cancel to abort."
        if lang == "en" else
        "💬 Напишите ваше сообщение для BelFed Support одним ответом.\n"
        "\n"
        "Оно придёт нам, и мы ответим вам здесь же, в боте.\n"
        "\n"
        f"Лимит: {FEEDBACK_DAILY_LIMIT} сообщения в сутки. Отправьте /cancel, чтобы отменить."
    )
    placeholder = ("Your message…" if lang == "en"
                   else "Ваше сообщение…")
    await update.message.reply_text(
        prompt,
        reply_markup=ForceReply(selective=True, input_field_placeholder=placeholder),
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    profile = await get_profile_by_telegram(user.id)
    lang = await get_user_lang(update, profile)
    if not profile:
        await update.message.reply_text(T(lang, "need_link"))
        return

    sub = await get_subscription(profile["id"])
    exp = parse_ts(profile.get("subscription_expires_at"))
    now = datetime.now(timezone.utc)
    plan = profile.get("subscription_plan")

    if is_admin(profile):
        msg = "✅ Admin. Full access." if lang == "en" else "✅ Администратор. Доступ ко всем материалам без ограничений."
    elif sub and sub.get("status") == "active" and exp and exp > now:
        has_pm = bool(sub.get("payment_method_id"))
        autorenew = (T(lang, "autorenew_on")
                     if has_pm and not sub.get("cancel_at_period_end")
                     else T(lang, "autorenew_off"))
        msg = T(lang, "status_active").format(until=exp.strftime("%d.%m.%Y"), autorenew=autorenew)
    elif plan == "trial" and exp and exp > now:
        days_left = max(0, (exp - now).days)
        msg = T(lang, "trial_active").format(until=exp.strftime("%d.%m.%Y"), days=days_left)
    else:
        msg = T(lang, "status_none")

    await update.message.reply_text(msg, disable_web_page_preview=True)

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    profile = await get_profile_by_telegram(user.id)
    lang = await get_user_lang(update, profile)
    if not profile:
        await update.message.reply_text(T(lang, "need_link")); return

    sub = await get_subscription(profile["id"])
    if not sub or sub.get("status") != "active":
        await update.message.reply_text(T(lang, "cancel_none")); return

    await sb_patch(
        "/rest/v1/subscriptions",
        params={"user_id": f"eq.{profile['id']}"},
        body={
            "cancel_at_period_end": True,
            "cancel_reason": "user_requested_via_bot",
            "canceled_at":   datetime.now(timezone.utc).isoformat(),
            "updated_at":    datetime.now(timezone.utc).isoformat(),
        },
    )
    exp = parse_ts(sub.get("current_period_end")) or datetime.now(timezone.utc)
    await update.message.reply_text(T(lang, "cancel_ok").format(until=exp.strftime("%d.%m.%Y")))

async def cmd_cancel_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выходит из режима сбора email."""
    user = update.effective_user
    profile = await get_profile_by_telegram(user.id)
    lang = await get_user_lang(update, profile)
    state = context.user_data.pop("awaiting_email_for_payment", None)
    if state:
        await update.message.reply_text(T(lang, "pay_canceled"))

async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пропускает сбор email на этапе привязки без отмены платежа."""
    user = update.effective_user
    profile = await get_profile_by_telegram(user.id)
    lang = await get_user_lang(update, profile)
    state = context.user_data.pop("awaiting_email_for_link", None)
    if state:
        await update.message.reply_text(T(lang, "email_link_skipped"))
        await send_main_menu(update, context, lang=lang)

async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /lang — позволяет сменить язык в любой момент."""
    await update.message.reply_text(
        TEXTS_RU["lang_pick_title"],
        reply_markup=lang_pick_keyboard("lang_menu"),
    )

async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/dashboard — редирект на веб-личный кабинет (members.html).

    Magic-link (auth-issue → auth-exchange) больше не используется.
    Юзер входит в лК по email+password на сайте. Привязка TG↔веб
    происходит через trial-intent-create + consume_trial_intent (для новых)
    или merge_lite_into_full RPC (для тех, кто начал в боте).
    """
    user = update.effective_user
    profile = await get_profile_by_telegram(user.id)
    lang = await get_user_lang(update, profile)
    if lang == "ru":
        url = "https://belfed.ru/members.html"
        btn = "🌐 Открыть belfed.ru/members.html"
        msg = (
            "📊 Личный кабинет — открытые позиции, аналитика, история сделок.\n\n"
            "Для входа нужно зарегистрироваться по email на сайте.\n"
            "Регистрация автоматически свяжется с вашим Telegram."
        )
    else:
        url = "https://belfed.com/members.html"
        btn = "🌐 Open belfed.com/members.html"
        msg = (
            "📊 Member dashboard — open positions, analytics, trade history.\n\n"
            "To sign in, register with your email on the site.\n"
            "Registration will automatically link to your Telegram."
        )
    await update.message.reply_text(
        msg,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(btn, url=url)]]),
        disable_web_page_preview=True,
    )

# ---------- /request <TICKER> -- chart-request submission from Telegram ----------
TICKER_RE = re.compile(r"^[A-Z0-9]{1,8}$")

async def _submit_chart_request(reply_target, user, profile, lang: str,
                                  ticker: str, asset_class):
    """Shared implementation used by /request and the inline-button flow.

    `reply_target` is any object with `.reply_text(...)` (Message or callback message).
    Returns nothing — sends user-facing reply directly.
    """
    if not TICKER_RE.match(ticker):
        err = ("❌ Invalid ticker. Use 1–8 uppercase letters/digits (e.g. TSLA, BTC, RENDER)."
               if lang == "en" else
               "❌ Некорректный тикер. Используйте 1–8 заглавных букв/цифр (напр. TSLA, BTC, RENDER).")
        await reply_target.reply_text(err)
        return

    if not BOT_SHARED_SECRET:
        log.error("chart-request: BOT_SHARED_SECRET not configured")
        fallback = ("⚠️ Service temporarily unavailable. Please use the website."
                    if lang == "en" else
                    "⚠️ Сервис временно недоступен. Пожалуйста, воспользуйтесь сайтом.")
        await reply_target.reply_text(fallback)
        return

    url = f"{SUPABASE_URL}/functions/v1/chart-request"
    body: dict = {"telegram_id": str(user.id), "ticker": ticker}
    if asset_class:
        body["asset_class"] = asset_class
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "x-bot-secret": BOT_SHARED_SECRET,
                    "apikey": SUPABASE_SERVICE_KEY,
                },
                json=body,
            )
        try:
            d = r.json()
        except Exception:
            d = {"error": "bad_json", "raw": r.text[:200]}
    except Exception as e:
        log.exception("chart-request: edge fn call failed")
        d = {"error": str(e)}
        r = None

    if r is not None and r.status_code == 200 and d.get("ok"):
        remaining = d.get("remaining")
        used = d.get("used_24h")
        m = _ASSET_CLASS_META.get(asset_class or "")
        if lang == "en":
            class_suffix = f" — {m['en']}" if m else ""
            ok_msg = (f"✅ Request submitted: ${ticker}{class_suffix}\n"
                      f"\n"
                      f"We'll publish a chart update soon and DM you the link.\n"
                      f"\n"
                      f"Quota: {used}/3 used, {remaining} remaining (rolling 24h).")
        else:
            class_suffix = f" ({m['ru']})" if m else ""
            ok_msg = (f"✅ Запрос принят: ${ticker}{class_suffix}\n"
                      f"\n"
                      f"Скоро опубликуем обновление и пришлём вам ссылку.\n"
                      f"\n"
                      f"Лимит: {used}/3 использовано, осталось {remaining} (за 24 ч).")
        await reply_target.reply_text(ok_msg)
        return

    err_code = (d or {}).get("error", "")
    if err_code == "quota_exceeded":
        reset_h = d.get("reset_in_hours")
        text = (f"⛔ Daily limit reached (3 / 24h). Try again in ~{reset_h}h."
                if lang == "en" else
                f"⛔ Суточный лимит исчерпан (3 / 24 ч). Попробуйте через ~{reset_h} ч.")
    elif err_code == "duplicate_pending":
        text = (f"ℹ️ You already have a pending request for ${ticker}. Be patient — we'll deliver it shortly."
                if lang == "en" else
                f"ℹ️ У вас уже есть ожидающий запрос по ${ticker}. Ожидайте — выполним.")
    elif err_code == "not_paid" or err_code == "forbidden":
        text = ("⛔ Chart requests are available to paid subscribers only. Use /status to check your access."
                if lang == "en" else
                "⛔ Запросы доступны только платным подписчикам. Проверьте доступ: /status.")
    else:
        text = (f"⚠️ Could not submit request: {err_code or 'unknown error'}"
                if lang == "en" else
                f"⚠️ Не удалось отправить запрос: {err_code or 'неизвестная ошибка'}")
    await reply_target.reply_text(text)


# Asset class metadata for the inline picker (labels + per-class ticker examples).
_ASSET_CLASS_META = {
    "stocks":      {"emoji": "📊", "ru": "Акции",  "en": "Stocks",      "examples_ru": "TSLA, NVDA, AAPL", "examples_en": "TSLA, NVDA, AAPL", "placeholder": "TSLA"},
    "crypto":      {"emoji": "₿",  "ru": "Крипта",  "en": "Crypto",      "examples_ru": "BTC, ETH, RENDER",  "examples_en": "BTC, ETH, RENDER",  "placeholder": "BTC"},
    "fx":          {"emoji": "💱", "ru": "FX",       "en": "FX",          "examples_ru": "EURUSD, USDJPY",    "examples_en": "EURUSD, USDJPY",    "placeholder": "EURUSD"},
    "commodities": {"emoji": "🌾", "ru": "Сырьё",   "en": "Commodities", "examples_ru": "XAU, BRENT, NG",    "examples_en": "XAU, BRENT, NG",    "placeholder": "XAU"},
}


def _class_picker_keyboard(lang: str) -> InlineKeyboardMarkup:
    """2×2 inline grid of asset-class buttons. Callback data: 'req_cls:<class>'."""
    def btn(cls: str) -> InlineKeyboardButton:
        m = _ASSET_CLASS_META[cls]
        label = f"{m['emoji']} {m['ru'] if lang == 'ru' else m['en']}"
        return InlineKeyboardButton(label, callback_data=f"req_cls:{cls}")
    return InlineKeyboardMarkup([
        [btn("stocks"),      btn("crypto")],
        [btn("fx"),          btn("commodities")],
    ])


def _class_picker_text(lang: str) -> str:
    if lang == "en":
        return "What asset class do you want analyzed?"
    return "К какому классу относится актив?"


def _request_prompt_text(lang: str, asset_class: str | None = None) -> str:
    m = _ASSET_CLASS_META.get(asset_class or "")
    if lang == "en":
        class_label = f" — {m['en']}" if m else ""
        examples = m["examples_en"] if m else "TSLA, BTC, RENDER, EURUSD"
        return (f"Reply with the ticker{class_label} (1–8 letters/digits).\n"
                f"\n"
                f"Examples: {examples}\n"
                f"\n"
                f"Limit: 3 requests per 24h.")
    class_label = f" ({m['ru']})" if m else ""
    examples = m["examples_ru"] if m else "TSLA, BTC, RENDER, EURUSD"
    return (f"Ответьте на это сообщение тикером{class_label} (1–8 букв/цифр).\n"
            f"\n"
            f"Примеры: {examples}\n"
            f"\n"
            f"Лимит: 3 запроса в сутки.")


async def cmd_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow paid subscribers to request a chart update via Telegram.

    Usage:  /request TSLA   |   /request BTC crypto   |   /request RENDER
    No args — enters ForceReply mode and waits for the next text message.
    """
    user = update.effective_user
    profile = await get_profile_by_telegram(user.id)
    lang = await get_user_lang(update, profile)
    if not profile:
        await update.message.reply_text(T(lang, "need_link"))
        return

    if not has_access(profile):
        text = ("⛔ Chart requests are available to paid subscribers only. Use /status to check your access."
                if lang == "en" else
                "⛔ Запросы доступны только платным подписчикам. Проверьте доступ: /status.")
        await update.message.reply_text(text)
        return

    args = context.args or []
    if not args:
        # No args — show asset-class picker first to avoid ticker collisions.
        await update.message.reply_text(
            _class_picker_text(lang),
            reply_markup=_class_picker_keyboard(lang),
        )
        return

    # Fast path for power users: /request TICKER [class]
    ticker = args[0].strip().upper().lstrip("$").lstrip("#")
    asset_class = (args[1].strip().lower() if len(args) > 1 else "") or None
    if asset_class and asset_class not in ("stocks", "crypto", "commodities", "fx"):
        asset_class = None

    await _submit_chart_request(update.message, user, profile, lang, ticker, asset_class)


# Admin-only: список последних платежей в TG.
ADMIN_TELEGRAM_IDS = {118296372}  # Артём

# ---------- Feedback / support (MVP) -------------------------------------
FEEDBACK_DAILY_LIMIT = 3   # max messages per rolling 24h per Telegram user
FEEDBACK_MAX_LEN     = 4000

async def feedback_used_24h(telegram_id: int) -> int:
    """Returns count of feedback messages from this telegram_id in the last 24h.
    Uses RPC `feedback_used_24h(bigint)` (security definer)."""
    status, data = await sb_post(
        "/rest/v1/rpc/feedback_used_24h",
        {"p_telegram_id": telegram_id},
    )
    if status in (200, 204) and isinstance(data, int):
        return data
    if status in (200, 204) and isinstance(data, list) and data:
        # RPC may return scalar wrapped in a list
        try:
            return int(data[0])
        except Exception:
            return 0
    log.warning("feedback_used_24h RPC returned %s %s", status, data)
    return 0

async def insert_feedback(profile_id: str | None, telegram_id: int,
                          username: str | None, first_name: str | None,
                          lang: str, body: str) -> int | None:
    """Insert a new feedback row. Returns the row id or None on error."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{SUPABASE_URL}/rest/v1/feedback_messages",
            headers={**SB_HEADERS, "Prefer": "return=representation"},
            json={
                "profile_id":   profile_id,
                "telegram_id":  telegram_id,
                "username":     username,
                "first_name":   first_name,
                "lang":         lang,
                "body":         body,
                "source":       "tg_bot",
            },
        )
    if r.status_code not in (200, 201):
        log.error("insert_feedback %s %s", r.status_code, r.text[:300])
        return None
    try:
        rows = r.json()
        return rows[0]["id"] if rows else None
    except Exception:
        return None

async def fetch_feedback(fid: int) -> dict | None:
    rows = await sb_get(
        "/rest/v1/feedback_messages",
        params={"id": f"eq.{fid}",
                "select": "id,telegram_id,username,first_name,lang,body,status,profile_id"},
    )
    return rows[0] if rows else None

async def mark_feedback_answered(fid: int, reply_text: str,
                                  admin_msg_id: str | None = None) -> bool:
    status = await sb_patch(
        "/rest/v1/feedback_messages",
        {"id": f"eq.{fid}"},
        {
            "status":          "answered",
            "admin_reply":     reply_text,
            "admin_message_id": admin_msg_id,
            "replied_at":      datetime.now(timezone.utc).isoformat(),
        },
    )
    return status in (200, 204)


async def cmd_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /reply <feedback_id> <text>  — sends a signed reply
    from BelFed Support to the original user and marks the row answered."""
    user = update.effective_user
    if user.id not in ADMIN_TELEGRAM_IDS:
        return  # silently ignore
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /reply <feedback_id> <text…>"
        )
        return
    try:
        fid = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ feedback_id must be an integer.")
        return
    reply_text = " ".join(args[1:]).strip()
    if not reply_text:
        await update.message.reply_text("❌ Empty reply.")
        return

    fb = await fetch_feedback(fid)
    if not fb:
        await update.message.reply_text(f"❌ Feedback #{fid} not found.")
        return

    target_tg = fb["telegram_id"]
    user_lang = (fb.get("lang") or "ru").lower()
    header = ("📩 Reply from BelFed Support:"
              if user_lang == "en"
              else "📩 Ответ от BelFed Support:")
    body = f"{header}\n\n{reply_text}"
    try:
        sent = await context.bot.send_message(chat_id=target_tg, text=body)
        sent_msg_id = str(sent.message_id) if sent else None
    except Exception as e:
        log.exception("cmd_reply: send_message failed")
        await update.message.reply_text(f"❌ Failed to deliver: {e}")
        return

    ok = await mark_feedback_answered(fid, reply_text, sent_msg_id)
    if not ok:
        await update.message.reply_text(
            f"⚠️ Delivered to user, but DB update failed for #{fid}."
        )
        return
    await update.message.reply_text(
        f"✅ Replied to feedback #{fid} (tg={target_tg})."
    )


async def cmd_dm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /dm <telegram_id> [html:] <text…> — sends a free-form
    support message from BelFed Support to any user by telegram_id.
    Unlike /reply, this is NOT tied to a feedback ticket.

    Optional HTML: prefix the message body with `html:` to render the text
    with parse_mode=HTML (bold, links via <a href="…">…</a>, etc.).
    Example:
      /dm 8022425516 Hi Syed — your trial is active again.
      /dm 8022425516 html: Open your <a href="https://belfed.com/members.html">dashboard</a>.
    """
    user = update.effective_user
    if user.id not in ADMIN_TELEGRAM_IDS:
        return  # silently ignore
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /dm <telegram_id> [html:] <text…>"
        )
        return
    try:
        target_tg = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ telegram_id must be an integer.")
        return

    rest = args[1:]
    parse_mode = None
    # Optional HTML mode: body starts with a `html:` token.
    if rest and rest[0].lower() in ("html:", "html"):
        parse_mode = "HTML"
        rest = rest[1:]
    msg_text = " ".join(rest).strip()
    if not msg_text:
        await update.message.reply_text("❌ Empty message.")
        return

    header = "📩 Message from BelFed Support:"
    if parse_mode == "HTML":
        body = f"<b>{header}</b>\n\n{msg_text}"
    else:
        body = f"{header}\n\n{msg_text}"
    try:
        sent = await context.bot.send_message(
            chat_id=target_tg, text=body, parse_mode=parse_mode
        )
    except Exception as e:
        log.exception("cmd_dm: send_message failed")
        await update.message.reply_text(
            f"❌ Failed to deliver to {target_tg}: {e}\n"
            f"(User may have never started the bot, or blocked it.)"
        )
        return
    await update.message.reply_text(
        f"✅ Message delivered to tg={target_tg} (msg_id={sent.message_id})."
    )


async def cmd_payments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_TELEGRAM_IDS:
        return  # тихо игнорируем
    args = [a.lower() for a in (context.args or [])]
    provider_filter = None
    days = 90
    for a in args:
        if a in ("stars", "yookassa"):
            provider_filter = a if a == "yookassa" else "telegram_stars"
        elif a.endswith("d") and a[:-1].isdigit():
            days = max(1, min(365, int(a[:-1])))
    # Читаем через REST (PostgREST), join руками.
    params = {
        "select":  "paid_at,provider,amount,currency,plan,user_id,status",
        "status":  "eq.succeeded",
        "order":   "paid_at.desc",
        "limit":   "20",
        "paid_at": f"gte.{(datetime.now(timezone.utc) - timedelta(days=days)).isoformat()}",
    }
    if provider_filter:
        params["provider"] = f"eq.{provider_filter}"
    rows = await sb_get("/rest/v1/payments", params=params) or []
    if not rows:
        await update.message.reply_text("Нет платежей за период.")
        return
    # Дотягиваем профили одним запросом
    ids = ",".join({r["user_id"] for r in rows if r.get("user_id")})
    profs = {}
    if ids:
        prof_rows = await sb_get("/rest/v1/profiles",
                                  params={"select": "id,email,telegram_username",
                                          "id":     f"in.({ids})"}) or []
        profs = {p["id"]: p for p in prof_rows}
    lines = ["💳 Последние платежи:\n"]
    for r in rows:
        p = profs.get(r.get("user_id"), {})
        un = ("@" + p["telegram_username"]) if p.get("telegram_username") else "—"
        em = p.get("email") or "—"
        if em.endswith("@belfed.local"):
            em = "(ghost)"
        dt = (r.get("paid_at") or "")[:16].replace("T", " ")
        prov = "stars" if r.get("provider") == "telegram_stars" else (r.get("provider") or "?")
        amt = r.get("amount")
        cur = r.get("currency")
        lines.append(f"{dt} • {prov} • {amt} {cur} • {un} • {em}")
    await update.message.reply_text("\n".join(lines))

# Admin-only: сводка по подписчикам в TG.
async def cmd_subscribers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_TELEGRAM_IDS:
        return  # тихо игнорируем
    args = [a.lower() for a in (context.args or [])]
    # Фильтр: active | trial | expired | en | ru | test (test — явный показ)
    filt = None
    include_test = False
    for a in args:
        if a in ("active", "trial", "expired", "en", "ru"):
            filt = a
        elif a == "test":
            filt = "test"
            include_test = True
        elif a in ("all", "+test", "with-test"):
            include_test = True

    # Обращаемся к view напрямую — service role ключ обходит RLS,
    # и команда и так завёрнута в ADMIN_TELEGRAM_IDS.
    params = {"select": "*", "order": "subscription_status.asc,subscription_expires_at.desc.nullslast,created_at.desc"}
    if filt == "active":
        params["subscription_status"] = "in.(active,admin)"
    elif filt == "trial":
        params["subscription_status"] = "eq.trial"
    elif filt == "expired":
        params["subscription_status"] = "eq.expired"
    elif filt == "en":
        params["lang"] = "eq.en"
    elif filt == "ru":
        params["lang"] = "eq.ru"
    elif filt == "test":
        params["is_test_profile"] = "eq.true"
    rows = await sb_get("/rest/v1/admin_subscribers_v1", params=params) or []
    # По умолчанию скрываем тестовые профили из сводки и фильтрованных списков.
    if not include_test:
        rows = [r for r in rows if not r.get("is_test_profile")]
    if not rows:
        await update.message.reply_text("Нет записей.")
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    def is_active_trial(r):
        t = r.get("trial_end")
        return r.get("subscription_status") == "trial" and t and t > now_iso

    # Если фильтра нет — выдаём сводку + топ-20 по последней оплате
    if not filt:
        active_en = sum(1 for r in rows if r.get("subscription_status") == "active" and r.get("lang") == "en")
        active_ru = sum(1 for r in rows if r.get("subscription_status") == "active" and r.get("lang") == "ru")
        trial_en  = sum(1 for r in rows if is_active_trial(r) and r.get("lang") == "en")
        trial_ru  = sum(1 for r in rows if is_active_trial(r) and r.get("lang") == "ru")
        expired_total = sum(1 for r in rows
                            if r.get("subscription_status") == "expired"
                            or (r.get("subscription_status") == "trial" and not is_active_trial(r)))
        total_rub = sum(float(r.get("total_rub_paid") or 0) for r in rows)
        total_stars = sum(float(r.get("total_stars_paid") or 0) for r in rows)

        lines = [
            "👥 Подписчики · сводка",
            "",
            f"✅ Active: {active_en + active_ru}  (EN {active_en} · RU {active_ru})",
            f"⏳ Trial:  {trial_en + trial_ru}  (EN {trial_en} · RU {trial_ru})",
            f"⛔ Expired: {expired_total}",
            f"📦 Всего профилей: {len(rows)}",
            "",
            f"💰 Сумма оплат: {int(total_rub):,}₽ + {int(total_stars):,}⭐".replace(",", " "),
            "",
            "Последние оплаты (топ-10):",
        ]
        paid = [r for r in rows if r.get("last_payment_at")]
        paid.sort(key=lambda r: r.get("last_payment_at") or "", reverse=True)
        for r in paid[:10]:
            un = ("@" + r["telegram_username"]) if r.get("telegram_username") else "—"
            em = r.get("email") or "—"
            if em.endswith("@belfed.local"):
                em = "(ghost)"
            dt = (r.get("last_payment_at") or "")[:10]
            amt = r.get("last_payment_amount")
            cur = (r.get("last_payment_currency") or "").upper()
            cur_sym = "₽" if cur == "RUB" else ("⭐" if cur in ("XTR", "STARS") else " " + cur)
            lines.append(f"{dt} • {amt}{cur_sym} • {un} • {em}")
        lines.append("")
        lines.append("🔍 Фильтры: /subscribers active | trial | expired | en | ru | test")
        lines.append("🌐 Полная панель: belfed.ru/admin-subscribers.html")
        await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)
        return

    # С фильтром — список (до 30)
    label = {
        "active":  "✅ Активные подписки",
        "trial":   "⏳ Триал",
        "expired": "⛔ Истёкшие",
        "en":      "🇬🇧 EN канал",
        "ru":      "🇷🇺 RU канал",
    }[filt]
    rows.sort(key=lambda r: r.get("subscription_expires_at") or r.get("trial_end") or "", reverse=True)
    lines = [f"{label} ({len(rows)})", ""]
    for r in rows[:30]:
        un = ("@" + r["telegram_username"]) if r.get("telegram_username") else ("id:" + (r.get("telegram_id") or "—"))
        em = r.get("email") or ""
        if em.endswith("@belfed.local"):
            em = ""
        em_part = (" · " + em) if em else ""
        lng = (r.get("lang") or "--").upper()
        st  = (r.get("subscription_status") or "").upper()
        exp = r.get("subscription_expires_at") or r.get("trial_end")
        exp_part = (" → " + exp[:10]) if exp else ""
        lines.append(f"[{lng}] {st}{exp_part} • {un}{em_part}")
    if len(rows) > 30:
        lines.append("")
        lines.append(f"… и ещё {len(rows) - 30}. Полный список: belfed.ru/admin-subscribers.html")
    await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)


async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает обычный текст: positions wizards / email / chart-request ticker."""
    # 0. Ignore non-private chats (groups, supergroups, channels, forum topics).
    # The bot is admin in paid community chats with privacy mode disabled, so it
    # otherwise sees every member message. None of the text flows below are
    # designed for group context, so we exit early to avoid noisy auto-replies
    # like freetext_fallback firing in forum topics.
    chat = update.effective_chat
    if chat is not None and chat.type != "private":
        return
    # 1. Positions module (wizards, comments, close-price input) — admin only
    if await positions.maybe_handle_text(update, context):
        return

    # 2. Chart-request ForceReply flow (after class was picked)
    cr_state = context.user_data.get("awaiting_chart_request_ticker")
    if cr_state:
        user = update.effective_user
        text = (update.message.text or "").strip()
        lang = cr_state.get("lang", "ru")
        # Allow user to abort by typing /cancel or 'cancel'
        if text.lower() in ("/cancel", "cancel", "отмена", "/отмена"):
            context.user_data.pop("awaiting_chart_request_ticker", None)
            await update.message.reply_text(
                "❌ Cancelled." if lang == "en" else "❌ Отменено."
            )
            return
        # Ticker is the first word; asset_class comes from picker state.
        parts = text.split()
        ticker = parts[0].strip().upper().lstrip("$").lstrip("#")
        asset_class = cr_state.get("asset_class")
        # Clear state BEFORE network call so a second message can't double-submit
        context.user_data.pop("awaiting_chart_request_ticker", None)
        profile = await get_profile_by_telegram(user.id)
        if not profile:
            await update.message.reply_text(T(lang, "need_link"))
            return
        await _submit_chart_request(update.message, user, profile, lang, ticker, asset_class)
        return

    # 3. Feedback / support ForceReply flow
    fb_state = context.user_data.get("awaiting_feedback")
    if fb_state:
        user = update.effective_user
        text = (update.message.text or "").strip()
        lang = fb_state.get("lang", "ru")
        if text.lower() in ("/cancel", "cancel", "отмена", "/отмена"):
            context.user_data.pop("awaiting_feedback", None)
            await update.message.reply_text(
                "❌ Cancelled." if lang == "en" else "❌ Отменено."
            )
            return
        if len(text) < 1 or len(text) > FEEDBACK_MAX_LEN:
            err = (f"⚠️ Message must be 1–{FEEDBACK_MAX_LEN} characters."
                   if lang == "en" else
                   f"⚠️ Сообщение должно быть 1–{FEEDBACK_MAX_LEN} символов.")
            await update.message.reply_text(err)
            return
        # Clear state BEFORE network calls to prevent double-submit
        context.user_data.pop("awaiting_feedback", None)
        # Re-check quota server-side (defense in depth)
        used = await feedback_used_24h(user.id)
        if used >= FEEDBACK_DAILY_LIMIT:
            text_lim = (f"⛔ Daily limit reached ({FEEDBACK_DAILY_LIMIT} / 24h)."
                        if lang == "en" else
                        f"⛔ Суточный лимит исчерпан ({FEEDBACK_DAILY_LIMIT} / 24 ч).")
            await update.message.reply_text(text_lim)
            return
        profile = await get_profile_by_telegram(user.id)
        profile_id = profile["id"] if profile else None
        fid = await insert_feedback(
            profile_id=profile_id,
            telegram_id=user.id,
            username=user.username,
            first_name=user.first_name,
            lang=lang,
            body=text,
        )
        if not fid:
            err = ("⚠️ Service temporarily unavailable. Please try again later."
                   if lang == "en" else
                   "⚠️ Сервис временно недоступен. Попробуйте позже.")
            await update.message.reply_text(err)
            return
        # Forward to admin DM(s) with metadata
        plan_str = "—"
        exp_str = "—"
        if profile:
            plan_str = profile.get("subscription_plan") or profile.get("subscription_status") or "—"
            exp_str = profile.get("subscription_expires_at") or "—"
        uname = f"@{user.username}" if user.username else "—"
        admin_msg = (
            f"💬 New feedback #{fid}\n"
            f"From: {user.first_name or '—'} ({uname}, id={user.id})\n"
            f"Plan: {plan_str}\n"
            f"Expires: {exp_str}\n"
            f"Lang: {lang}\n"
            f"\n"
            f"{text}\n"
            f"\n"
            f"Reply with: /reply {fid} <text>"
        )
        for admin_tg in ADMIN_TELEGRAM_IDS:
            try:
                await context.bot.send_message(chat_id=admin_tg, text=admin_msg)
            except Exception as e:
                log.error("feedback: failed to notify admin %s: %s", admin_tg, e)
        ack = (f"✅ Your message has been sent. We typically reply within 24h.\n"
               f"\n"
               f"Quota: {used + 1}/{FEEDBACK_DAILY_LIMIT} used (rolling 24h)."
               if lang == "en" else
               f"✅ Ваше сообщение отправлено. Обычно отвечаем в течение 24 ч.\n"
               f"\n"
               f"Лимит: {used + 1}/{FEEDBACK_DAILY_LIMIT} использовано (за 24 ч).")
        await update.message.reply_text(ack)
        return

    # 0) email для founding-flow (юзер пришёл по founding deep-link без TG-профиля)
    founding_state = context.user_data.get("awaiting_email_for_founding")
    if founding_state:
        user = update.effective_user
        text = (update.message.text or "").strip()
        lang = founding_state.get("lang", "en")
        source = founding_state.get("source", "founding_en" if lang == "en" else "founding_ru")

        if not is_valid_email(text):
            await update.message.reply_text(T(lang, "email_link_invalid"))
            return

        # Сначала создаём lite-профиль через bot-claim-trial (нужен telegram_id в БД
        # чтобы link_telegram_email мог найти текущий "лайт" и смерджить).
        res_trial = await claim_trial_via_edge(user.id, user.username,
                                                source=source, lang=lang)
        if not res_trial:
            await update.message.reply_text(T(lang, "trial_claim_error"))
            return

        # Теперь link: если email уже зарегистрирован на сайте — мерджит lite-профиль
        # в существующий. Если email новый — просто привязывает к lite-профилю.
        res_link = await link_telegram_email_via_rpc(user.id, text, user.username)
        if not res_link or not res_link.get("ok"):
            err = (res_link or {}).get("error", "unknown")
            log.warning("founding email-link error=%s tg=%s email=%s", err, user.id, text)
            if err == "email_taken":
                await update.message.reply_text(T(lang, "email_link_taken"))
                return  # оставляем state — пусть пробует другой email
            await update.message.reply_text(T(lang, "email_link_temporary"))
            return

        # Успех — выходим из state
        context.user_data.pop("awaiting_email_for_founding", None)

        if res_link.get("linked"):
            # Существующий сайт-юзер успешно привязан
            await update.message.reply_text(T(lang, "email_link_ok_linked").format(email=text))
        elif res_link.get("created_email"):
            await update.message.reply_text(T(lang, "email_link_ok_saved").format(email=text))

        # Бонус +7 дней раннему подписчику (идемпотентно)
        profile_id = res_link.get("user_id")
        if profile_id:
            try:
                bonus = await grant_early_subscriber_bonus_via_rpc(profile_id)
                if bonus and bonus.get("granted") and bonus.get("mode") == "expiry_extended":
                    new_exp = bonus.get("new_expires_at") or ""
                    new_exp_display = new_exp[:10] if isinstance(new_exp, str) and len(new_exp) >= 10 else new_exp
                    if isinstance(new_exp_display, str) and len(new_exp_display) == 10:
                        try:
                            y, m, d = new_exp_display.split("-")
                            new_exp_display = f"{d}.{m}.{y}"
                        except Exception:
                            pass
                    await update.message.reply_text(
                        T(lang, "early_bonus_granted").format(new_expiry=new_exp_display),
                        parse_mode="Markdown",
                    )
            except Exception as e:
                log.warning("founding-flow early bonus failed: %s", e)

        # Запускаем founding-flow на привязанном профиле
        profile = await get_profile_by_telegram(user.id)
        if not profile:
            await update.message.reply_text(T(lang, "pay_error"))
            return

        claim = await ensure_pending_founding_claim_via_rpc(
            profile["id"], lang, source=source,
        )
        if not claim or not claim.get("ok"):
            reason = (claim or {}).get("reason", "unknown")
            log.warning("founding intent rejected for %s: %s", profile["id"], reason)
            if reason == "quota_full":
                txt = ("⚠️ Все founding-слоты заняты. Спасибо за интерес!"
                       if lang == "ru" else
                       "⚠️ All founding slots are taken. Thanks for your interest!")
            elif reason == "already_founding":
                txt = ("✅ Вы уже founding-участник. Скидка действует автоматически."
                       if lang == "ru" else
                       "✅ You're already a founding member. Discount applies automatically.")
            else:
                txt = ("⚠️ Не удалось обработать заявку. Попробуйте через минуту."
                       if lang == "ru" else
                       "⚠️ Couldn't process your request. Please try again in a minute.")
            await update.message.reply_text(txt)
            await send_main_menu(update, context, lang=lang)
            return

        price_info = await resolve_payment_price_via_rpc(profile["id"])
        if not price_info or not price_info.get("ok"):
            await update.message.reply_text(T(lang, "pay_error"))
            return

        amount_rub = price_info.get("amount_rub")
        amount_usd = price_info.get("amount_usd")
        discount   = price_info.get("discount_pct") or 30

        if lang == "ru":
            intro = (
                f"✨ BelFed Founding Members\n"
                f"\n"
                f"// 01\n"
                f"Скидка {int(discount)}% от стоимости сервиса — навсегда. "
                f"{amount_rub}₽/мес вместо {PRICE_RUB}₽/мес. "
                f"Скидка сохраняется при любом изменении базовой цены в будущем.\n"
                f"\n"
                f"// 02\n"
                f"3 заявки на разбор актива в день с приоритетным рассмотрением. "
                f"После закрытия программы новые пользователи будут получать 1/день — "
                f"у вас остаётся 3, навсегда.\n"
                f"\n"
                f"// 03\n"
                f"Статус Founding Member привязан к вашему аккаунту. Подписку можно "
                f"поставить на паузу в любой момент — 60 дней на возврат без потери "
                f"скидки и статуса.\n"
                f"\n"
                f"Оформляем подписку картой (Мир / SBP / SberPay / T-Pay / ЮMoney)."
            )
            await update.message.reply_text(intro)
            email_for_receipt = profile.get("email")
            if email_for_receipt and is_valid_email(email_for_receipt) and not is_ghost_email(email_for_receipt):
                await create_and_send_payment(update.message, context, profile, email_for_receipt, lang)
            else:
                context.user_data["awaiting_email_for_payment"] = {
                    "profile_id":  profile["id"],
                    "lang":        lang,
                    "provider":    "yookassa",
                    "telegram_id": user.id,
                }
                await update.message.reply_text(T(lang, "ask_email"))
        else:
            intro = (
                f"✨ BelFed Founding Members\n"
                f"\n"
                f"// 01\n"
                f"{int(discount)}% off, forever. ${amount_usd}/mo instead of ${PRICE_USD}/mo. "
                f"The discount stays no matter how our pricing changes later.\n"
                f"\n"
                f"// 02\n"
                f"3 asset requests per day with priority review. New users will get 1/day "
                f"after the program closes — you keep 3, permanently.\n"
                f"\n"
                f"// 03\n"
                f"Founding Member status tied to your account. Pause anytime — 60 days "
                f"to come back without losing the discount or status.\n"
                f"\n"
                f"Tap Subscribe below to open the Telegram Stars payment screen."
            )
            await update.message.reply_text(intro)
            await _send_stars_invoice(update.message, profile, lang, user.id)
        return

    # 1) email для привязки после голого /start
    link_state = context.user_data.get("awaiting_email_for_link")
    if link_state:
        user = update.effective_user
        text = (update.message.text or "").strip()
        lang = link_state.get("lang", "ru")

        if not is_valid_email(text):
            await update.message.reply_text(T(lang, "email_link_invalid"))
            return

        res = await link_telegram_email_via_rpc(user.id, text, user.username)
        if not res:
            # network / HTTP error — RPC didn't return JSON
            log.error("link_telegram_email RPC returned None for tg=%s", user.id)
            await update.message.reply_text(T(lang, "email_link_temporary"))
            return

        if res.get("ok"):
            # выходим из state
            context.user_data.pop("awaiting_email_for_link", None)
            if res.get("reused"):
                await update.message.reply_text(T(lang, "email_link_reused"))
            elif res.get("linked"):
                await update.message.reply_text(T(lang, "email_link_ok_linked").format(email=text))
            else:
                await update.message.reply_text(T(lang, "email_link_ok_saved").format(email=text))

            # +7 дней бонус раннему подписчику (идемпотентно в RPC)
            profile_id = res.get("user_id")
            if profile_id:
                bonus = await grant_early_subscriber_bonus_via_rpc(profile_id)
                if bonus and bonus.get("granted"):
                    mode = bonus.get("mode") or ""
                    if mode == "lifetime_acknowledged":
                        await update.message.reply_text(T(lang, "early_bonus_lifetime"), parse_mode="Markdown")
                    elif mode == "expiry_extended":
                        new_exp = bonus.get("new_expires_at") or ""
                        # format YYYY-MM-DD HH:MM:SS+TZ -> DD.MM.YYYY
                        new_exp_display = new_exp[:10] if isinstance(new_exp, str) and len(new_exp) >= 10 else new_exp
                        if isinstance(new_exp_display, str) and len(new_exp_display) == 10:
                            try:
                                y, m, d = new_exp_display.split("-")
                                new_exp_display = f"{d}.{m}.{y}"
                            except Exception:
                                pass
                        try:
                            await update.message.reply_text(
                                T(lang, "early_bonus_granted").format(new_expiry=new_exp_display),
                                parse_mode="Markdown",
                            )
                        except Exception as e:
                            log.error("early_bonus message failed: %s", e)

            await send_main_menu(update, context, lang=lang)
            return

        err = res.get("error") or ""
        # Log every non-ok outcome with full context for ops triage
        log.warning(
                "link_telegram_email returned error=%s for tg=%s email=%s payload=%s",
                err, user.id, text, res,
        )
        if err == "email_taken":
            await update.message.reply_text(T(lang, "email_link_taken"))
            # state оставляем — пусть попробует другой email или /skip
            return
        if err == "email_invalid":
            await update.message.reply_text(T(lang, "email_link_invalid"))
            return
        if err == "email_required":
            await update.message.reply_text(T(lang, "email_link_required"))
            return
        if err == "no_profile":
            # Нет профиля под этим telegram_id — нужно пройти /start, чтобы ensure_lite_profile создал ghost
            context.user_data.pop("awaiting_email_for_link", None)
            await update.message.reply_text(T(lang, "email_link_no_profile"))
            return
        if err == "email_taken_race":
            # UNIQUE-конфликт (две параллельные привязки) — в большинстве случаев это же сам юзерв соседнем окне
            context.user_data.pop("awaiting_email_for_link", None)
            await update.message.reply_text(T(lang, "email_link_race"))
            return
        if err == "telegram_id_required":
            # не должно случаться из бота, но обработаем как temporary
            await update.message.reply_text(T(lang, "email_link_temporary"))
            return
        # Неизвестный код ошибки — режим «временная проблема, попробуйте снова» вместо ложного pay_error
        await update.message.reply_text(T(lang, "email_link_temporary"))
        return

    state = context.user_data.get("awaiting_email_for_payment")
    if not state:
        # No active state: user just typed something. Don't stay silent —
        # give a friendly hint how to reach support / open the menu.
        try:
            profile = await get_profile_by_telegram(update.effective_user.id)
        except Exception:
            profile = None
        lang = await get_user_lang(update, profile) if profile is not None else "ru"
        await update.message.reply_text(T(lang, "freetext_fallback"))
        return

    user = update.effective_user
    text = (update.message.text or "").strip()
    lang = state.get("lang", "ru")
    provider = state.get("provider", "yookassa")
    telegram_id = state.get("telegram_id") or user.id

    if not is_valid_email(text):
        await update.message.reply_text(T(lang, "email_invalid"))
        return

    # Сохраняем email в БД — триггер profiles_auto_opt_in_email автоматически
    # добавит его в email_subscribers (при активной подписке).
    profile_id = state["profile_id"]
    await update_profile_email(profile_id, text)

    # Выходим из режима
    context.user_data.pop("awaiting_email_for_payment", None)

    await update.message.reply_text(T(lang, "email_saved").format(email=text))

    # Создаём платёж у нужного провайдера
    profile = await get_profile_by_telegram(user.id)
    if not profile:
        await update.message.reply_text(T(lang, "pay_no_profile"))
        return
    await _finalize_payment(update.message, context, profile, text, lang, provider, telegram_id)

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # Positions module callbacks (positions:*) handled separately
    if (query.data or "").startswith("positions:"):
        await positions.maybe_handle_callback(update, context)
        return
    await query.answer()
    user = query.from_user
    data = query.data or ""

    # Founding flow: пользователь нажал Skip (он новый, не сайт-подписчик)
    if data.startswith("founding_skip|"):
        _, _, source = data.partition("|")
        if not source:
            source = "founding_en"
        lang = detect_lang_from_source(source) or "en"
        # Очищаем state email
        context.user_data.pop("awaiting_email_for_founding", None)
        # Обычный trial flow + founding follow-up как раньше
        intro = ("✨ Запускаем 14-дневный триал. После него вы сможете оформить founding-подписку."
                 if lang == "ru" else
                 "✨ Starting your 14-day trial. After that you can claim the founding price.")
        await query.message.reply_text(intro)
        await run_trial_flow(update, context, user.id, user.username,
                              source=source, lang=lang,
                              reply_target=query.message)
        return

    # Stars invoice refresh: пользователь нажал «🔄 Обновить ссылку» под истёкшим invoice.
    # Telegram Stars invoice TTL ~10 минут — генерируем свежий через _send_stars_invoice.
    # ensure_pending_founding_claim идемпотентен → переиспользует существующий founding-intent
    # без перерасхода квоты. Цена и founding_flag берутся из resolve_payment_price.
    if data.startswith("stars_refresh|"):
        profile = await get_profile_by_telegram(user.id)
        lang = await get_user_lang(update, profile)
        if not profile:
            await query.message.reply_text(T(lang, "need_link"))
            return
        # Передаём query.message как reply_target — новая invoice-карточка появится
        # отдельным сообщением под старой (старую не трогаем, чтобы избежать гонки
        # при перезапуске Telegram-клиента у пользователя).
        await _send_stars_invoice(query.message, profile, lang, user.id)
        return

    # Выбор языка: payload "lang_trial|ru" или "lang_menu|en"
    if data.startswith("lang_trial|") or data.startswith("lang_menu|"):
        prefix, _, lang = data.partition("|")
        if lang not in ("ru", "en"):
            return
        # сохраняем в БД (если профиль есть — обновим, если нет — RPC создаст mapping)
        await set_user_language(user.id, lang)
        await query.message.reply_text(T(lang, "lang_saved"))

        if prefix == "lang_trial":
            source = context.user_data.pop("pending_trial_source", "trial")
            intent_token = context.user_data.pop("pending_trial_intent_token", None)
            await run_trial_flow(update, context, user.id, user.username,
                                  source=source, lang=lang,
                                  reply_target=query.message,
                                  intent_token=intent_token)
        else:
            # lang_menu — голый /start. Гарантируем существование lite-профиля,
            # потом спрашиваем email (или выводим меню, если email уже реальный).
            profile = await get_profile_by_telegram(user.id)
            if not profile:
                # Создаём lite-профиль с триалом (тот же RPC что и у trial deep-link).
                # invite-ссылку намеренно НЕ отдаём сразу — сначала собираем email.
                claim_res = await claim_trial_via_edge(user.id, user.username,
                                                       source="telegram_direct", lang=lang)
                profile = await get_profile_by_telegram(user.id)

                # ——— Guard: не продвигаем пользователя в email-state, если профиль
                # не создался. Иначе link_telegram_email RPC вернёт no_profile и
                # пользователь зависает в цикле "нажмите /start ещё раз".
                # claim_trial_via_edge возвращает None при 5xx, и dict с ok=false
                # при 4xx/business-error. Оба случая обрабатываются здесь.
                if not profile:
                    log.error(
                        "lang_menu: profile not created after claim_trial_via_edge "
                        "telegram_id=%s claim_res=%s",
                        user.id, claim_res,
                    )
                    await query.message.reply_text(T(lang, "start_temporary"))
                    return

            if profile and not is_ghost_email(profile.get("email")) and profile.get("email"):
                await query.message.reply_text(T(lang, "email_link_reused"))
                await send_main_menu(update, context, lang=lang, reply_to=query.message)
            else:
                await _ask_email_for_link(query.message, context, lang)
        return

    # Доступ к платному каналу
    if data == "paid_invite":
        profile = await get_profile_by_telegram(user.id)
        lang = await get_user_lang(update, profile)
        if not has_access(profile):
            await query.message.reply_text(T(lang, "no_access"), disable_web_page_preview=True)
            return
        link = await grant_paid_invite(context, user.id, lang)
        if link:
            await query.message.reply_text(
                T(lang, "paid_invite_msg").format(link=link),
                disable_web_page_preview=True,
            )
        else:
            await query.message.reply_text(T(lang, "paid_invite_fail"))
        return

    if data == "open_cabinet":
        profile = await get_profile_by_telegram(user.id)
        lang = await get_user_lang(update, profile)
        if not profile:
            await query.message.reply_text(T(lang, "cabinet_no_profile")); return
        await send_cabinet_link(query.message, user.id, lang)
        return

    if data == "sub_status":
        profile = await get_profile_by_telegram(user.id)
        lang = await get_user_lang(update, profile)
        if not profile:
            await query.message.reply_text(T(lang, "need_link")); return
        sub = await get_subscription(profile["id"])
        exp = parse_ts(profile.get("subscription_expires_at"))
        now = datetime.now(timezone.utc)
        plan = profile.get("subscription_plan")
        if sub and sub.get("status") == "active" and exp and exp > now:
            has_pm = bool(sub.get("payment_method_id"))
            autorenew = (T(lang, "autorenew_on")
                         if has_pm and not sub.get("cancel_at_period_end")
                         else T(lang, "autorenew_off"))
            msg = T(lang, "status_active").format(until=exp.strftime("%d.%m.%Y"), autorenew=autorenew)
        elif plan == "trial" and exp and exp > now:
            days_left = max(0, (exp - now).days)
            msg = T(lang, "trial_active").format(until=exp.strftime("%d.%m.%Y"), days=days_left)
        else:
            msg = T(lang, "status_none")
        await query.message.reply_text(msg, disable_web_page_preview=True)
        return

    if data == "disclaimer":
        profile = await get_profile_by_telegram(user.id)
        lang = await get_user_lang(update, profile)
        await query.message.reply_text(T(lang, "disclaimer"))
        return

    if data == "feedback_open":
        profile = await get_profile_by_telegram(user.id)
        lang = await get_user_lang(update, profile)
        # Quota check (3 / 24h)
        used = await feedback_used_24h(user.id)
        if used >= FEEDBACK_DAILY_LIMIT:
            text = (f"⛔ Daily limit reached ({FEEDBACK_DAILY_LIMIT} / 24h). "
                    f"Please try again later."
                    if lang == "en" else
                    f"⛔ Суточный лимит исчерпан ({FEEDBACK_DAILY_LIMIT} / 24 ч). "
                    f"Пожалуйста, попробуйте позже.")
            await query.message.reply_text(text)
            return
        context.user_data["awaiting_feedback"] = {"lang": lang}
        prompt = (
            "💬 Write your message to BelFed Support in a single reply.\n"
            "\n"
            "It will be delivered to our team. We'll reply here in the bot.\n"
            "\n"
            f"Limit: {FEEDBACK_DAILY_LIMIT} messages per 24h. Send /cancel to abort."
            if lang == "en" else
            "💬 Напишите ваше сообщение для BelFed Support одним ответом.\n"
            "\n"
            "Оно придёт нам, и мы ответим вам здесь же, в боте.\n"
            "\n"
            f"Лимит: {FEEDBACK_DAILY_LIMIT} сообщения в сутки. Отправьте /cancel, чтобы отменить."
        )
        placeholder = ("Your message…" if lang == "en"
                       else "Ваше сообщение…")
        await query.message.reply_text(
            prompt,
            reply_markup=ForceReply(selective=True, input_field_placeholder=placeholder),
        )
        return

    if data == "request_asset":
        profile = await get_profile_by_telegram(user.id)
        lang = await get_user_lang(update, profile)
        if not profile:
            await query.message.reply_text(T(lang, "need_link"))
            return
        if not has_access(profile):
            text = ("⛔ Chart requests are available to paid subscribers only."
                    if lang == "en" else
                    "⛔ Запросы доступны только платным подписчикам.")
            await query.message.reply_text(text)
            return
        # Show asset-class picker first — ForceReply for ticker comes after class is chosen.
        await query.message.reply_text(
            _class_picker_text(lang),
            reply_markup=_class_picker_keyboard(lang),
        )
        return

    if data.startswith("req_cls:"):
        asset_class = data.split(":", 1)[1]
        if asset_class not in _ASSET_CLASS_META:
            return
        profile = await get_profile_by_telegram(user.id)
        lang = await get_user_lang(update, profile)
        if not profile or not has_access(profile):
            text = ("⛔ Chart requests are available to paid subscribers only."
                    if lang == "en" else
                    "⛔ Запросы доступны только платным подписчикам.")
            await query.message.reply_text(text)
            return
        # Replace the picker keyboard on the previous message with a single "← back"
        # button so the user can change their mind even after selecting a category.
        back_label = "← Change category" if lang == "en" else "← Сменить категорию"
        back_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(back_label, callback_data="req_back")
        ]])
        try:
            await query.edit_message_reply_markup(reply_markup=back_kb)
        except Exception:
            pass
        context.user_data["awaiting_chart_request_ticker"] = {
            "lang": lang,
            "asset_class": asset_class,
        }
        placeholder = _ASSET_CLASS_META[asset_class]["placeholder"]
        await query.message.reply_text(
            _request_prompt_text(lang, asset_class),
            reply_markup=ForceReply(selective=True, input_field_placeholder=placeholder),
        )
        return

    if data == "req_back":
        profile = await get_profile_by_telegram(user.id)
        lang = await get_user_lang(update, profile)
        # Drop any pending ticker-await state.
        context.user_data.pop("awaiting_chart_request_ticker", None)
        # Restore the picker on the original message.
        try:
            await query.edit_message_reply_markup(reply_markup=_class_picker_keyboard(lang))
        except Exception:
            # If the original message can't be edited, send a fresh picker.
            await query.message.reply_text(
                _class_picker_text(lang),
                reply_markup=_class_picker_keyboard(lang),
            )
        return

    if data == "start_payment":
        profile = await get_profile_by_telegram(user.id)
        lang = await get_user_lang(update, profile)
        if not profile:
            await query.message.reply_text(T(lang, "pay_no_profile"))
            return
        await start_payment_with_email(query.message, context, profile, lang)
        return

    if data == "start_payment_stars":
        profile = await get_profile_by_telegram(user.id)
        lang = await get_user_lang(update, profile)
        if not profile:
            await query.message.reply_text(T(lang, "pay_no_profile"))
            return
        # Собираем email для базы подписчиков (newsletter, win-back, transactional).
        await start_payment_with_email(query.message, context, profile, lang,
                                       provider="stars", telegram_id=user.id)
        return

# ---------- Telegram Stars: pre_checkout + successful_payment ------------
async def on_pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Telegram требует ответить в течение 10 секунд, иначе платёж отменяется.
    Для Stars обычно просто auto-approve."""
    pcq = update.pre_checkout_query
    try:
        # Минимальная проверка: правильная валюта и payload
        if pcq.currency != "XTR" or not (pcq.invoice_payload or "").startswith("stars_sub|"):
            await pcq.answer(ok=False, error_message="Invalid invoice")
            return
        await pcq.answer(ok=True)
    except Exception as e:
        log.error("on_pre_checkout failed: %s", e)
        try:
            await pcq.answer(ok=False, error_message="Internal error")
        except Exception:
            pass


# ---------- Admin notification: new paid subscription --------------------
async def notify_admins_new_subscription(
    context: ContextTypes.DEFAULT_TYPE,
    profile: dict,
    profile_after: dict | None,
    user,  # telegram.User
    sp,    # telegram.SuccessfulPayment
    is_founding_payment: bool,
    is_recurring_flag: bool,
    claim_res: dict | None,
    trial_end_before: "datetime | None" = None,
):
    """Send a short admin DM whenever a paid subscription is activated.
    Best-effort: errors are logged, not raised — payment flow must never fail
    because of a notification glitch.
    """
    try:
        prof = profile_after or profile or {}
        tg_username = ("@" + user.username) if user.username else "—"
        full_name = " ".join(filter(None, [user.first_name, user.last_name])) or "—"
        email = prof.get("email") or "—"
        lang = prof.get("lang") or "—"
        amount_units = sp.total_amount  # for XTR this is the Stars count integer
        currency = sp.currency or "—"
        provider = "Telegram Stars" if currency == "XTR" else (
            "YooKassa" if currency in ("RUB", "rub") else currency
        )
        usd_estimate = ""
        if currency == "XTR":
            # 669⭐ → $10.50 founding, 956⭐ → $15.00 standard. Show approximate USD.
            if amount_units == 669:
                usd_estimate = " (~$10.50)"
            elif amount_units == 956:
                usd_estimate = " (~$15.00)"

        # founding number, if claim succeeded
        founding_line = ""
        if is_founding_payment:
            no = (claim_res or {}).get("founding_member_no")
            # Founding Member shown without member_number per product decision
            founding_line = "Type: Founding Member 🌟"
        else:
            founding_line = "Type: Standard subscription"

        # subscription_expires_at after the payment
        exp_str = "—"
        exp_dt = parse_ts(prof.get("subscription_expires_at")) if prof else None
        if exp_dt:
            exp_str = exp_dt.strftime("%d.%m.%Y")

        # trial rollover info — only meaningful if user paid BEFORE trial_end
        rollover_line = ""
        if trial_end_before:
            now_utc = datetime.now(timezone.utc)
            if trial_end_before > now_utc:
                days = (trial_end_before - now_utc).days
                was_str = trial_end_before.strftime("%b %d")
                rollover_line = f"Trial rollover: +{days} days (was {was_str})\n"

        recurring_str = "yes (auto-renew)" if is_recurring_flag else "one-time"

        text = (
            "💰 New paid subscription\n"
            "\n"
            f"User: {tg_username} ({full_name})\n"
            f"TG ID: {user.id}\n"
            f"Email: {email}\n"
            f"Lang: {lang}\n"
            f"Profile: {prof.get('id', '—')}\n"
            "\n"
            f"{founding_line}\n"
            f"Provider: {provider}\n"
            f"Amount: {amount_units}{' ⭐' if currency == 'XTR' else ' ' + currency}{usd_estimate}\n"
            f"Recurring: {recurring_str}\n"
            "\n"
            f"Subscription until: {exp_str}\n"
            f"{rollover_line}"
            f"Charge ID: {sp.telegram_payment_charge_id}"
        )

        for admin_tg in ADMIN_TELEGRAM_IDS:
            try:
                await context.bot.send_message(chat_id=admin_tg, text=text)
            except Exception as e:
                log.error("admin notify: failed to send to %s: %s", admin_tg, e)
    except Exception as e:
        log.error("notify_admins_new_subscription: unexpected error: %s", e)


async def on_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """После успешной оплаты: записываем в БД, выдаём invite в EN-group."""
    msg = update.message
    sp = msg.successful_payment
    user = update.effective_user

    payload = sp.invoice_payload or ""
    parts = payload.split("|")
    # payload format: stars_sub|<profile_id>|<telegram_id>[|founding=1]
    if len(parts) < 3 or parts[0] != "stars_sub":
        log.warning("successful_payment with unexpected payload: %s", payload)
        return
    profile_id_from_payload = parts[1]
    # founding-флаг — ищем "founding=1" среди опциональных хвостовых полей payload (форвард-совместимо).
    is_founding_payment = any(p == "founding=1" for p in parts[3:])

    # Для recurring подписок Telegram присылает subscription_expiration_date (Unix timestamp)
    sub_exp = getattr(sp, "subscription_expiration_date", None)
    is_recurring_flag = getattr(sp, "is_recurring", False) or sub_exp is not None

    # Сырой event — в jsonb для аудита
    raw = {
        "telegram_payment_charge_id":   sp.telegram_payment_charge_id,
        "provider_payment_charge_id":   sp.provider_payment_charge_id,
        "currency":                     sp.currency,
        "total_amount":                 sp.total_amount,
        "invoice_payload":              sp.invoice_payload,
        "subscription_expiration_date": sub_exp,
        "is_recurring":                 is_recurring_flag,
        "is_first_recurring":           getattr(sp, "is_first_recurring", None),
        "telegram_id":                  user.id,
        "username":                     user.username,
    }

    # Проверяем профиль по telegram_id (первоисточник правды)
    profile = await get_profile_by_telegram(user.id)
    if not profile or profile["id"] != profile_id_from_payload:
        # payload и telegram_id не совпали — это подозрительно, но платёж реальный:
        # верим текущему telegram_id, если профиль есть
        log.warning("payload profile_id mismatch: payload=%s, actual=%s (telegram_id=%s)",
                    profile_id_from_payload, profile["id"] if profile else None, user.id)
    if not profile:
        log.error("successful_payment: no profile for telegram_id=%s", user.id)
        await msg.reply_text(
            "⚠️ Payment received but we couldn't find your account. "
            "Please contact support@belfed.com with this code: "
            f"{sp.telegram_payment_charge_id}"
        )
        return

    lang = await get_user_lang(update, profile)

    # Запись в БД через RPC
    # capture pre-payment trial_end for rollover reporting
    trial_end_before = parse_ts(profile.get("subscription_expires_at")) if profile else None

    ok = await apply_stars_payment_via_rpc(
        telegram_charge_id=sp.telegram_payment_charge_id,
        user_id=profile["id"],
        amount_stars=sp.total_amount,
        subscription_expiration_date=sub_exp,
        paid_at=datetime.now(timezone.utc),
        raw_event=raw,
        is_recurring=is_recurring_flag,
    )
    if not ok:
        log.error("apply_stars_payment_via_rpc returned False")
        await msg.reply_text(
            "⚠️ Payment received but activation failed. "
            "Please contact support@belfed.com with this code: "
            f"{sp.telegram_payment_charge_id}"
        )
        return

    claim_res = None
    # Если это founding-платёж — закрепляем слот и убираем pending-claim.
    # Ошибка claim не блокирует выдачу доступа — платёж реальный, юзер имеет право на доступ.
    if is_founding_payment:
        claim_res = await claim_founding_slot_via_rpc(
            profile["id"], lang,
            source="telegram_stars",
            notes=f"charge={sp.telegram_payment_charge_id}",
        )
        if claim_res and claim_res.get("ok"):
            log.info("founding slot claimed via stars: user=%s no=%s",
                     profile["id"], claim_res.get("founding_member_no"))
        else:
            log.error("claim_founding_slot failed after stars payment: user=%s res=%s",
                     profile["id"], claim_res)

    # Генерируем invite в EN-group
    invite = await grant_paid_invite(context, user.id, lang)

    # Читаем свежий expires_at
    profile_after = await get_profile_by_telegram(user.id)
    exp = parse_ts(profile_after.get("subscription_expires_at")) if profile_after else None
    until_str = exp.strftime("%d.%m.%Y") if exp else "—"

    if invite:
        await msg.reply_text(
            T(lang, "stars_payment_received").format(until=until_str, invite=invite),
            disable_web_page_preview=True,
        )
    else:
        await msg.reply_text(
            T(lang, "stars_payment_no_invite").format(until=until_str),
            disable_web_page_preview=True,
        )
        await send_main_menu(update, context, lang=lang)

    # Stars users almost always have ghost email (tg<id>@belfed.local) from the trial flow.
    # Now that they've paid, ask them for a real email + privacy consent via a web form
    # (link them to /upgrade-email.html with a single-use, 24h token).
    try:
        if profile.get("is_lite_profile"):
            upgrade_token = await create_stars_upgrade_intent(profile["id"], user.id, lang)
            if upgrade_token:
                # Stars is EN-only flow — form lives on belfed.com regardless of UI lang.
                upgrade_url = f"{WEB_URL_EN}/upgrade-email.html?stars_token={upgrade_token}&lang={lang}"
                if lang == "ru":
                    upgrade_txt = (
                        "✉️ Пожалуйста, укажите ваш email — это нужно для работы с личным кабинетом, "
                        "восстановления доступа и важных сервисных уведомлений. Ссылка действует 24 часа."
                    )
                    upgrade_btn = "✉️ Добавить email"
                else:
                    upgrade_txt = (
                        "✉️ Please add your email — it’s required for member-dashboard recovery "
                        "and important service notifications. Link valid 24 hours, no marketing spam."
                    )
                    upgrade_btn = "✉️ Add email"
                await msg.reply_text(
                    upgrade_txt,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(upgrade_btn, url=upgrade_url)]]),
                    disable_web_page_preview=True,
                )
    except Exception as e:
        log.warning("post-Stars upgrade-email prompt failed: %s", e)

    # Web dashboard — без magic-link, простой редирект на members.html.
    # Привязка TG↔веб делается при регистрации на сайте.
    try:
        if lang == "ru":
            url = "https://belfed.ru/members.html"
            btn_label = "🌐 Открыть belfed.ru/members.html"
            txt = (
                "📊 Личный кабинет — открытые позиции, аналитика, история сделок.\n\n"
                "Для входа нужно зарегистрироваться по email на сайте.\n"
                "Регистрация автоматически свяжется с вашим Telegram."
            )
        else:
            url = "https://belfed.com/members.html"
            btn_label = "🌐 Open belfed.com/members.html"
            txt = (
                "📊 Member dashboard — open positions, analytics, trade history.\n\n"
                "To sign in, register with your email on the site.\n"
                "Registration will automatically link to your Telegram."
            )
        await msg.reply_text(
            txt,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(btn_label, url=url)]]),
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.warning("post-Stars web dashboard prompt failed: %s", e)


    # ── Notify admins about the new paid subscription (best-effort) ──
    try:
        await notify_admins_new_subscription(
            context=context,
            profile=profile,
            profile_after=profile_after,
            user=user,
            sp=sp,
            is_founding_payment=is_founding_payment,
            is_recurring_flag=is_recurring_flag,
            claim_res=claim_res,
            trial_end_before=trial_end_before,
        )
    except Exception as e:
        log.warning("admin notify call failed: %s", e)


# ---------- Roster: фиксируем приход/уход в TG-группах --------------------
async def roster_upsert(
    chat_id: int, telegram_id: int, status: str,
    username: str | None = None, first_name: str | None = None,
    last_name: str | None = None, is_admin: bool = False,
    custom_title: str | None = None, source: str = "chat_member_update",
    raw: dict | None = None,
) -> None:
    """Вызывает RPC roster_upsert. Ошибки логируем, но не прокидываем."""
    try:
        status_code, data = await sb_post(
            "/rest/v1/rpc/roster_upsert",
            {
                "p_chat_id":      chat_id,
                "p_telegram_id":  telegram_id,
                "p_status":       status,
                "p_username":     username,
                "p_first_name":   first_name,
                "p_last_name":    last_name,
                "p_is_admin":     bool(is_admin),
                "p_custom_title": custom_title,
                "p_source":       source,
                "p_raw":          raw,
            },
        )
        if status_code not in (200, 204):
            log.warning("roster_upsert RPC %s: %s", status_code, data)
    except Exception as e:
        log.warning("roster_upsert exc: %s", e)


async def on_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ловит любые изменения партиципирования в RU/EN платных группах
    и пишет свежий снимок в telegram_group_roster."""
    cmu = update.chat_member or update.my_chat_member
    if not cmu:
        return
    chat = cmu.chat
    new_member = cmu.new_chat_member
    if not new_member or not new_member.user:
        return

    chat_id = chat.id
    # Интересуют только наши две группы (RU + EN paid)
    interesting = {COMMUNITY_RU_ID}
    if COMMUNITY_EN_ID:
        interesting.add(COMMUNITY_EN_ID)
    if chat_id not in interesting:
        return

    user = new_member.user
    status = new_member.status or "member"
    is_admin = status in ("administrator", "creator")
    raw = {
        "old_status": (cmu.old_chat_member.status if cmu.old_chat_member else None),
        "new_status": status,
        "date":       getattr(cmu, "date", None).isoformat() if getattr(cmu, "date", None) else None,
        "from":       getattr(cmu.from_user, "id", None) if cmu.from_user else None,
    }

    await roster_upsert(
        chat_id=chat_id,
        telegram_id=user.id,
        status=status,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        is_admin=is_admin,
        custom_title=getattr(new_member, "custom_title", None),
        source="chat_member_update",
        raw=raw,
    )
    log.info("roster: chat=%s user=%s status=%s", chat_id, user.id, status)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("unhandled error", exc_info=context.error)

async def _set_bot_commands(application: Application):
    """Populate the Telegram client ‘/’ menu so users can discover commands.

    Falls back silently on failure — must not block bot startup.
    """
    try:
        ru_cmds = [
            BotCommand("start",     "Меню и приветствие"),
            BotCommand("request",   "Запросить анализ актива"),
            BotCommand("status",    "Моя подписка"),
            BotCommand("dashboard", "Открыть дашборд"),
            BotCommand("support",   "Написать в поддержку"),
            BotCommand("cancel",    "Отменить автопродление"),
            BotCommand("lang",      "Язык / Language"),
        ]
        en_cmds = [
            BotCommand("start",     "Menu and welcome"),
            BotCommand("request",   "Request asset analysis"),
            BotCommand("status",    "My subscription"),
            BotCommand("dashboard", "Open dashboard"),
            BotCommand("support",   "Contact support"),
            BotCommand("cancel",    "Cancel auto-renew"),
            BotCommand("lang",      "Language / Язык"),
        ]
        # Default scope = RU (primary audience); per-language overlays for EN/RU clients.
        await application.bot.set_my_commands(ru_cmds, scope=BotCommandScopeDefault())
        await application.bot.set_my_commands(ru_cmds, scope=BotCommandScopeDefault(), language_code="ru")
        await application.bot.set_my_commands(en_cmds, scope=BotCommandScopeDefault(), language_code="en")
        log.info("Bot commands menu registered (RU default + EN overlay)")
    except Exception as e:
        log.warning("set_my_commands failed: %s", e)


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(_set_bot_commands).build()
    app.add_handler(CommandHandler("start",          cmd_start))
    app.add_handler(CommandHandler("link",           cmd_link))
    app.add_handler(CommandHandler("status",         cmd_status))
    app.add_handler(CommandHandler("support",        cmd_support))
    app.add_handler(CommandHandler("cabinet",        cmd_cabinet))
    app.add_handler(CommandHandler("cancel",         cmd_cancel))
    app.add_handler(CommandHandler("cancel_payment", cmd_cancel_payment))
    app.add_handler(CommandHandler("skip",           cmd_skip))
    app.add_handler(CommandHandler("lang",           cmd_lang))
    app.add_handler(CommandHandler("dashboard",      cmd_dashboard))
    app.add_handler(CommandHandler("request",        cmd_request))
    app.add_handler(CommandHandler("payments",       cmd_payments))
    app.add_handler(CommandHandler("subscribers",    cmd_subscribers))
    app.add_handler(CommandHandler("reply",          cmd_reply))
    app.add_handler(CommandHandler("dm",             cmd_dm))
    # Positions management commands (admin-only) — must register BEFORE the
    # generic CallbackQueryHandler so command handlers fire first.
    positions.register(app)
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))
    # Roster: журналируем все изменения участия в RU/EN группах
    app.add_handler(ChatMemberHandler(on_chat_member_update,
                                       ChatMemberHandler.CHAT_MEMBER))
    # Telegram Stars: pre-checkout + successful payment
    app.add_handler(PreCheckoutQueryHandler(on_pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_successful_payment))
    app.add_error_handler(on_error)
    log.info("BelFed bot (RU YooKassa + EN Telegram Stars, multilingual) running")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
