"""
BelFed bot — module для управления открытыми торговыми позициями.

Команды (доступны только админу — Артём, telegram_id=118296372):
  /new            — пошаговый мастер заведения позиции
  /list           — список открытых позиций (с inline-кнопками)
  /close TICKER   — закрыть позицию (запросит цену выхода)
  /move_stop TICKER NEW_STOP — сдвинуть стоп
  /comment TICKER — добавить комментарий с авто-переводом RU→EN
  /edit TICKER    — изменить любое поле
  /cancel_op      — отменить текущий мастер/режим

Также реагирует на:
  - InlineKeyboard кнопки (positions:close|<id>, positions:comment|<id>, ...)
  - Текстовые сообщения, когда юзер находится в режиме мастера или ввода комментария.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters,
)

log = logging.getLogger("belfed-bot.positions")

# ---------- Config ---------------------------------------------------------
ADMIN_TELEGRAM_IDS = {
    int(x.strip()) for x in os.environ.get("BELFED_ADMIN_TELEGRAM_IDS", "118296372").split(",")
    if x.strip().isdigit()
}
SUPABASE_URL         = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
BOT_SHARED_SECRET    = os.environ.get("BOT_SHARED_SECRET", "")
PUBLISHER_URL        = f"{SUPABASE_URL}/functions/v1/positions-publish"
TRANSLATE_URL        = f"{SUPABASE_URL}/functions/v1/positions-translate"

SB_HEADERS = {
    "apikey":        SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type":  "application/json",
}

# ---------- Supabase REST helpers (mini, scoped to this module) -----------
async def sb_select(table: str, params: dict) -> list[dict] | None:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{SUPABASE_URL}/rest/v1/{table}",
                             headers=SB_HEADERS, params=params)
        if r.status_code == 200:
            return r.json()
        log.warning("sb_select %s -> %s %s", table, r.status_code, r.text[:200])
        return None

async def sb_insert(table: str, body: dict) -> dict | None:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{SUPABASE_URL}/rest/v1/{table}",
                              headers={**SB_HEADERS, "Prefer": "return=representation"},
                              json=body)
        if r.status_code in (200, 201):
            data = r.json()
            return data[0] if isinstance(data, list) and data else data
        log.warning("sb_insert %s -> %s %s", table, r.status_code, r.text[:300])
        return None

async def sb_update(table: str, params: dict, body: dict) -> int:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.patch(f"{SUPABASE_URL}/rest/v1/{table}",
                               headers={**SB_HEADERS, "Prefer": "return=minimal"},
                               params=params, json=body)
        return r.status_code

async def call_publisher(event: str, position_id: int,
                          triggered_price: float | None = None) -> dict | None:
    payload: dict[str, Any] = {"event": event, "position_id": position_id}
    if triggered_price is not None:
        payload["triggered_price"] = triggered_price
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.post(PUBLISHER_URL,
                                  headers={"Content-Type": "application/json",
                                           "x-bot-secret": BOT_SHARED_SECRET},
                                  json=payload)
            return r.json() if r.status_code == 200 else None
        except Exception as e:
            log.warning("publisher call failed: %s", e)
            return None

async def call_translate(text_ru: str) -> str | None:
    """Calls positions-translate edge function. Returns EN translation or None."""
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.post(TRANSLATE_URL,
                                  headers={"Content-Type": "application/json",
                                           "x-bot-secret": BOT_SHARED_SECRET},
                                  json={"text": text_ru, "source": "ru", "target": "en"})
            if r.status_code == 200:
                return r.json().get("translation")
        except Exception as e:
            log.warning("translate call failed: %s", e)
    return None

# ---------- Auth -----------------------------------------------------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_TELEGRAM_IDS

async def deny(update: Update) -> None:
    msg = update.effective_message
    if msg:
        await msg.reply_text("Команда доступна только администратору.")

# ---------- Formatting helpers --------------------------------------------
def fmt_price(p: float | str | None) -> str:
    if p is None:
        return "—"
    try:
        v = float(p)
    except Exception:
        return str(p)
    if v >= 1000:
        return f"{v:,.2f}".replace(",", " ")
    if v >= 10:
        return f"{v:.2f}"
    return f"{v:.6f}".rstrip("0").rstrip(".")

def fmt_position_short(p: dict) -> str:
    arrow = "📈" if p["direction"] == "long" else "📉"
    targets = []
    for i, key in enumerate(("target_1", "target_2", "target_3"), 1):
        if p.get(key):
            hit = "✓" if p.get(f"{key}_hit_at") else ""
            targets.append(f"T{i}={fmt_price(p[key])}{hit}")
    return (
        f"{arrow} #{p['id']} <b>{p['ticker']}</b> · entry {fmt_price(p['entry_price'])} · "
        f"stop {fmt_price(p['stop_price'])} · " + " · ".join(targets)
    )

# ---------- /list ---------------------------------------------------------
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await deny(update)
    rows = await sb_select("active_positions",
                           {"status": "in.(open,partially_closed)",
                            "select": "id,asset_class,ticker,direction,status,entry_price,stop_price,"
                                      "target_1,target_2,target_3,"
                                      "target_1_hit_at,target_2_hit_at,target_3_hit_at",
                            "order": "opened_at.desc"})
    if not rows:
        await update.message.reply_text("Открытых позиций нет.")
        return
    lines = [f"<b>Открытые позиции ({len(rows)}):</b>"]
    keyboard = []
    for p in rows:
        lines.append(fmt_position_short(p))
        keyboard.append([
            InlineKeyboardButton(f"💬 {p['ticker']}", callback_data=f"positions:comment|{p['id']}"),
            InlineKeyboardButton(f"🛡 stop", callback_data=f"positions:movestop|{p['id']}"),
            InlineKeyboardButton(f"❌ close", callback_data=f"positions:close|{p['id']}"),
        ])
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True,
    )

# ---------- /new wizard ---------------------------------------------------
WIZARD_KEY = "positions_new_wizard"

WIZARD_STEPS = [
    "asset_class", "ticker", "direction", "entry", "stop",
    "target_1", "target_2", "target_3", "publish", "confirm",
]

WIZARD_PROMPTS = {
    "asset_class":  "Шаг 1/9. Тип актива: stock или crypto?",
    "ticker":       "Шаг 2/9. Тикер (например, BTC, AAPL):",
    "direction":    "Шаг 3/9. Направление: long или short?",
    "entry":        "Шаг 4/9. Цена входа (число, в USD):",
    "stop":         "Шаг 5/9. Стоп-лосс (число):",
    "target_1":     "Шаг 6/9. Цель 1 (число или \"—\" если нет):",
    "target_2":     "Шаг 7/9. Цель 2 (число или \"—\"):",
    "target_3":     "Шаг 8/9. Цель 3 (число или \"—\"):",
    "publish":      "Шаг 9/9. Публиковать в каналы RU/EN сейчас? (yes/no/ru/en)",
    "confirm":      "Подтвердить создание? (yes/no)",
}

def _parse_num(s: str) -> float | None:
    s = s.replace(",", ".").replace(" ", "").replace("$", "").strip()
    if s in ("", "—", "-", "none", "null", "skip"):
        return None
    try:
        return float(s)
    except Exception:
        return None

async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await deny(update)
    context.user_data[WIZARD_KEY] = {"step": 0, "data": {}}
    await update.message.reply_text(
        "📝 Новая позиция. Я буду спрашивать поэтапно. /cancel_op чтобы отменить.\n\n"
        + WIZARD_PROMPTS["asset_class"]
    )

async def _wizard_summary(d: dict) -> str:
    arrow = "📈" if d.get("direction") == "long" else "📉"
    pub = []
    if d.get("publish_to_ru"): pub.append("RU")
    if d.get("publish_to_en"): pub.append("EN")
    pub_str = ", ".join(pub) if pub else "не публиковать"
    lines = [
        f"<b>Проверь:</b>",
        f"{arrow} {d['asset_class']} {d['ticker']} {d['direction']}",
        f"Entry: {fmt_price(d['entry_price'])}",
        f"Stop:  {fmt_price(d['stop_price'])}",
    ]
    for i in (1, 2, 3):
        v = d.get(f"target_{i}")
        if v: lines.append(f"T{i}:    {fmt_price(v)}")
    lines.append(f"Публикация: {pub_str}")
    return "\n".join(lines)

async def wizard_handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if message was consumed by wizard."""
    state = context.user_data.get(WIZARD_KEY)
    if not state:
        return False
    text = (update.message.text or "").strip()
    if text.startswith("/"):
        return False  # let command handlers deal with it
    step_idx = state["step"]
    step = WIZARD_STEPS[step_idx]
    d = state["data"]

    if step == "asset_class":
        v = text.lower()
        if v not in ("stock", "crypto"):
            await update.message.reply_text("Только stock или crypto.")
            return True
        d["asset_class"] = v
    elif step == "ticker":
        d["ticker"] = text.upper().lstrip("$")
    elif step == "direction":
        v = text.lower()
        if v not in ("long", "short"):
            await update.message.reply_text("Только long или short.")
            return True
        d["direction"] = v
    elif step == "entry":
        v = _parse_num(text)
        if v is None or v <= 0:
            await update.message.reply_text("Введи положительное число.")
            return True
        d["entry_price"] = v
    elif step == "stop":
        v = _parse_num(text)
        if v is None or v <= 0:
            await update.message.reply_text("Введи положительное число.")
            return True
        # sanity: stop on opposite side from direction
        if d["direction"] == "long" and v >= d["entry_price"]:
            await update.message.reply_text("Для long стоп должен быть НИЖЕ цены входа. Перевводи:")
            return True
        if d["direction"] == "short" and v <= d["entry_price"]:
            await update.message.reply_text("Для short стоп должен быть ВЫШЕ цены входа. Перевводи:")
            return True
        d["stop_price"] = v
    elif step in ("target_1", "target_2", "target_3"):
        v = _parse_num(text)
        if text.strip() not in ("—", "-", "skip", "none") and (v is None or v <= 0):
            await update.message.reply_text("Введи число или '—' чтобы пропустить.")
            return True
        d[step] = v  # may be None
    elif step == "publish":
        v = text.lower()
        if v in ("yes", "y", "да", "оба", "both"):
            d["publish_to_ru"] = True; d["publish_to_en"] = True
        elif v == "ru":
            d["publish_to_ru"] = True; d["publish_to_en"] = False
        elif v == "en":
            d["publish_to_ru"] = False; d["publish_to_en"] = True
        elif v in ("no", "n", "нет", "skip"):
            d["publish_to_ru"] = False; d["publish_to_en"] = False
        else:
            await update.message.reply_text("yes / no / ru / en")
            return True
    elif step == "confirm":
        v = text.lower()
        if v in ("yes", "y", "да", "ок", "ok"):
            # Build insert body
            body = {
                "asset_class":  d["asset_class"],
                "ticker":       d["ticker"],
                "direction":    d["direction"],
                "entry_price":  d["entry_price"],
                "stop_price":   d["stop_price"],
                "target_1":     d.get("target_1"),
                "target_2":     d.get("target_2"),
                "target_3":     d.get("target_3"),
                "publish_to_ru": d.get("publish_to_ru", False),
                "publish_to_en": d.get("publish_to_en", False),
                "status":       "open",
            }
            row = await sb_insert("active_positions", body)
            context.user_data.pop(WIZARD_KEY, None)
            if not row:
                await update.message.reply_text("❌ Не удалось создать позицию.")
                return True
            await update.message.reply_text(
                f"✅ Позиция #{row['id']} создана.",
                parse_mode="HTML",
            )
            # Publish if requested
            if body["publish_to_ru"] or body["publish_to_en"]:
                res = await call_publisher("opened", row["id"], body["entry_price"])
                if res and res.get("ok"):
                    await update.message.reply_text("📣 Опубликовано в каналах.")
                else:
                    await update.message.reply_text("⚠ Публикация не прошла, см. логи.")
            return True
        else:
            context.user_data.pop(WIZARD_KEY, None)
            await update.message.reply_text("Отменено.")
            return True
    else:
        return False

    # Move to next step
    state["step"] += 1
    next_step = WIZARD_STEPS[state["step"]]
    if next_step == "confirm":
        await update.message.reply_text(await _wizard_summary(d), parse_mode="HTML")
        await update.message.reply_text(WIZARD_PROMPTS["confirm"])
    else:
        await update.message.reply_text(WIZARD_PROMPTS[next_step])
    return True

