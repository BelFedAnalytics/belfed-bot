"""
BelFed bot — module для микро-опросов участников (Founding Member feedback).

Две волны:
  fm0 — в день оплаты: почему оплатил, где нашёл, торгует или инвестирует,
        опыт, интересные активы, как планирует использовать сервис.
  fm7 — через неделю: оценки продуктовых блоков, готовность рекомендовать.

Принципы:
  - всё кнопками, ввод текста только в необязательном финальном вопросе;
  - одно сообщение на весь опрос, оно перерисовывается на каждом шаге;
  - ответы пишутся в public.member_surveys после каждого нажатия, поэтому
    брошенный на середине опрос это тоже данные;
  - в answers хранятся короткие коды, а не подписи кнопок: подписи разные в
    двух языках и со временем меняются, коды остаются сопоставимыми.

Админские команды:
  /survey <telegram_id> <fm0|fm7> — отправить волну опроса участнику
  /fmletter <telegram_id> [ru|en] — отправить письмо Founding Member с кнопками
  /surveys                        — сводка по заполненным опросам
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

log = logging.getLogger("belfed-bot.surveys")

ADMIN_TELEGRAM_IDS = {
    int(x.strip()) for x in os.environ.get("BELFED_ADMIN_TELEGRAM_IDS", "118296372").split(",")
    if x.strip().isdigit()
}
SUPABASE_URL         = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
BOT_USERNAME         = os.environ.get("BELFED_BOT_USERNAME", "belfedbot")

SB_HEADERS = {
    "apikey":        SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type":  "application/json",
}

# Окно, в течение которого текст после финального экрана считается открытым
# ответом, если состояние в памяти потеряно (например, бот перезапустился).
OPEN_ANSWER_WINDOW_MIN = 30

# ---------- Определение волн ----------------------------------------------
# max_pick > 1 — мультивыбор с кнопкой «готово».
WAVES: dict[str, dict[str, Any]] = {
    "fm0": {
        "intro": {
            "ru": "Короткий опрос, шесть вопросов по одному нажатию.",
            "en": "A short survey, six questions, one tap each.",
        },
        "questions": [
            {
                "key": "q1", "max_pick": 2,
                "text": {
                    "ru": "Какая часть сервиса убедила вас оформить подписку?",
                    "en": "Which part of the service made you decide to subscribe?",
                },
                "options": [
                    ("analytics", {"ru": "Аналитика и обзоры рынка", "en": "Market Analytics and Reviews"}),
                    ("assets",    {"ru": "Анализ активов и запросы", "en": "Asset Analysis and Requests"}),
                    ("trades",    {"ru": "Торговые операции",        "en": "Trade Operations"}),
                    ("other",     {"ru": "Другое",                   "en": "Something Else"}),
                ],
            },
            {
                "key": "q2", "max_pick": 1,
                "text": {"ru": "Где вы нас нашли?", "en": "Where did you find us?"},
                "options": [
                    ("x",           {"ru": "X (Twitter)",    "en": "X (Twitter)"}),
                    ("tradingview", {"ru": "TradingView",    "en": "TradingView"}),
                    ("telegram",    {"ru": "Telegram",       "en": "Telegram"}),
                    ("youtube",     {"ru": "YouTube",        "en": "YouTube"}),
                    ("google",      {"ru": "Поиск Google",   "en": "Google search"}),
                    ("referral",    {"ru": "По рекомендации", "en": "Someone recommended us"}),
                    ("other",       {"ru": "Другое",         "en": "Other"}),
                ],
            },
            {
                "key": "q3", "max_pick": 1,
                "text": {"ru": "Вы торгуете или инвестируете сами?",
                         "en": "Do you trade or invest yourself?"},
                "options": [
                    ("trading",   {"ru": "Активно торгую",        "en": "I trade actively"}),
                    ("investing", {"ru": "Инвестирую долгосрочно", "en": "I invest long term"}),
                    ("both",      {"ru": "И то, и другое",        "en": "Both"}),
                    ("starting",  {"ru": "Только начинаю",        "en": "Just getting started"}),
                ],
            },
            {
                "key": "q4", "max_pick": 1,
                "text": {"ru": "Сколько времени вы на рынках?",
                         "en": "How long have you been in the markets?"},
                "options": [
                    ("lt1", {"ru": "Меньше года",   "en": "Under a year"}),
                    ("1_3", {"ru": "1-3 года",      "en": "1-3 years"}),
                    ("3_7", {"ru": "3-7 лет",       "en": "3-7 years"}),
                    ("gt7", {"ru": "Больше 7 лет",  "en": "7+ years"}),
                ],
            },
            {
                "key": "q5", "max_pick": 3,
                "text": {"ru": "Какие активы вам интересны?",
                         "en": "Which assets are you most interested in?"},
                "options": [
                    ("crypto",   {"ru": "Крипто",                 "en": "Crypto"}),
                    ("equities", {"ru": "Акции",                  "en": "Equities"}),
                    ("metals",   {"ru": "Драгметаллы",            "en": "Precious metals"}),
                    ("energy",   {"ru": "Нефть и энергоносители", "en": "Oil and energy"}),
                    ("fx",       {"ru": "Валюты",                 "en": "FX"}),
                    ("indices",  {"ru": "Индексы",                "en": "Indices"}),
                ],
            },
            {
                "key": "q6", "max_pick": 1,
                "text": {"ru": "Как вы планируете использовать BelFed?",
                         "en": "How do you plan to use BelFed?"},
                "options": [
                    ("ideas",        {"ru": "Готовые торговые идеи",         "en": "Ready-to-use trade ideas"}),
                    ("confirmation", {"ru": "Подтверждение своих идей",      "en": "Confirmation for my own ideas"}),
                    ("context",      {"ru": "Контекст и направление рынка",  "en": "Market context and direction"}),
                    ("time",         {"ru": "Экономия времени на анализе",   "en": "Saving time on research"}),
                    ("learning",     {"ru": "Изучение вашего подхода к анализу",
                                      "en": "Learning how you analyse markets"}),
                ],
            },
        ],
        "final": {
            "ru": ("Готово, спасибо. Ваши ответы напрямую идут в планирование покрытия.\n\n"
                   "Будем рады услышать, как вам работают наши сервисы и что вы хотели бы "
                   "добавить. Напишите пару строк прямо здесь, это приходит лично мне. "
                   "Необязательно."),
            "en": ("That is it, thank you. Your answers go straight into how we plan coverage.\n\n"
                   "We would also be glad to hear how our services work for you and what you "
                   "would like us to add. Write a line or two right here, it comes to me "
                   "directly. Optional."),
        },
    },
    "fm7": {
        "intro": {
            "ru": ("Неделя в статусе Founding Member. Четыре нажатия, и они определят, "
                   "что мы сделаем следующим."),
            "en": ("One week as a Founding Member. Four taps, and it decides what we build next."),
        },
        "questions": [
            {
                "key": "r_analytics", "max_pick": 1, "scale": True,
                "text": {"ru": "Оцените: Аналитика и обзоры рынка",
                         "en": "Rate: Market Analytics and Reviews"},
            },
            {
                "key": "r_assets", "max_pick": 1, "scale": True,
                "text": {"ru": "Оцените: Анализ активов и запросы",
                         "en": "Rate: Asset Analysis and Requests"},
            },
            {
                "key": "r_trades", "max_pick": 1, "scale": True,
                "text": {"ru": "Оцените: Торговые операции",
                         "en": "Rate: Trade Operations"},
            },
            {
                "key": "r_product", "max_pick": 1, "scale": True,
                "text": {"ru": "Оцените: Бот и кабинет участника",
                         "en": "Rate: The bot and member dashboard"},
            },
            {
                "key": "q_rec", "max_pick": 1,
                "text": {"ru": "Порекомендуете BelFed тому, кто торгует?",
                         "en": "Would you recommend BelFed to someone who trades?"},
                "options": [
                    ("yes",      {"ru": "Да",        "en": "Yes"}),
                    ("probably", {"ru": "Скорее да", "en": "Probably"}),
                    ("notyet",   {"ru": "Пока нет",  "en": "Not yet"}),
                ],
            },
        ],
        "final": {
            "ru": ("Спасибо. Это прямо влияет на то, что мы делаем дальше.\n\n"
                   "Если есть одна вещь, которую стоит улучшить в первую очередь, "
                   "напишите её здесь. Необязательно."),
            "en": ("Thank you. This feeds directly into what we do next.\n\n"
                   "If there is one thing we should improve first, write it here. Optional."),
        },
    },
}

SCALE_LABELS = {
    "ru": {"1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "skip": "Не пользовался"},
    "en": {"1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "skip": "Did not use"},
}
DONE_LABEL = {"ru": "Готово", "en": "Done"}
NOTHING_PICKED = {"ru": "Сначала выберите вариант", "en": "Pick an option first"}
SAVED_TOAST = {"ru": "Записал, спасибо", "en": "Saved, thank you"}


async def _profile_of(context, telegram_id: int) -> dict | None:
    getter = context.application.bot_data.get("get_profile_by_telegram")
    if not getter:
        return None
    try:
        return await getter(telegram_id)
    except Exception:
        log.warning("surveys: profile lookup failed for tg=%s", telegram_id)
        return None


def letter_keyboard_without_survey(markup) -> InlineKeyboardMarkup | None:
    """Снимает кнопку опроса под письмом, остальные оставляет: опрос
    ушёл отдельным сообщением, повторное нажатие не нужно."""
    if not markup:
        return None
    rows = [[b for b in row if not (b.callback_data or "").startswith("sv:start:")]
            for row in markup.inline_keyboard]
    rows = [r for r in rows if r]
    return InlineKeyboardMarkup(rows) if rows else None


def _options_for(q: dict, lang: str) -> list[tuple[str, str]]:
    if q.get("scale"):
        lab = SCALE_LABELS[lang]
        return [(c, lab[c]) for c in ("1", "2", "3", "4", "5", "skip")]
    return [(code, labels[lang]) for code, labels in q["options"]]


# ---------- Supabase REST -------------------------------------------------
async def _sb(method: str, path: str, params: dict | None = None,
              body: dict | None = None, prefer: str | None = None):
    headers = dict(SB_HEADERS)
    if prefer:
        headers["Prefer"] = prefer
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.request(method, f"{SUPABASE_URL}/rest/v1/{path}",
                                 headers=headers, params=params or {}, json=body)
    if r.status_code not in (200, 201, 204):
        log.warning("surveys sb %s %s -> %s %s", method, path, r.status_code, r.text[:300])
        return None
    try:
        return r.json()
    except Exception:
        return []


async def get_row(profile_id: str, wave: str) -> dict | None:
    rows = await _sb("GET", "member_surveys", params={
        "profile_id": f"eq.{profile_id}", "wave": f"eq.{wave}",
        "select": "id,answers,open_answer,completed_at,lang,telegram_id",
    })
    return rows[0] if rows else None


async def upsert_row(profile_id: str, telegram_id: int, wave: str, lang: str,
                     sent_by: str = "manual") -> dict | None:
    rows = await _sb("POST", "member_surveys",
                     params={"on_conflict": "profile_id,wave"},
                     body={"profile_id": profile_id, "telegram_id": telegram_id,
                           "wave": wave, "lang": lang, "sent_by": sent_by},
                     prefer="resolution=merge-duplicates,return=representation")
    return rows[0] if rows else None


async def save_answers(row_id: int, answers: dict, completed: bool = False) -> bool:
    body: dict[str, Any] = {"answers": answers}
    if completed:
        body["completed_at"] = datetime.now(timezone.utc).isoformat()
    res = await _sb("PATCH", "member_surveys", params={"id": f"eq.{row_id}"},
                    body=body, prefer="return=minimal")
    return res is not None


async def save_open_answer(row_id: int, text: str) -> bool:
    res = await _sb("PATCH", "member_surveys", params={"id": f"eq.{row_id}"},
                    body={"open_answer": text[:2000]}, prefer="return=minimal")
    return res is not None


async def pending_open_answer(telegram_id: int) -> dict | None:
    """Опрос, завершённый недавно и без открытого ответа. Страховка на случай
    потери состояния в памяти (перезапуск бота между шагами)."""
    since = (datetime.now(timezone.utc)
             - timedelta(minutes=OPEN_ANSWER_WINDOW_MIN)).isoformat()
    rows = await _sb("GET", "member_surveys", params={
        "telegram_id": f"eq.{telegram_id}",
        "open_answer": "is.null",
        "completed_at": f"gte.{since}",
        "select": "id,wave,lang", "order": "completed_at.desc", "limit": "1",
    })
    return rows[0] if rows else None


# ---------- Рендер --------------------------------------------------------
def _progress(wave: str, idx: int, lang: str) -> str:
    total = len(WAVES[wave]["questions"])
    return f"{idx + 1}/{total}"


def _keyboard(wave: str, q: dict, lang: str, picked: list[str]) -> InlineKeyboardMarkup:
    opts = _options_for(q, lang)
    multi = q["max_pick"] > 1
    rows: list[list[InlineKeyboardButton]] = []
    per_row = 5 if q.get("scale") else 2
    buf: list[InlineKeyboardButton] = []
    for code, label in opts:
        if q.get("scale") and code == "skip":
            if buf:
                rows.append(buf)
                buf = []
            rows.append([InlineKeyboardButton(label, callback_data=f"sv:{wave}:{q['key']}:{code}")])
            continue
        mark = "✅ " if multi and code in picked else ""
        buf.append(InlineKeyboardButton(f"{mark}{label}",
                                        callback_data=f"sv:{wave}:{q['key']}:{code}"))
        if len(buf) == per_row:
            rows.append(buf)
            buf = []
    if buf:
        rows.append(buf)
    if multi:
        rows.append([InlineKeyboardButton(DONE_LABEL[lang],
                                          callback_data=f"sv:{wave}:{q['key']}:_done")])
    return InlineKeyboardMarkup(rows)


def _question_text(wave: str, idx: int, lang: str, first: bool) -> str:
    q = WAVES[wave]["questions"][idx]
    head = ""
    if first:
        head = WAVES[wave]["intro"][lang] + "\n\n"
    hint = ""
    if q["max_pick"] > 1:
        hint = ("\nМожно выбрать до %d, затем «Готово»." % q["max_pick"]) if lang == "ru" \
            else ("\nPick up to %d, then \"Done\"." % q["max_pick"])
    return f"{head}{_progress(wave, idx, lang)}  {q['text'][lang]}{hint}"


def _q_index(wave: str, key: str) -> int:
    for i, q in enumerate(WAVES[wave]["questions"]):
        if q["key"] == key:
            return i
    return -1


# ---------- Отправка ------------------------------------------------------
async def send_wave(bot, telegram_id: int, profile_id: str, wave: str,
                    lang: str, sent_by: str = "manual") -> bool:
    if wave not in WAVES:
        return False
    row = await upsert_row(profile_id, telegram_id, wave, lang, sent_by)
    if not row:
        return False
    try:
        await bot.send_message(
            chat_id=telegram_id,
            text=_question_text(wave, 0, lang, first=True),
            reply_markup=_keyboard(wave, WAVES[wave]["questions"][0], lang, []),
        )
        return True
    except Exception as e:
        log.warning("send_wave failed tg=%s wave=%s: %s", telegram_id, wave, e)
        return False


# ---------- Обработка нажатий --------------------------------------------
async def maybe_handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    query = update.callback_query
    data = query.data or ""
    if not data.startswith("sv:"):
        return False
    # Кнопка «пройти опрос» под письмом: sv:start:<wave>
    if data.startswith("sv:start:"):
        wave = data.split(":", 2)[2]
        await query.answer()
        profile = await _profile_of(context, query.from_user.id)
        if not profile or wave not in WAVES:
            return True
        lang = (profile.get("lang") or "ru").lower()
        row = await get_row(profile["id"], wave)
        if row and row.get("completed_at"):
            await query.message.reply_text(
                "Опрос уже пройден, спасибо." if lang == "ru"
                else "You have already completed this survey, thank you.")
            return True
        if row and row.get("lang"):
            lang = (row["lang"] or lang).lower()
        await send_wave(context.bot, query.from_user.id, profile["id"], wave, lang,
                        sent_by="letter_button")
        try:
            await query.edit_message_reply_markup(
                reply_markup=letter_keyboard_without_survey(query.message.reply_markup))
        except Exception:
            pass
        return True

    parts = data.split(":", 3)
    if len(parts) != 4:
        await query.answer()
        return True
    _, wave, qkey, code = parts
    if wave not in WAVES:
        await query.answer()
        return True

    lang_default = "ru"
    profile = await _profile_of(context, query.from_user.id)
    if profile:
        lang_default = (profile.get("lang") or "ru").lower()
    profile_id = (profile or {}).get("id")

    row = None
    if profile_id:
        row = await get_row(profile_id, wave)
    if not row:
        await query.answer()
        return True
    lang = (row.get("lang") or lang_default).lower()
    if lang not in ("ru", "en"):
        lang = "ru"

    idx = _q_index(wave, qkey)
    if idx < 0:
        await query.answer()
        return True
    q = WAVES[wave]["questions"][idx]
    answers = dict(row.get("answers") or {})

    # Мультивыбор: нажатие переключает вариант, «Готово» переходит дальше.
    if q["max_pick"] > 1:
        picked = list(answers.get(qkey) or [])
        if code == "_done":
            if not picked:
                await query.answer(NOTHING_PICKED[lang], show_alert=False)
                return True
        else:
            if code in picked:
                picked.remove(code)
            elif len(picked) >= q["max_pick"]:
                await query.answer(
                    ("Можно выбрать не больше %d" % q["max_pick"]) if lang == "ru"
                    else ("Up to %d, please" % q["max_pick"]))
                return True
            else:
                picked.append(code)
            answers[qkey] = picked
            await save_answers(row["id"], answers)
            await query.answer()
            try:
                await query.edit_message_reply_markup(
                    reply_markup=_keyboard(wave, q, lang, picked))
            except Exception:
                pass
            return True
    else:
        answers[qkey] = code
        await query.answer()

    is_last = idx == len(WAVES[wave]["questions"]) - 1
    await save_answers(row["id"], answers, completed=is_last)

    if is_last:
        try:
            await query.edit_message_text(WAVES[wave]["final"][lang], reply_markup=None)
        except Exception:
            pass
        # Ждём необязательный свободный ответ следующим сообщением.
        context.user_data["awaiting_survey_open"] = {"row_id": row["id"], "lang": lang}
        await _notify_admins_complete(context, query.from_user, wave, answers, lang)
        return True

    nxt = WAVES[wave]["questions"][idx + 1]
    try:
        await query.edit_message_text(
            _question_text(wave, idx + 1, lang, first=False),
            reply_markup=_keyboard(wave, nxt, lang, list(answers.get(nxt["key"]) or [])),
        )
    except Exception as e:
        log.warning("survey edit failed: %s", e)
    return True


async def maybe_handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Свободный ответ после финального экрана. True — сообщение обработано."""
    msg = update.effective_message
    text = (msg.text or "").strip()
    if not text:
        return False
    state = context.user_data.get("awaiting_survey_open")
    row_id = None
    lang = "ru"
    if state:
        row_id, lang = state["row_id"], state["lang"]
    else:
        pend = await pending_open_answer(update.effective_user.id)
        if pend:
            row_id, lang = pend["id"], (pend.get("lang") or "ru")
    if not row_id:
        return False

    await save_open_answer(row_id, text)
    context.user_data.pop("awaiting_survey_open", None)
    await msg.reply_text("Спасибо, записал." if lang == "ru" else "Thank you, noted.")
    for admin_id in ADMIN_TELEGRAM_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=("📝 Survey open answer\n"
                      f"tg={update.effective_user.id} "
                      f"@{update.effective_user.username or '-'}\n\n{text[:1500]}"),
            )
        except Exception:
            pass
    return True


