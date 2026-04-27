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
from datetime import datetime, timedelta, timezone

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters,
)

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
PRICE_RUB            = os.environ.get("PRICE_MONTHLY_RUB", "1500")
PRICE_USD            = os.environ.get("PRICE_MONTHLY_USD", "19")
BOT_SHARED_SECRET    = os.environ.get("BOT_SHARED_SECRET", "")
BOT_CLAIM_TRIAL_URL  = os.environ.get(
    "BOT_CLAIM_TRIAL_URL",
    f"{SUPABASE_URL}/functions/v1/bot-claim-trial",
)
YOOKASSA_CREATE_URL  = os.environ.get(
    "YOOKASSA_CREATE_PAYMENT_URL",
    f"{SUPABASE_URL}/functions/v1/yookassa-create-payment",
)
PREVIEW_CHANNEL_URL  = os.environ.get("TELEGRAM_PREVIEW_CHANNEL_URL", "").strip()

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
                                lang: str = "ru") -> dict | None:
    """Создаёт lite-профиль с триалом и возвращает invite-ссылку (RU или EN)."""
    if not BOT_SHARED_SECRET:
        log.error("BOT_SHARED_SECRET is not set; cannot call bot-claim-trial")
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                BOT_CLAIM_TRIAL_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-bot-secret": BOT_SHARED_SECRET,
                },
                json={
                    "telegram_id":       str(telegram_id),
                    "telegram_username": username or "",
                    "source":            source,
                    "lang":              lang,
                },
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
                "select": "status,plan_code,current_period_end,cancel_at_period_end,payment_method_id"},
    )
    return rows[0] if rows else None

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
                        f"Оформить подписку {PRICE_RUB} ₽ / мес: " + WEB_URL_RU + "/members.html#subscribe"),
    "status_active":   ("✅ Подписка активна\n"
                        f"План: monthly ({PRICE_RUB} ₽ / мес)\n"
                        "Действует до: {until}\n{autorenew}"),
    "status_none":     "❌ Подписки нет.\n\n" + f"Оформить ({PRICE_RUB} ₽ / мес): " + WEB_URL_RU + "/members.html#subscribe",
    "autorenew_on":    "Автопродление: включено",
    "autorenew_off":   "Автопродление: отключено · доступ закончится в указанную дату",
    "need_link":       "Сначала привяжите аккаунт сайта: " + WEB_URL_RU + "/members.html",
    "link_already":    "✅ Аккаунт уже привязан к этому Telegram. /status — посмотреть подписку.",
    "link_ok":         "✅ Telegram привязан. Бесплатный доступ открыт на 14 дней — без привязки карты.",
    "link_bad":        "⚠️ Токен недействителен или истёк. Сгенерируйте новый на сайте.",
    "cancel_ok":       "Автопродление отключено. Доступ сохранится до {until}.",
    "cancel_none":     "У вас нет активной подписки для отмены.",
    "btn_pay":         f"💳 Оформить — {PRICE_RUB} ₽ / мес",
    "btn_paid":        "📺 Закрытый канал",
    "btn_status":      "📋 Моя подписка",
    "btn_disclaimer":  "⚠️ Disclaimer",
    "btn_link":        "🔗 Привязать аккаунт",
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
        + WEB_URL_RU + "/members.html#subscribe"
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
    "lang_pick_title": "🌐 Выберите язык / Choose language:",
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
                        f"Subscribe (${PRICE_USD} / mo): " + WEB_URL_EN + "/members.html#subscribe"),
    "status_active":   ("✅ Subscription active\n"
                        f"Plan: monthly (${PRICE_USD} / mo)\n"
                        "Valid until: {until}\n{autorenew}"),
    "status_none":     "❌ No active subscription.\n\n" + f"Subscribe (${PRICE_USD} / mo): " + WEB_URL_EN + "/members.html#subscribe",
    "autorenew_on":    "Auto-renew: on",
    "autorenew_off":   "Auto-renew: off · access ends on the date above",
    "need_link":       "Please link your account first: " + WEB_URL_EN + "/members.html",
    "link_already":    "✅ Your account is already linked to this Telegram. Use /status to view subscription.",
    "link_ok":         "✅ Telegram linked. 14 days of free access — no card required.",
    "link_bad":        "⚠️ Token invalid or expired. Generate a new one on the site.",
    "cancel_ok":       "Auto-renew disabled. Access remains until {until}.",
    "cancel_none":     "You don't have an active subscription to cancel.",
    "btn_pay":         f"💳 Subscribe — ${PRICE_USD} / mo",
    "btn_paid":        "📺 Private channel",
    "btn_status":      "📋 My subscription",
    "btn_disclaimer":  "⚠️ Disclaimer",
    "btn_link":        "🔗 Link account",
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
    "trial_claim_used": (
        "⚠️ The trial for this Telegram account has already been used.\n\n"
        f"To continue, subscribe (${PRICE_USD} / mo): "
        + WEB_URL_EN + "/members.html#subscribe"
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
                         reply_target):
    """Вызывает edge-функцию и шлёт ответ + меню. reply_target — message или query.message."""
    res = await claim_trial_via_edge(user_id, username, source=source, lang=lang)
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
    else:
        err = res.get("error")
        if err == "trial_already_used":
            await reply_target.reply_text(T(lang, "trial_claim_used"))
        else:
            log.warning("claim_trial returned error [lang=%s]: %s", lang, res)
            await reply_target.reply_text(T(lang, "trial_claim_error"))
    await send_main_menu(update, context, lang=lang, reply_to=reply_target)