# ---------- /close --------------------------------------------------------
CLOSE_KEY = "positions_close_pending"

async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await deny(update)
    args = context.args or []
    if not args:
        await update.message.reply_text("Использование: /close TICKER")
        return
    ticker = args[0].upper().lstrip("$")
    rows = await sb_select("active_positions",
                           {"ticker": f"eq.{ticker}",
                            "status": "in.(open,partially_closed)",
                            "select": "id,ticker,direction,entry_price,stop_price,publish_to_ru,publish_to_en"})
    if not rows:
        await update.message.reply_text(f"Открытых позиций по {ticker} не найдено.")
        return
    if len(rows) > 1:
        await update.message.reply_text(f"Найдено несколько ({len(rows)}). Используй /list и закрой по кнопке.")
        return
    p = rows[0]
    context.user_data[CLOSE_KEY] = {"id": p["id"], "ticker": p["ticker"]}
    await update.message.reply_text(f"Введи цену выхода для {p['ticker']} (число):")

async def _do_close(update: Update, context: ContextTypes.DEFAULT_TYPE,
                    pos_id: int, exit_price: float):
    # Fetch position to compute R
    rows = await sb_select("active_positions",
                           {"id": f"eq.{pos_id}",
                            "select": "id,ticker,direction,entry_price,stop_price,publish_to_ru,publish_to_en"})
    if not rows:
        await update.message.reply_text("Позиция не найдена.")
        return
    p = rows[0]
    entry = float(p["entry_price"]); stop = float(p["stop_price"])
    risk = abs(entry - stop)
    if p["direction"] == "long":
        rr = (exit_price - entry) / risk if risk > 0 else 0
    else:
        rr = (entry - exit_price) / risk if risk > 0 else 0

    from datetime import datetime, timezone
    code = await sb_update("active_positions", {"id": f"eq.{pos_id}"}, {
        "status": "closed",
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "exit_price": exit_price,
        "result_rr": round(rr, 2),
    })
    if code not in (200, 204):
        await update.message.reply_text(f"❌ Ошибка обновления: {code}")
        return
    await update.message.reply_text(
        f"✅ Закрыто #{pos_id} {p['ticker']} @ {fmt_price(exit_price)} · {rr:+.2f}R",
    )
    if p.get("publish_to_ru") or p.get("publish_to_en"):
        await call_publisher("closed", pos_id, exit_price)