async def _notify_admins_complete(context, user, wave: str, answers: dict, lang: str) -> None:
    lines = [f"✅ Survey {wave} completed", f"tg={user.id} @{user.username or '-'} lang={lang}"]
    for q in WAVES[wave]["questions"]:
        val = answers.get(q["key"])
        if val is None:
            continue
        labels = dict(_options_for(q, lang))
        if isinstance(val, list):
            shown = ", ".join(labels.get(v, v) for v in val)
        else:
            shown = labels.get(val, val)
        lines.append(f"{q['key']}: {shown}")
    body = "\n".join(lines)
    for admin_id in ADMIN_TELEGRAM_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=body)
        except Exception:
            pass


# ---------- Письмо Founding Member ---------------------------------------
LETTER = {
    "en": (
        "{name}, thank you.\n\n"
        "You are now a Founding Member, number {n} of the first fifty on the English side. "
        "That is more than a payment to us: you backed the service before it became an "
        "obvious choice, and your status stays locked in for as long as your subscription "
        "is active.\n\n"
        "As a Founding Member, your terms are fixed:\n"
        "• 30% off the service price, for life\n"
        "• Priority handling of your asset breakdown requests\n"
        "• The option to freeze your subscription for up to 30 days without losing status\n\n"
        "We especially value your feedback because you used the full trial period, saw how "
        "we work firsthand, and made a deliberate decision to join as a Founding Member. "
        "BelFed is still in beta, and we are actively shaping the platform around our "
        "members. Could you spare two minutes for a quick survey? It runs right here in the "
        "bot, one tap per question, and it helps us aim our research and coverage at what "
        "you actually trade.\n\n"
        "{request_block}"
        "If anything is unclear or missing, use \"Contact support\" below and it reaches me "
        "personally. I answer Founding Members myself.\n\n"
        "Artem, BelFed Analytics"
    ),
    "ru": (
        "{name}, спасибо.\n\n"
        "Вы стали Founding Member, номер {n} из первых пятидесяти на русской стороне. "
        "Для нас это больше, чем оплата: вы поддержали сервис до того, как он стал "
        "очевидным выбором, и ваш статус закреплён на всё время активной подписки.\n\n"
        "Условия Founding Member зафиксированы:\n"
        "• 30% от цены сервиса, навсегда\n"
        "• Приоритетная обработка ваших запросов на разбор активов\n"
        "• Возможность заморозить подписку до 30 дней без потери статуса\n\n"
        "Ваше мнение для нас особенно важно: вы прошли весь пробный период, увидели нашу "
        "работу изнутри и осознанно решили стать Founding Member. BelFed пока в бете, и мы "
        "строим сервис вокруг участников. Найдётся две минуты на короткий опрос? Он "
        "проходит прямо здесь, в боте, по одному нажатию на вопрос, и помогает нам "
        "направить исследования и покрытие на то, чем вы действительно торгуете.\n\n"
        "{request_block}"
        "Если что-то непонятно или чего-то не хватает, нажмите «Написать в поддержку» "
        "ниже, это приходит лично мне. Founding Member я отвечаю сам.\n\n"
        "Артём, BelFed Analytics"
    ),
}

