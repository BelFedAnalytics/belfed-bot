// deno-lint-ignore-file no-explicit-any
// Supabase Edge Function: telegram-founding-renewal-reminder v9
//
// Daily cron that sends a Founding renewal reminder 2-5 days before expiry.
// Active, uncancelled Tribute subscriptions are provider-managed auto-renewals.
//
// v9: Tribute sells 1, 3, 6 and 12-month periods. The auto-renewal notice now
// states the actual upcoming charge and period (from the latest succeeded
// payment) instead of a fixed monthly price; the manual-renewal notice carries
// the period-choice note. Long dashes removed from user-facing copy.
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

// Tribute sells 1, 3, 6 and 12-month periods and preselects the longest one
// on its checkout page.
const PERIOD_NOTE_RU =
  "Обратите внимание: период подписки (1, 3, 6 или 12 месяцев) выбирается на странице оплаты Tribute. " +
  "По умолчанию там отмечен вариант на 12 месяцев, поэтому перед оплатой выберите нужный вам срок.";
const PERIOD_NOTE_EN =
  "Please note: you choose the subscription period (1, 3, 6 or 12 months) on the Tribute checkout page. " +
  "The 12-month option is preselected there, so pick the period you want before paying.";

interface LastPayment {
  amount: number | string | null;
  currency: string | null;
  period_months: number | null;
}

// The paid period lives on the payment row, not on the subscription, so the
// upcoming auto-renewal charge is described from the latest succeeded payment.
async function getLastPayment(userId: string): Promise<LastPayment | null> {
  try {
    const { data, error } = await admin
      .from("payments")
      .select("amount, currency, period_months")
      .eq("user_id", userId)
      .eq("status", "succeeded")
      .order("created_at", { ascending: false })
      .limit(1);
    if (error) return null;
    return (data?.[0] as LastPayment) ?? null;
  } catch (_e) {
    return null;
  }
}

function fmtMoney(amount: number | string | null, currency: string | null): string {
  const n = Number(amount);
  if (amount == null || !isFinite(n)) return "";
  const cur = (currency ?? "RUB").toUpperCase();
  const whole = Number.isInteger(n);
  if (cur === "RUB") {
    const digits = whole ? String(n) : n.toFixed(2);
    return digits.replace(/\B(?=(\d{3})+(?!\d))/g, "\u00a0") + "\u00a0₽";
  }
  if (cur === "USD") return "$" + (whole ? String(n) : n.toFixed(2));
  return (whole ? String(n) : n.toFixed(2)) + " " + cur;
}

function periodLabelRU(months: number): string {
  if (months === 1) return "1 месяц";
  if (months >= 2 && months <= 4) return `${months} месяца`;
  return `${months} месяцев`;
}

function periodLabelEN(months: number): string {
  return months === 1 ? "1 month" : `${months} months`;
}

function chargeLineRU(pay: LastPayment | null): string {
  const price = pay ? fmtMoney(pay.amount, pay.currency) : "";
  if (!price) return "Сумма и период списания те же, что при прошлой оплате.";
  const months = Number(pay?.period_months) || 1;
  return `Сумма списания: ${price} за ${periodLabelRU(months)}, как и при прошлой оплате.`;
}

function chargeLineEN(pay: LastPayment | null): string {
  const price = pay ? fmtMoney(pay.amount, pay.currency) : "";
  if (!price) return "The amount and period are the same as for your previous payment.";
  const months = Number(pay?.period_months) || 1;
  return `Amount to be charged: ${price} for ${periodLabelEN(months)}, the same as your previous payment.`;
}

function buildTextRU(periodEnd: string, willAutoRenew: boolean, pay: LastPayment | null): string {
  const dateStr = fmtDateRU(periodEnd);
  if (willAutoRenew) {
    return (
      "🪙 BelFed Founding: напоминание о продлении\n\n" +
      `Ваш текущий период подписки заканчивается ${dateStr}. ` +
      "Подписка продлится автоматически в эту дату.\n\n" +
      chargeLineRU(pay) + " Founding-цена закреплена за вами.\n\n" +
      "Ничего делать не нужно, это просто напоминание. " +
      "Отключить автопродление можно в боте @tribute, в разделе My subscriptions."
    );
  }
  return (
    "🪙 BelFed Founding: пора продлить подписку\n\n" +
    `Ваш текущий период подписки заканчивается ${dateStr}. ` +
    "Чтобы продолжить пользоваться сервисом и сохранить founding-цену 1 050 ₽ / мес:\n\n" +
    "👉 Нажмите /start founding в этом боте\n\n" +
    PERIOD_NOTE_RU + "\n\n" +
    "После оплаты подписка будет продлеваться автоматически на выбранный срок по той же цене. " +
    "Если есть вопросы, напишите прямо сюда.\n\n" +
    "BelFed Support"
  );
}

function buildTextEN(periodEnd: string, willAutoRenew: boolean, pay: LastPayment | null): string {
  const dateStr = fmtDateEN(periodEnd);
  if (willAutoRenew) {
    return (
      "🪙 BelFed Founding: renewal reminder\n\n" +
      `Your current subscription period ends on ${dateStr}. ` +
      "Your subscription will renew automatically on that date.\n\n" +
      chargeLineEN(pay) + " Your founding price is locked in.\n\n" +
      "Nothing to do, this is just a heads-up. " +
      "You can turn off auto-renewal in the @tribute bot under My subscriptions."
    );
  }
  return (
    "🪙 BelFed Founding: time to renew\n\n" +
    `Your current subscription period ends on ${dateStr}. ` +
    "To keep your access and the founding price of $10.50 / month:\n\n" +
    "👉 Tap /start founding in this bot\n\n" +
    PERIOD_NOTE_EN + "\n\n" +
    "After payment the subscription renews automatically for the period you chose at the same price. " +
    "Questions? Just reply here.\n\n" +
    "BelFed Support"
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

      const pay = willAutoRenew ? await getLastPayment(row.user_id) : null;
      const text = row.locale === "en"
        ? buildTextEN(row.current_period_end, willAutoRenew, pay)
        : buildTextRU(row.current_period_end, willAutoRenew, pay);

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
        period_months: pay?.period_months ?? null,
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
    version: "v9",
    processed: results.length,
    results,
  }), {
    headers: { "Content-Type": "application/json" },
  });
});