# ---------- /move_stop ----------------------------------------------------
async def cmd_move_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await deny(update)
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Использование: /move_stop TICKER NEW_STOP")
        return
    ticker = args[0].upper().lstrip("$")
    new_stop = _parse_num(args[1])
    if not new_stop or new_stop <= 0:
        await update.message.reply_text("Некорректный стоп.")
        return
    rows = await sb_select("active_positions",
                           {"ticker": f"eq.{ticker}",
                            "status": "in.(open,partially_closed)",
                            "select": "id,ticker,stop_price,publish_to_ru,publish_to_en"})
    if not rows:
        await update.message.reply_text(f"Открытых позиций по {ticker} не найдено.")
        return
    if len(rows) > 1:
        await update.message.reply_text("Несколько позиций — используй /list.")
        return
    p = rows[0]
    code = await sb_update("active_positions", {"id": f"eq.{p['id']}"}, {"stop_price": new_stop})
    if code in (200, 204):
        await update.message.reply_text(
            f"🛡 Стоп #{p['id']} {p['ticker']}: {fmt_price(p['stop_price'])} → {fmt_price(new_stop)}"
        )
    else:
        await update.message.reply_text(f"❌ Ошибка: {code}")

# ---------- /comment with auto-translate ----------------------------------
COMMENT_KEY = "positions_comment_pending"
COMMENT_REVIEW_KEY = "positions_comment_review"