REQUEST_BLOCK = {
    "en": ("One more thing: you have not sent us an asset request yet. Tap \"Request a "
           "breakdown\" below, pick the asset class and send a ticker. We will lay out the "
           "trend, the key levels and the probable price reactions. Median turnaround is "
           "about 3 hours, and as a Founding Member you go first in the queue.\n\n"),
    "ru": ("Ещё одно: вы пока не присылали нам запрос на разбор. Нажмите «Запросить "
           "разбор» ниже, выберите класс актива и отправьте тикер. Разберём тренд, "
           "ключевые уровни и вероятные реакции цены. Медианное время ответа около "
           "3 часов, а у Founding Member запрос идёт первым в очереди.\n\n"),
}

BTN = {
    "en": {"survey": "Take 2-minute survey", "request": "📊 Request a breakdown",
           "support": "💬 Contact support"},
    "ru": {"survey": "Пройти опрос, 2 минуты", "request": "📊 Запросить разбор",
           "support": "💬 Написать в поддержку"},
}


def letter_keyboard(wave: str, lang: str, with_request: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(BTN[lang]["survey"], callback_data=f"sv:start:{wave}")]]
    if with_request:
        rows.append([InlineKeyboardButton(
            BTN[lang]["request"], url=f"https://t.me/{BOT_USERNAME}?start=request")])
    rows.append([InlineKeyboardButton(BTN[lang]["support"], callback_data="feedback_open")])
    return InlineKeyboardMarkup(rows)