# ---------- Payment flow with email collection ---------------------------
async def start_payment_with_email(query_or_message, context: ContextTypes.DEFAULT_TYPE,
                                    profile: dict, lang: str):
    """Если у юзера уже есть валидный email — сразу создаёт платёж.
    Иначе — переводит юзера в state 'awaiting_email' и просит email."""
    existing_email = profile.get("email")
    if is_valid_email(existing_email):
        await create_and_send_payment(query_or_message, context, profile, existing_email, lang)
        return

    # Переходим в режим сбора email
    context.user_data["awaiting_email_for_payment"] = {
        "profile_id": profile["id"],
        "lang":       lang,
    }
    await query_or_message.reply_text(T(lang, "ask_email"))

async def create_and_send_payment(reply_target, context: ContextTypes.DEFAULT_TYPE,
                                   profile: dict, email: str, lang: str):
    """Создаёт платёж с email и шлёт пользователю кнопку оплаты."""
    await reply_target.reply_text(T(lang, "pay_creating"))
    web_url = WEB_URL_EN if lang == "en" else WEB_URL_RU
    return_url = f"{web_url}/members.html?payment=return"
    res = await create_payment_via_edge(profile["id"], email, return_url)
    if not res or not res.get("confirmation_url"):
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

    # Если /start пришёл во время сбора email — выходим из режима
    context.user_data.pop("awaiting_email_for_payment", None)

    # Deep-link /start trial[_xxx][_ru|_en]
    if args and (args[0] == "trial" or args[0].startswith("trial_")):
        source = args[0]
        # 1) определяем язык: суффикс источника > профиль > Telegram language_code
        lang_from_src = detect_lang_from_source(source)
        profile = await get_profile_by_telegram(user.id)
        if lang_from_src:
            lang = lang_from_src
        elif profile and profile.get("lang") in ("ru", "en"):
            lang = profile["lang"]
        else:
            # Спрашиваем язык. Сохраняем источник в user_data.
            context.user_data["pending_trial_source"] = source
            await update.message.reply_text(
                TEXTS_RU["lang_pick_title"],
                reply_markup=lang_pick_keyboard("lang_trial"),
            )
            return
        await run_trial_flow(update, context, user.id, user.username,
                              source=source, lang=lang,
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
        await send_main_menu(update, context, lang=profile["lang"])
        return

    # Нет профиля — спрашиваем язык
    await update.message.reply_text(
        TEXTS_RU["lang_pick_title"],
        reply_markup=lang_pick_keyboard("lang_menu"),
    )

async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          lang: str = "ru", reply_to=None):
    user = update.effective_user
    profile = await get_profile_by_telegram(user.id)
    web_url = WEB_URL_EN if lang == "en" else WEB_URL_RU

    rows = []
    if has_access(profile):
        rows.append([InlineKeyboardButton(T(lang, "btn_paid"), callback_data="paid_invite")])

    rows.append([InlineKeyboardButton(T(lang, "btn_status"), callback_data="sub_status")])

    sub = await get_subscription(profile["id"]) if profile else None
    has_active_paid = sub and sub.get("status") == "active"
    if not has_active_paid and not is_admin(profile):
        if profile:
            # Бот сам создаёт платёж — Telegram-only flow
            rows.append([InlineKeyboardButton(T(lang, "btn_pay"), callback_data="start_payment")])
        else:
            # Нет профиля — fallback на сайт (редкий случай, юзер открыл бота без deep-link)
            rows.append([InlineKeyboardButton(T(lang, "btn_pay"), url=f"{web_url}/members.html#subscribe")])
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

async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /lang — позволяет сменить язык в любой момент."""
    await update.message.reply_text(
        TEXTS_RU["lang_pick_title"],
        reply_markup=lang_pick_keyboard("lang_menu"),
    )

async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает обычный текст. Сейчас — только email при оплате."""
    state = context.user_data.get("awaiting_email_for_payment")
    if not state:
        return  # текст вне известных режимов — игнорируем

    user = update.effective_user
    text = (update.message.text or "").strip()
    lang = state.get("lang", "ru")

    if not is_valid_email(text):
        await update.message.reply_text(T(lang, "email_invalid"))
        return

    # Сохраняем email в БД
    profile_id = state["profile_id"]
    await update_profile_email(profile_id, text)

    # Выходим из режима
    context.user_data.pop("awaiting_email_for_payment", None)

    await update.message.reply_text(T(lang, "email_saved").format(email=text))

    # Создаём платёж
    profile = await get_profile_by_telegram(user.id)
    if not profile:
        await update.message.reply_text(T(lang, "pay_no_profile"))
        return
    await create_and_send_payment(update.message, context, profile, text, lang)

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    data = query.data or ""

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
            await run_trial_flow(update, context, user.id, user.username,
                                  source=source, lang=lang,
                                  reply_target=query.message)
        else:
            await send_main_menu(update, context, lang=lang, reply_to=query.message)
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

    if data == "start_payment":
        profile = await get_profile_by_telegram(user.id)
        lang = await get_user_lang(update, profile)
        if not profile:
            await query.message.reply_text(T(lang, "pay_no_profile"))
            return
        await start_payment_with_email(query.message, context, profile, lang)
        return

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("unhandled error", exc_info=context.error)

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",          cmd_start))
    app.add_handler(CommandHandler("link",           cmd_link))
    app.add_handler(CommandHandler("status",         cmd_status))
    app.add_handler(CommandHandler("cancel",         cmd_cancel))
    app.add_handler(CommandHandler("cancel_payment", cmd_cancel_payment))
    app.add_handler(CommandHandler("lang",           cmd_lang))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))
    app.add_error_handler(on_error)
    log.info("BelFed bot (RU+EN multilingual, single plan + trial + email) running")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
