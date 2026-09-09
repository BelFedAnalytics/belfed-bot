// digest-weekly-broadcast v1 - automatic weekly service digest for members.
//
// Scheduled hourly by pg_cron; the function itself decides whether the current
// hour is the delivery slot for a language (Sunday 12:00 Europe/Moscow for ru,
// Sunday 18:00 Europe/London for en), so DST shifts never move the send time.
//
// AUTH: header `x-bot-secret` == BOT_SHARED_SECRET, or
//       `Authorization: Bearer <service role key>`, or header `x-cron-secret`
//       matching the Vault secret `belfed_cron_secret` (used by pg_cron, so the
//       schedule carries no plaintext secret in cron.job.command).
// BODY: { lang?: "ru"|"en", force?: boolean, dry_run?: boolean,
//         limit?: number, telegram_ids?: (string|number)[] }
//   force        - ignore the schedule window (manual run / test)
//   dry_run      - build everything, send nothing, mark nothing
//   telegram_ids - explicit recipients instead of the RPC selection (test run)
//
// Gating and content live in the DB: weekly_digest_recipients() filters by
// subscription_status (active | trial | admin), the opt-out flag and the
// idempotency mark; build_digest_for_telegram() re-checks access per recipient
// at send time. Delivery is recorded with weekly_digest_mark_sent() so a repeat
// invocation inside the same local week sends nothing twice.

import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.45.0";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const BOT_TOKEN = Deno.env.get("TELEGRAM_BOT_TOKEN")!;
const BOT_SHARED_SECRET = Deno.env.get("BOT_SHARED_SECRET") ?? "";

const admin = createClient(SUPABASE_URL, SERVICE_ROLE, {
  auth: { autoRefreshToken: false, persistSession: false },
});

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, x-bot-secret",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const json = (b: unknown, s = 200) =>
  new Response(JSON.stringify(b), {
    status: s,
    headers: { ...cors, "Content-Type": "application/json" },
  });

type Lang = "ru" | "en";

// Delivery slots are per language and expressed in the member's own local time:
// the weekly recap closes the week rather than opening the next one.
const LANGS: { lang: Lang; tz: string; weekday: string; hour: number }[] = [
  { lang: "ru", tz: "Europe/Moscow", weekday: "Sun", hour: 12 },
  { lang: "en", tz: "Europe/London", weekday: "Sun", hour: 18 },
];

const COPY = {
  ru: {
    pick_day: "Сегодня",
    pick_yesterday: "Вчера",
    opt_out: "Не присылать недельную сводку",
  },
  en: {
    pick_day: "Today",
    pick_yesterday: "Yesterday",
    opt_out: "Stop the weekly recap",
  },
} as const;

function isSlotNow(tz: string, slotWeekday: string, slotHour: number): boolean {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: tz,
    weekday: "short",
    hour: "numeric",
    hour12: false,
  }).formatToParts(new Date());
  const weekday = parts.find((p) => p.type === "weekday")?.value ?? "";
  const hour = Number(parts.find((p) => p.type === "hour")?.value ?? "-1");
  return weekday === slotWeekday && hour === slotHour;
}

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

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function recipientsFor(lang: Lang, limit: number): Promise<string[]> {
  const { data, error } = await admin.rpc("weekly_digest_recipients", {
    p_lang: lang,
    p_limit: limit,
  });
  if (error) throw new Error(`recipients_failed: ${error.message}`);
  const rows = (data ?? []) as { telegram_id: string }[];
  return rows.map((r) => String(r.telegram_id)).filter((v) => /^\d+$/.test(v));
}