async def founding_number(profile_id: str, locale: str) -> int | None:
    rows = await _sb("GET", "profiles", params={
        "founding_member": "is.true", "founding_locale": f"eq.{locale}",
        "select": "id,founding_member_since", "order": "founding_member_since.asc",
    })
    if not rows:
        return None
    for i, r in enumerate(rows, start=1):
        if r["id"] == profile_id:
            return i
    return None


async def chart_requests_count(profile_id: str) -> int:
    rows = await _sb("GET", "chart_requests", params={
        "user_id": f"eq.{profile_id}", "select": "id", "limit": "1"})
    return len(rows or [])


# ---------- Админские команды --------------------------------------------
def _is_admin(uid: int) -> bool:
    return uid in ADMIN_TELEGRAM_IDS


async def cmd_survey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    args = context.args or []
    if len(args) < 2 or not args[0].isdigit() or args[1] not in WAVES:
        await update.message.reply_text("Usage: /survey <telegram_id> <fm0|fm7>")
        return
    tg_id, wave = int(args[0]), args[1]
    getter = context.application.bot_data.get("get_profile_by_telegram")
    profile = await getter(tg_id) if getter else None
    if not profile:
        await update.message.reply_text(f"❌ No profile for tg={tg_id}")
        return
    lang = (profile.get("lang") or "ru").lower()
    ok = await send_wave(context.bot, tg_id, profile["id"], wave, lang)
    await update.message.reply_text(
        f"✅ Survey {wave} sent to tg={tg_id} (lang={lang})" if ok
        else f"❌ Failed to send {wave} to tg={tg_id}")


