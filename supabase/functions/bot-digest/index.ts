// bot-digest v2 - pull-only service digest for the Telegram bot /digest command.
//
// AUTH: shared secret in header `x-bot-secret` must match BOT_SHARED_SECRET env.
// BODY: { telegram_id: string|number, kind?: "day"|"yesterday"|"week",
//         send?: boolean, with_picker?: boolean }
// with_picker appends a row of period buttons (callback_data digest:<kind>) under
// the message, so the member can switch the period without retyping the command.
// Resolves the profile by telegram_id, gates by subscription_status
// (active | trial | admin), builds the message in the DB via RPC
// build_digest_for_telegram, and sends it to the same chat unless send=false.

import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.45.0";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const BOT_TOKEN = Deno.env.get("TELEGRAM_BOT_TOKEN")!;
const BOT_SHARED_SECRET = Deno.env.get("BOT_SHARED_SECRET")!;

const admin = createClient(SUPABASE_URL, SERVICE_ROLE, {
  auth: { autoRefreshToken: false, persistSession: false },
});

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type, x-bot-secret",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const json = (b: unknown, s = 200) =>
  new Response(JSON.stringify(b), { status: s, headers: { ...cors, "Content-Type": "application/json" } });

type Lang = "ru" | "en";

const COPY = {
  ru: {
    not_linked:
      "Чтобы получать сводку по сервису, нужен доступ. Нажмите /start и оформите пробный период.",
    no_access:
      "Сводка доступна участникам с активным доступом. Ваш доступ сейчас неактивен, продлить можно в меню бота.",
    empty_day: "Сегодня на сервисе пока без событий. Загляните позже.",
    empty_yesterday: "Вчера на сервисе событий не было.",
    empty_week: "За последние семь дней событий на сервисе не было.",
    pick_day: "Сегодня",
    pick_yesterday: "Вчера",
    pick_week: "За неделю",
  },
  en: {
    not_linked:
      "The service summary needs an active access. Tap /start to begin your trial.",
    no_access:
      "The summary is available to members with active access. Yours is inactive right now, you can renew it from the bot menu.",
    empty_day: "Nothing has happened on the service today yet. Check back later.",
    empty_yesterday: "There were no service events yesterday.",
    empty_week: "There were no service events over the past seven days.",
    pick_day: "Today",
    pick_yesterday: "Yesterday",
    pick_week: "Past week",
  },
} as const;

async function sendTg(
  chatId: string,
  text: string,
  replyMarkup: unknown | null,
): Promise<{ ok: boolean; message_id?: number; error?: string }> {
  const payload: Record<string, unknown> = {
    chat_id: chatId,
    text,
    parse_mode: "HTML",
    link_preview_options: { is_disabled: true },
  };
  if (replyMarkup) payload.reply_markup = replyMarkup;

  const r = await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const j = await r.json().catch(() => ({ ok: false }));
  if (!j.ok) return { ok: false, error: j.description ?? "tg_error" };
  return { ok: true, message_id: j.result?.message_id };
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "POST") return json({ ok: false, error: "method_not_allowed" }, 405);

  const incomingSecret = req.headers.get("x-bot-secret") ?? "";
  if (!BOT_SHARED_SECRET || incomingSecret !== BOT_SHARED_SECRET) {
    return json({ ok: false, error: "unauthorized" }, 401);
  }

  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return json({ ok: false, error: "invalid_json" }, 400);
  }

  const telegramId = body.telegram_id == null ? "" : String(body.telegram_id).trim();
  if (!/^\d+$/.test(telegramId)) return json({ ok: false, error: "telegram_id_required" }, 400);

  const rawKind = String(body.kind ?? "day").toLowerCase();
  const kind = rawKind === "week" || rawKind === "yesterday" ? rawKind : "day";
  const shouldSend = body.send !== false;
  const withPicker = body.with_picker === true;

  const { data, error } = await admin.rpc("build_digest_for_telegram", {
    p_telegram_id: telegramId,
    p_kind: kind,
  });
  if (error) return json({ ok: false, error: "db_error", detail: error.message }, 500);

  const res = (data ?? {}) as Record<string, unknown>;
  const status = String(res.status ?? "");
  const lang: Lang = res.lang === "en" ? "en" : "ru";
  const copy = COPY[lang];

  let text: string | null = null;
  let replyMarkup: unknown | null = null;

  if (status === "not_linked") {
    text = copy.not_linked;
  } else if (status === "no_access") {
    text = copy.no_access;
  } else if (status === "ok" && res.is_empty === true) {
    text = kind === "week"
      ? copy.empty_week
      : kind === "yesterday"
        ? copy.empty_yesterday
        : copy.empty_day;
  } else if (status === "ok") {
    text = typeof res.text === "string" ? res.text : null;
    replyMarkup = res.reply_markup ?? null;
  }

  if (!text) return json({ ok: false, error: "no_content", status, kind, lang }, 500);

  // Period picker: one extra row, current period omitted so the member is not
  // offered the digest they are already reading.
  if (withPicker && status === "ok") {
    const all = [
      { kind: "day", label: copy.pick_day },
      { kind: "yesterday", label: copy.pick_yesterday },
      { kind: "week", label: copy.pick_week },
    ];
    const row = all
      .filter((p) => p.kind !== kind)
      .map((p) => ({ text: p.label, callback_data: `digest:${p.kind}` }));
    const markup = (replyMarkup ?? {}) as { inline_keyboard?: unknown[] };
    const rows = Array.isArray(markup.inline_keyboard) ? markup.inline_keyboard : [];
    replyMarkup = { ...markup, inline_keyboard: [...rows, row] };
  }

  if (!shouldSend) {
    return json({ ok: true, sent: false, status, kind, lang, text, reply_markup: replyMarkup });
  }

  const sent = await sendTg(telegramId, text, replyMarkup);
  return json({
    ok: sent.ok,
    sent: sent.ok,
    status,
    kind,
    lang,
    message_id: sent.message_id ?? null,
    error: sent.error ?? null,
  });
});