async function deliver(
  lang: Lang,
  telegramIds: string[],
  dryRun: boolean,
): Promise<Record<string, unknown>> {
  const result = {
    lang,
    candidates: telegramIds.length,
    sent: 0,
    skipped_empty: 0,
    skipped_no_access: 0,
    failed: 0,
    errors: [] as string[],
  };

  for (const telegramId of telegramIds) {
    const { data, error } = await admin.rpc("build_digest_for_telegram", {
      p_telegram_id: telegramId,
      p_kind: "week",
    });
    if (error) {
      result.failed += 1;
      if (result.errors.length < 10) result.errors.push(`${telegramId}: db ${error.message}`);
      continue;
    }

    const res = (data ?? {}) as Record<string, unknown>;
    // Access is re-checked per recipient at send time: a subscription that
    // lapsed after the recipient list was built must not receive anything.
    if (String(res.status ?? "") !== "ok") {
      result.skipped_no_access += 1;
      continue;
    }
    // A silent week is not worth a push notification.
    if (res.is_empty === true || typeof res.text !== "string" || !res.text) {
      result.skipped_empty += 1;
      continue;
    }

    // Button labels follow the recipient's own language, not the batch label.
    const copy = COPY[res.lang === "en" ? "en" : "ru"];
    const markup = (res.reply_markup ?? {}) as { inline_keyboard?: unknown[] };
    const rows = Array.isArray(markup.inline_keyboard) ? markup.inline_keyboard : [];
    const replyMarkup = {
      ...markup,
      inline_keyboard: [
        ...rows,
        [
          { text: copy.pick_day, callback_data: "digest:day" },
          { text: copy.pick_yesterday, callback_data: "digest:yesterday" },
        ],
        [{ text: copy.opt_out, callback_data: "digest:weekly_off" }],
      ],
    };

    if (dryRun) {
      result.sent += 1;
      continue;
    }

    const sent = await sendTg(telegramId, res.text as string, replyMarkup);
    if (sent.ok) {
      result.sent += 1;
      const { error: markError } = await admin.rpc("weekly_digest_mark_sent", {
        p_telegram_id: telegramId,
      });
      if (markError && result.errors.length < 10) {
        result.errors.push(`${telegramId}: mark ${markError.message}`);
      }
    } else {
      result.failed += 1;
      if (result.errors.length < 10) result.errors.push(`${telegramId}: tg ${sent.error}`);
      // Blocked or deleted chats are permanent: record the attempt so the
      // broadcast does not retry the same dead chat every week.
      const permanent = /blocked|deactivated|chat not found|user is deactivated/i
        .test(sent.error ?? "");
      if (permanent) {
        await admin.rpc("weekly_digest_mark_sent", { p_telegram_id: telegramId });
      }
    }
    await sleep(120);
  }

  return result;
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "POST") return json({ ok: false, error: "method_not_allowed" }, 405);

  const secret = req.headers.get("x-bot-secret") ?? "";
  const bearer = (req.headers.get("authorization") ?? "").replace(/^Bearer\s+/i, "");
  let authorized =
    (BOT_SHARED_SECRET !== "" && secret === BOT_SHARED_SECRET) ||
    (SERVICE_ROLE !== "" && bearer === SERVICE_ROLE);

  if (!authorized) {
    const cronSecret = req.headers.get("x-cron-secret") ?? "";
    if (cronSecret !== "") {
      const { data } = await admin.rpc("weekly_digest_check_cron_secret", {
        p_secret: cronSecret,
      });
      authorized = data === true;
    }
  }
  if (!authorized) return json({ ok: false, error: "unauthorized" }, 401);

  let body: Record<string, unknown> = {};
  if (req.headers.get("content-length") !== "0") {
    try {
      body = await req.json();
    } catch {
      body = {};
    }
  }

  const force = body.force === true;
  const dryRun = body.dry_run === true;
  const limit = Math.min(Math.max(Number(body.limit ?? 500) || 500, 1), 2000);
  const askedLang = body.lang === "en" || body.lang === "ru" ? (body.lang as Lang) : null;
  const explicitIds = Array.isArray(body.telegram_ids)
    ? body.telegram_ids.map((v) => String(v).trim()).filter((v) => /^\d+$/.test(v))
    : null;

  // An explicit recipient list is one batch: languages are resolved per member
  // inside deliver(), so iterating LANGS here would send the same digest twice.
  const targets = explicitIds
    ? [{ lang: askedLang ?? ("ru" as Lang) }]
    : LANGS
      .filter((l) => (askedLang ? l.lang === askedLang : true))
      .filter((l) => (force ? true : isSlotNow(l.tz, l.weekday, l.hour)));

  if (targets.length === 0) {
    return json({ ok: true, skipped: "outside_slot", dry_run: dryRun });
  }

  const results: Record<string, unknown>[] = [];
  for (const t of targets) {
    try {
      const ids = explicitIds ?? (await recipientsFor(t.lang, limit));
      results.push(await deliver(t.lang, ids, dryRun));
    } catch (e) {
      results.push({ lang: t.lang, error: String(e) });
    }
  }

  return json({ ok: true, dry_run: dryRun, force, results });
});