async def cmd_fmletter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Usage: /fmletter <telegram_id> [ru|en] [n=<number>] [req=0|1]\n"
            "n= и req= переопределяют номер участника и абзац про запрос разбора, "
            "чтобы можно было проверить письмо на себе.")
        return
    tg_id = int(args[0])
    n_override: int | None = None
    req_override: bool | None = None
    for a in args[1:]:
        low = a.lower()
        if low.startswith("n=") and low[2:].isdigit():
            n_override = int(low[2:])
        elif low.startswith("req="):
            req_override = low[4:] in ("1", "yes", "true")
    getter = context.application.bot_data.get("get_profile_by_telegram")
    profile = await getter(tg_id) if getter else None
    if not profile:
        await update.message.reply_text(f"❌ No profile for tg={tg_id}")
        return
    lang = args[1].lower() if len(args) > 1 and args[1].lower() in ("ru", "en") \
        else (profile.get("lang") or "ru").lower()

    name = (profile.get("display_name") or "").strip()
    if not name:
        rows = await _sb("GET", "telegram_group_roster", params={
            "telegram_id": f"eq.{tg_id}", "select": "first_name", "limit": "1"})
        if rows and rows[0].get("first_name"):
            name = rows[0]["first_name"].strip()
    if not name:
        name = "Hi" if lang == "en" else "Здравствуйте"

    locale = profile.get("founding_locale") or lang
    n = n_override if n_override is not None \
        else (await founding_number(profile["id"], locale) or 0)
    with_request = req_override if req_override is not None \
        else (await chart_requests_count(profile["id"])) == 0
    text = LETTER[lang].format(
        name=name, n=n,
        request_block=REQUEST_BLOCK[lang] if with_request else "")

    # Готовим строку опроса заранее, чтобы кнопка сработала с первого нажатия.
    await upsert_row(profile["id"], tg_id, "fm0", lang, sent_by="fmletter")
    try:
        await context.bot.send_message(chat_id=tg_id, text=text,
                                       reply_markup=letter_keyboard("fm0", lang, with_request),
                                       disable_web_page_preview=True)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to deliver to {tg_id}: {e}")
        return
    await update.message.reply_text(
        f"✅ FM letter sent to tg={tg_id} (lang={lang}, number={n}, "
        f"request block={'yes' if with_request else 'no'})")


async def cmd_surveys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    rows = await _sb("GET", "member_surveys", params={
        "select": "wave,lang,telegram_id,answers,open_answer,completed_at",
        "order": "started_at.desc", "limit": "20"})
    if not rows:
        await update.message.reply_text("No surveys yet.")
        return
    out = []
    for r in rows:
        state = "done" if r.get("completed_at") else "in progress"
        answered = len([k for k, v in (r.get("answers") or {}).items() if v])
        out.append(f"{r['wave']} {r['lang']} tg={r['telegram_id']} {state}, "
                   f"{answered} answers"
                   + (", open answer" if r.get("open_answer") else ""))
    await update.message.reply_text("\n".join(out[:25]))


def register(app: Application) -> None:
    app.add_handler(CommandHandler("survey", cmd_survey))
    app.add_handler(CommandHandler("fmletter", cmd_fmletter))
    app.add_handler(CommandHandler("surveys", cmd_surveys))