async def cmd_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await deny(update)
    args = context.args or []
    if not args:
        await update.message.reply_text("Использование: /comment TICKER")
        return
    ticker = args[0].upper().lstrip("$")
    rows = await sb_select("active_positions",
                           {"ticker": f"eq.{ticker}",
                            "status": "in.(open,partially_closed)",
                            "select": "id,ticker,publish_to_ru,publish_to_en"})
    if not rows:
        await update.message.reply_text(f"Открытых позиций по {ticker} не найдено.")
        return
    if len(rows) > 1:
        await update.message.reply_text("Несколько позиций — используй /list.")
        return
    p = rows[0]
    context.user_data[COMMENT_KEY] = {"id": p["id"], "ticker": p["ticker"]}
    await update.message.reply_text(
        f"💬 Напиши комментарий для {p['ticker']} на русском.\n"
        f"Я переведу на английский и покажу на подтверждение перед публикацией."
    )

async def _start_comment_review(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                pos_id: int, ticker: str, ru_text: str):
    await update.message.reply_text("⏳ Перевожу...")
    en_text = await call_translate(ru_text)
    if not en_text:
        await update.message.reply_text(
            "⚠ Перевод не удался. Введи английскую версию вручную (одним сообщением)."
        )
        context.user_data[COMMENT_REVIEW_KEY] = {
            "id": pos_id, "ticker": ticker, "ru": ru_text, "en": None,
            "awaiting_manual_en": True,
        }
        return
    context.user_data[COMMENT_REVIEW_KEY] = {
        "id": pos_id, "ticker": ticker, "ru": ru_text, "en": en_text,
    }
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Опубликовать", callback_data="positions:comment_send")],
        [InlineKeyboardButton("✏️ Изменить EN",  callback_data="positions:comment_edit_en")],
        [InlineKeyboardButton("❌ Отмена",       callback_data="positions:comment_cancel")],
    ])
    await update.message.reply_text(
        f"<b>{ticker} — превью комментария</b>\n\n"
        f"<b>RU:</b>\n{ru_text}\n\n"
        f"<b>EN:</b>\n{en_text}",
        parse_mode="HTML", reply_markup=kb,
    )

