// deno-lint-ignore-file no-explicit-any
// Supabase Edge Function: telegram-founding-renewal-reminder v8
//
// Daily cron that sends a Founding renewal reminder 2-5 days before expiry.
// Active, uncancelled Tribute subscriptions are provider-managed auto-renewals.
//
// CRON: 30 8 * * * POST Authorization: Bearer <SERVICE_ROLE_KEY>

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const TG_TOKEN = Deno.env.get("TELEGRAM_BOT_TOKEN")!;

const admin = createClient(SUPABASE_URL, SERVICE_ROLE_KEY, {
  auth: { persistSession: false },
});

async function tg(method: string, body: Record<string, any>) {
  const r = await fetch(`https://api.telegram.org/bot${TG_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return await r.json().catch(() => ({ ok: false }));
}

function fmtDateRU(iso: string): string {
  return new Date(iso).toLocaleDateString("ru-RU", {
    day: "2-digit",
    month: "long",
  });
}

function fmtDateEN(iso: string): string {
  return new Date(iso).toLocaleDateString("en-US", {
    day: "numeric",
    month: "long",
  });
}

function buildTextRU(periodEnd: string, willAutoRenew: boolean): string {
  const dateStr = fmtDateRU(periodEnd);
  if (willAutoRenew) {
    return (
      "🪙 BelFed Founding — напоминание о продлении\n\n" +
      `Ваш текущий период подписки заканчивается ${dateStr}. ` +
      "Подписка продлится автоматически в эту дату.\n\n" +
      "Founding-цена 1050 ₽/мес зафиксирована за Вами навсегда. " +
      "Ничего делать не нужно — это просто напоминание."
    );
  }
  return (
    "🪙 BelFed Founding — пора продлить подписку\n\n" +
    `Ваш текущий период подписки заканчивается ${dateStr}. ` +
    "Чтобы продолжить пользоваться сервисом и сохранить founding-цену 1050 ₽/мес навсегда:\n\n" +
    "👉 Нажмите /start founding в этом боте\n\n" +
    "Один платёж 1050 ₽ — и далее списания будут автоматическими каждый месяц по той же цене. " +
    "Если есть вопросы — напишите прямо сюда.\n\n" +
    "— BelFed Support"
  );
}

function buildTextEN(periodEnd: string, willAutoRenew: boolean): string {
  const dateStr = fmtDateEN(periodEnd);
  if (willAutoRenew) {
    return (
      "🪙 BelFed Founding — renewal reminder\n\n" +
      `Your current subscription period ends on ${dateStr}. ` +
      "Your subscription will renew automatically on that date.\n\n" +
      "Your founding price of $10.50/month is locked in forever. " +
      "Nothing to do — this is just a heads-up."
    );
  }
  return (
    "🪙 BelFed Founding — time to renew\n\n" +
    `Your current subscription period ends on ${dateStr}. ` +
    "To keep your access and lock in the founding price of $10.50/month forever:\n\n" +
    "👉 Tap /start founding in this bot\n\n" +
    "One payment of 669⭐ — and from then on Telegram will renew the subscription " +
    "automatically each month at the same price. Questions? Just reply here.\n\n" +
    "— BelFed Support"
  );
}

Deno.serve(async (req) => {
  if (req.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405 });
  }

  const { data: list, error } = await admin
    .from("users_for_founding_renewal_reminder")
    .select("*")
    .limit(500);
  if (error) return new Response(error.message, { status: 500 });

  console.log(`[founding-renewal-reminder] candidates=${list?.length ?? 0}`);

  const results: any[] = [];
  for (const row of list ?? []) {
    try {
      const yookassaAuto = row.provider === "yookassa" &&
        !!row.payment_method_id;
      const starsAuto = row.provider === "telegram_stars" &&
        !!row.provider_subscription_id;
      const tributeAuto = row.provider === "tribute" &&
        !row.cancel_at_period_end;
      const willAutoRenew = yookassaAuto || starsAuto || tributeAuto;

      const text = row.locale === "en"
        ? buildTextEN(row.current_period_end, willAutoRenew)
        : buildTextRU(row.current_period_end, willAutoRenew);

      const r: any = await tg("sendMessage", {
        chat_id: Number(row.telegram_id),
        text,
        disable_web_page_preview: true,
      });

      if (r?.ok) {
        await admin
          .from("profiles")
          .update({
            founding_renewal_reminder_sent_at: new Date().toISOString(),
          })
          .eq("id", row.user_id);
      }
      results.push({
        user: row.user_id,
        locale: row.locale,
        will_auto_renew: willAutoRenew,
        ok: !!r?.ok,
      });
    } catch (e) {
      results.push({
        user: row.user_id,
        ok: false,
        error: (e as Error).message,
      });
    }
  }

  return new Response(JSON.stringify({
    version: "v8",
    processed: results.length,
    results,
  }), {
    headers: { "Content-Type": "application/json" },
  });
});