async def _save_and_publish_comment(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                     review: dict):
    pos_id = review["id"]
    code = await sb_update("active_positions", {"id": f"eq.{pos_id}"},
                           {"comment_ru": review["ru"], "comment_en": review["en"]})
    if code not in (200, 204):
        await (update.callback_query.message if update.callback_query else update.message).reply_text(
            f"❌ Ошибка сохранения: {code}"
        )
        return
    res = await call_publisher("comment", pos_id)
    target = (update.callback_query.message if update.callback_query else update.message)
    if res and res.get("ok"):
        await target.reply_text(f"📣 Опубликовано: {review['ticker']}")
    else:
        await target.reply_text("✅ Сохранено в БД, но публикация не прошла. Проверь логи.")
    context.user_data.pop(COMMENT_REVIEW_KEY, None)

# ---------- Text router (called from main bot's on_text_message) ---------
async def maybe_handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if message was consumed by a positions-module flow.
    The main bot should call this BEFORE its own awaiting_email_for_payment logic.
    """
    if not is_admin(update.effective_user.id):
        return False
    text = (update.message.text or "").strip()

    # 1. /new wizard
    if WIZARD_KEY in context.user_data:
        return await wizard_handle_text(update, context)

    # 2. /close awaiting price
    pending = context.user_data.get(CLOSE_KEY)
    if pending:
        v = _parse_num(text)
        if v is None or v <= 0:
            await update.message.reply_text("Введи положительное число (цена выхода).")
            return True
        context.user_data.pop(CLOSE_KEY, None)
        await _do_close(update, context, pending["id"], v)
        return True

    # 3. /comment awaiting RU text
    pending = context.user_data.get(COMMENT_KEY)
    if pending:
        context.user_data.pop(COMMENT_KEY, None)
        await _start_comment_review(update, context, pending["id"], pending["ticker"], text)
        return True

    # 4. Manual EN edit during comment review
    review = context.user_data.get(COMMENT_REVIEW_KEY)
    if review and (review.get("awaiting_manual_en") or review.get("awaiting_edit_en")):
        review["en"] = text
        review.pop("awaiting_manual_en", None)
        review.pop("awaiting_edit_en", None)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Опубликовать", callback_data="positions:comment_send")],
            [InlineKeyboardButton("✏️ Ещё раз EN",   callback_data="positions:comment_edit_en")],
            [InlineKeyboardButton("❌ Отмена",       callback_data="positions:comment_cancel")],
        ])
        await update.message.reply_text(
            f"<b>{review['ticker']} — обновлённое превью</b>\n\n"
            f"<b>RU:</b>\n{review['ru']}\n\n"
            f"<b>EN:</b>\n{review['en']}",
            parse_mode="HTML", reply_markup=kb,
        )
        return True

    return False

# ---------- Inline button router -----------------------------------------
async def maybe_handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if callback was handled by positions module."""
    query = update.callback_query
    if not query or not (query.data or "").startswith("positions:"):
        return False
    if not is_admin(query.from_user.id):
        await query.answer("Только для администратора.", show_alert=True)
        return True
    await query.answer()
    data = query.data[len("positions:"):]

    if "|" in data:
        action, _, arg = data.partition("|")
        try:
            pos_id = int(arg)
        except Exception:
            return True
        if action == "close":
            context.user_data[CLOSE_KEY] = {"id": pos_id, "ticker": "?"}
            # Look up ticker for nicer prompt
            rows = await sb_select("active_positions",
                                   {"id": f"eq.{pos_id}", "select": "ticker"})
            if rows:
                context.user_data[CLOSE_KEY]["ticker"] = rows[0]["ticker"]
            await query.message.reply_text(
                f"Введи цену выхода для #{pos_id} {context.user_data[CLOSE_KEY]['ticker']} (число):"
            )
            return True
        if action == "movestop":
            rows = await sb_select("active_positions",
                                   {"id": f"eq.{pos_id}", "select": "ticker,stop_price"})
            t = rows[0]["ticker"] if rows else f"#{pos_id}"
            await query.message.reply_text(
                f"Чтобы сдвинуть стоп для {t}, отправь команду:\n"
                f"<code>/move_stop {t} НОВЫЙ_СТОП</code>",
                parse_mode="HTML",
            )
            return True
        if action == "comment":
            rows = await sb_select("active_positions",
                                   {"id": f"eq.{pos_id}", "select": "ticker"})
            t = rows[0]["ticker"] if rows else "?"
            context.user_data[COMMENT_KEY] = {"id": pos_id, "ticker": t}
            await query.message.reply_text(
                f"💬 Напиши комментарий для {t} на русском (я переведу на английский)."
            )
            return True

    # Comment review buttons
    review = context.user_data.get(COMMENT_REVIEW_KEY)
    if data == "comment_send":
        if not review:
            await query.message.reply_text("Нет активного черновика.")
            return True
        await _save_and_publish_comment(update, context, review)
        return True
    if data == "comment_edit_en":
        if not review:
            await query.message.reply_text("Нет активного черновика.")
            return True
        review["awaiting_edit_en"] = True
        await query.message.reply_text(
            "Введи английский текст одним сообщением (заменит текущий перевод):"
        )
        return True
    if data == "comment_cancel":
        context.user_data.pop(COMMENT_REVIEW_KEY, None)
        await query.message.reply_text("Черновик отменён.")
        return True

    return True  # consumed (unknown positions:* — silent)

# ---------- /cancel_op (cancel any pending positions wizard/state) -------
async def cmd_cancel_op(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await deny(update)
    cleared = []
    for k in (WIZARD_KEY, CLOSE_KEY, COMMENT_KEY, COMMENT_REVIEW_KEY):
        if context.user_data.pop(k, None) is not None:
            cleared.append(k)
    if cleared:
        await update.message.reply_text(f"Отменено: {len(cleared)} активных режима.")
    else:
        await update.message.reply_text("Нет активных режимов.")

# ---------- Registration --------------------------------------------------
def register(app: Application) -> None:
    """Wire all positions handlers into a python-telegram-bot Application.

    The main bot should ALSO call `await maybe_handle_text(update, context)`
    at the START of its own on_text_message handler, and return early if it
    returned True.
    """
    app.add_handler(CommandHandler("new",        cmd_new))
    app.add_handler(CommandHandler("list",       cmd_list))
    app.add_handler(CommandHandler("close",      cmd_close))
    app.add_handler(CommandHandler("move_stop",  cmd_move_stop))
    app.add_handler(CommandHandler("comment",    cmd_comment))
    app.add_handler(CommandHandler("cancel_op",  cmd_cancel_op))
    # Callback queries — handled with group=-1 so they run before main on_button
    # but we instead recommend invoking `maybe_handle_callback` from on_button.
