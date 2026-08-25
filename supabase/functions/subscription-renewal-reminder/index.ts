// deno-lint-ignore-file no-explicit-any
//
// subscription-renewal-reminder v13 (2026-08-25)
// ==============================================
// Telegram DM + email reminders about an upcoming / just-passed PAID
// subscription expiry, nudging the member to renew via Tribute.
//
// v13 changes:
//   - Active Tribute subscriptions with cancel_at_period_end=false are treated
//     as provider-managed auto-renewals. They no longer receive a misleading
//     manual-renew CTA that Tribute rejects while the subscription is active.
//   - Once the local subscription expires, the normal recovery reminders
//     remain available.
//
// v12 changes:
//   - EMAIL CHANNEL ADDED (Brevo). Email is sent for pre_3d, on_expiry and
//     post_3d only; pre_1d stays Telegram-only on purpose (avoids over-mailing).
//   - Renewal email is treated as TRANSACTIONAL: recipients auto-unsubscribed by
//     the system at expiry (email_subscribers.notes = 'auto_expired') still get
//     it. A manual unsubscribe is always respected.
//   - Per-channel idempotency: renewal_reminders_sent gains email_message_id /
//     email_sent_at / email_error, so a failed email can be retried without
//     re-sending the Telegram DM (and vice versa).
//   - Every email is logged into email_sends (segment 'renewal').
//   - WORDING: "сигналы" replaced with "оповещения о наших торговых сделках",
//     long dashes removed from all user-facing copy.
//   - New body params: { test_email, test_kind, test_lang, test_founding } to
//     send a single preview email without touching production state.
//
// v10/v11 behaviour kept:
//   - Send ALWAYS, except when renewal is automatic:
//       * yookassa + payment_method_id -> auto charge, skip
//       * telegram_stars + provider_subscription_id -> Telegram renews, skip
//       * tribute + !cancel_at_period_end -> Tribute renews, skip while active
//   - Cadence pre_3d, pre_1d, on_expiry, post_3d; window [now-4d, now+4d].
//   - Trials, admin and test profiles excluded.
//   - Idempotency unique (user_id, reminder_kind, expires_at).
//
// Auth: x-bot-secret header (BOT_SHARED_SECRET env var).
//
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL       = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE_KEY   = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const TG_TOKEN           = Deno.env.get("TELEGRAM_BOT_TOKEN") ?? "";
const BOT_SHARED_SECRET  = Deno.env.get("BOT_SHARED_SECRET") ?? "";
const BREVO_API_KEY      = Deno.env.get("BREVO_API_KEY") ?? "";

const TRIBUTE_RU_URL = Deno.env.get("TRIBUTE_RU_URL") ?? "https://t.me/tribute/app?startapp=sXHG";
const TRIBUTE_EN_URL = Deno.env.get("TRIBUTE_EN_URL") ?? "https://t.me/tribute/app?startapp=sXIq";

const FROM_EMAIL = "noreply@belfed.com";
const FROM_NAME  = "BelFed Analytics";
const REPLY_TO   = "contact@belfed.com";
const SITE_RU    = "https://belfed.ru";
const SITE_EN    = "https://belfed.com";

const PRE_1D_EXCLUDE_TELEGRAM_IDS = new Set<string>(["8131674736"]); // Dawid

const sb = createClient(SUPABASE_URL, SERVICE_ROLE_KEY, { auth: { persistSession: false } });

async function tg(method: string, body: Record<string, any>) {
  if (!TG_TOKEN) return { ok: false, description: "no_bot_token" };
  try {
    const r = await fetch(`https://api.telegram.org/bot${TG_TOKEN}/${method}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    return await r.json().catch(() => ({ ok: false, description: "parse_error" }));
  } catch (e) {
    return { ok: false, description: (e as Error).message };
  }
}

function fmtDate(d: Date, lang: string): string {
  if (lang === "ru") {
    return d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" });
  }
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

type ReminderKind = "pre_3d" | "pre_1d" | "on_expiry" | "post_3d";

const EMAIL_KINDS = new Set<ReminderKind>(["pre_3d", "on_expiry", "post_3d"]);

// ─── Telegram copy ─────────────────────────────────────────────────────────

function buildMessage(opts: { lang: string; founding: boolean; kind: ReminderKind; expiresAt: Date; }) {
  const { lang, founding, kind, expiresAt } = opts;
  const dateStr = fmtDate(expiresAt, lang);
  const amountRu = founding ? "1050₽" : "1500₽";
  const amountEn = founding ? "$10.50" : "$15";
  const tributeUrl = lang === "ru" ? TRIBUTE_RU_URL : TRIBUTE_EN_URL;

  // ---- post_3d: win-back, day-neutral wording -----------------------------
  if (kind === "post_3d") {
    if (lang === "ru") {
      const bullets =
        "• получать оповещения о наших торговых сделках в реальном времени\n" +
        "• следить за перспективными идеями и главными трендами рынков\n" +
        "• читать аналитику ведущих инвестдомов\n" +
        "• запрашивать разбор любого актива";
      let cta = "Оплата " + amountRu + "/мес через Tribute, меньше минуты.";
      if (founding) cta += "\nСкидка 30% сохраняется навсегда.";
      const text =
        "Ваша подписка BelFed Analytics завершилась\n\n" +
        "Продлите доступ, чтобы снова:\n" + bullets + "\n\n" + cta;
      const btn = founding ? "✨ Продлить за " + amountRu : "💳 Продлить подписку";
      return { text, button: btn, url: tributeUrl };
    }
    const bullets =
      "• get real-time alerts on our trades\n" +
      "• follow promising ideas and the key market trends\n" +
      "• read analytics from leading investment houses\n" +
      "• request a breakdown of any asset";
    let cta = "Payment " + amountEn + "/month via Tribute, under a minute.";
    if (founding) cta += "\n30% discount stays with you forever.";
    const text =
      "Your BelFed Analytics subscription has ended\n\n" +
      "Renew your access to once again:\n" + bullets + "\n\n" + cta;
    const btn = founding ? "✨ Renew for " + amountEn : "💳 Renew subscription";
    return { text, button: btn, url: tributeUrl };
  }

  // ---- on_expiry: last day ------------------------------------------------
  if (kind === "on_expiry") {
    if (lang === "ru") {
      const header = "⚠️ Сегодня последний день подписки BelFed (" + dateStr + ").";
      let cta =
        "После окончания доступ к закрытому каналу с оповещениями о наших торговых сделках и к аналитике закроется.\n\n" +
        "Продление " + amountRu + "/мес через Tribute, меньше минуты.";
      if (founding) cta += "\nСкидка 30% сохраняется навсегда.";
      const btn = founding ? "✨ Продлить за " + amountRu : "💳 Продлить за " + amountRu;
      return { text: header + "\n\n" + cta, button: btn, url: tributeUrl };
    }
    const header = "⚠️ Today is the last day of your BelFed subscription (" + dateStr + ").";
    let cta =
      "After it ends you will lose access to the private channel with live alerts on our trades and to the analytics.\n\n" +
      "Renewal is " + amountEn + "/month via Tribute, under a minute.";
    if (founding) cta += "\n30% discount stays with you forever.";
    const btn = founding ? "✨ Renew for " + amountEn : "💳 Renew for " + amountEn;
    return { text: header + "\n\n" + cta, button: btn, url: tributeUrl };
  }

  // ---- pre_3d -------------------------------------------------------------
  if (kind === "pre_3d") {
    if (lang === "ru") {
      const header = "⏳ Ваша подписка BelFed истекает через 3 дня (" + dateStr + ").";
      let cta = "Продлить можно заранее: оплата " + amountRu + "/мес через Tribute, доступ не прервётся.";
      if (founding) cta += "\nСкидка 30% сохраняется навсегда.";
      const btn = founding ? "✨ Продлить за " + amountRu : "💳 Продлить за " + amountRu;
      return { text: header + "\n\n" + cta, button: btn, url: tributeUrl };
    }
    const header = "⏳ Your BelFed subscription expires in 3 days (" + dateStr + ").";
    let cta = "You can renew ahead of time: " + amountEn + "/month via Tribute, no gap in access.";
    if (founding) cta += "\n30% discount stays with you forever.";
    const btn = founding ? "✨ Renew for " + amountEn : "💳 Renew for " + amountEn;
    return { text: header + "\n\n" + cta, button: btn, url: tributeUrl };
  }

  // ---- pre_1d -------------------------------------------------------------
  if (lang === "ru") {
    const header = "⏰ Ваша подписка BelFed истекает завтра (" + dateStr + ").";
    let cta = "Нажмите кнопку ниже, оплата " + amountRu + "/мес через Tribute.";
    if (founding) cta += "\nСкидка 30% сохраняется навсегда.";
    const btn = founding ? "✨ Продлить за " + amountRu : "💳 Продлить за " + amountRu;
    return { text: header + "\n\n" + cta, button: btn, url: tributeUrl };
  }
  const header = "⏰ Your BelFed subscription expires tomorrow (" + dateStr + ").";
  let cta = "Tap the button below to renew via Tribute, " + amountEn + "/month.";
  if (founding) cta += "\n30% discount stays with you forever.";
  const btn = founding ? "✨ Renew for " + amountEn : "💳 Renew for " + amountEn;
  return { text: header + "\n\n" + cta, button: btn, url: tributeUrl };
}

// ─── Email copy + template ─────────────────────────────────────────────────

function escapeHtml(s: string): string {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function emailShell(opts: {
  lang: "ru" | "en";
  preheader: string;
  innerHtml: string;
  unsubscribeToken: string | null;
}): string {
  const site = opts.lang === "en" ? SITE_EN : SITE_RU;
  const settingsUrl = `${site}/members.html#email`;
  const disclaimer = opts.lang === "en"
    ? "Service email about your BelFed Analytics subscription. Not investment advice."
    : "Служебное письмо о вашей подписке BelFed Analytics. Не является инвестиционной рекомендацией.";
  const settingsLabel = opts.lang === "en" ? "Manage notifications" : "Настроить уведомления";
  const unsubLabel = opts.lang === "en" ? "Unsubscribe" : "Отписаться";
  const unsubHtml = opts.unsubscribeToken
    ? ` &middot; <a href="${site}/unsubscribe.html?token=${encodeURIComponent(opts.unsubscribeToken)}" style="color:#666;text-decoration:underline">${unsubLabel}</a>`
    : "";
  return `<!DOCTYPE html>
<html lang="${opts.lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BelFed Analytics</title>
</head>
<body style="margin:0;padding:0;background:#f5f2eb;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Helvetica,Arial,sans-serif;color:#0a0a0a">
<div style="display:none;max-height:0;overflow:hidden;color:#f5f2eb">${escapeHtml(opts.preheader)}</div>
<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="background:#f5f2eb">
  <tr><td align="center" style="padding:32px 12px">
    <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="600" style="max-width:600px;background:#ffffff;border:2px solid #000">
      <tr><td style="padding:24px 28px;border-bottom:2px solid #000">
        <div style="font-family:'Courier New',monospace;font-size:12px;letter-spacing:3px;color:#000">BELFED&nbsp;//&nbsp;ANALYTICS</div>
      </td></tr>
      <tr><td style="padding:28px">
        ${opts.innerHtml}
      </td></tr>
      <tr><td style="padding:18px 28px;border-top:1px dashed #ccc;font-size:11px;color:#666;line-height:1.6">
        ${disclaimer}<br>
        <a href="${settingsUrl}" style="color:#666;text-decoration:underline">${settingsLabel}</a>${unsubHtml}
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>`;
}

function ctaButton(text: string, url: string): string {
  return `<a href="${url}" style="display:inline-block;padding:14px 26px;background:#000;color:#f5f2eb;text-decoration:none;font-family:'Courier New',monospace;font-size:11px;letter-spacing:2px;text-transform:uppercase;border:1px solid #000">${escapeHtml(text)}</a>`;
}

function para(text: string): string {
  return `<p style="margin:0 0 16px 0;font-size:15px;line-height:1.7;color:#1a1a1a">${text}</p>`;
}

interface EmailContent { subject: string; preheader: string; html: string; }

function buildEmail(opts: {
  lang: "ru" | "en";
  founding: boolean;
  kind: ReminderKind;
  expiresAt: Date;
  unsubscribeToken: string | null;
}): EmailContent {
  const { lang, founding, kind, expiresAt, unsubscribeToken } = opts;
  const dateStr = fmtDate(expiresAt, lang);
  const amount = lang === "ru" ? (founding ? "1050 ₽" : "1500 ₽") : (founding ? "$10.50" : "$15");
  const fullAmount = lang === "ru" ? "1500 ₽" : "$15";
  const tributeUrl = lang === "ru" ? TRIBUTE_RU_URL : TRIBUTE_EN_URL;
  const kicker = lang === "ru" ? "// ПОДПИСКА" : "// SUBSCRIPTION";

  let subject = "";
  let preheader = "";
  let headline = "";
  let bodyHtml = "";
  let button = "";

  if (kind === "pre_3d") {
    if (lang === "ru") {
      subject = "Подписка BelFed истекает через 3 дня";
      preheader = "Продлите заранее, доступ не прервётся";
      headline = "Подписка истекает " + dateStr;
      bodyHtml =
        para("Здравствуйте! Ваша подписка BelFed Analytics активна ещё 3 дня, до " + dateStr + ".") +
        para("Продлите заранее, чтобы доступ к закрытому каналу с оповещениями о наших торговых сделках и к аналитике не прерывался. Оплата " + amount + "/мес через Tribute, меньше минуты.") +
        (founding ? para("Ваша скидка Founding Member 30% сохраняется навсегда: " + amount + "/мес вместо " + fullAmount + ".") : "");
      button = "Продлить подписку";
    } else {
      subject = "Your BelFed subscription expires in 3 days";
      preheader = "Renew early so your access continues";
      headline = "Subscription expires " + dateStr;
      bodyHtml =
        para("Hi! Your BelFed Analytics subscription is active for 3 more days, until " + dateStr + ".") +
        para("Renew early so your access to the private channel with live alerts on our trades and to the analytics continues without a gap. " + amount + "/month via Tribute, takes less than a minute.") +
        (founding ? para("Your Founding Member discount of 30% is locked in for life: " + amount + "/month instead of " + fullAmount + ".") : "");
      button = "Renew subscription";
    }
  } else if (kind === "on_expiry") {
    if (lang === "ru") {
      subject = "Сегодня последний день подписки BelFed";
      preheader = "После окончания доступ к закрытому каналу закроется";
      headline = "Сегодня последний день доступа";
      bodyHtml =
        para("Здравствуйте! Сегодня последний день вашей подписки BelFed Analytics.") +
        para("После окончания закроется доступ к закрытому каналу с оповещениями о наших торговых сделках в реальном времени и к аналитике на сайте.") +
        para("Продление занимает меньше минуты: " + amount + "/мес через Tribute. Если продлите сегодня, доступ не прервётся ни на час.") +
        (founding ? para("Ваша скидка Founding Member 30% сохраняется навсегда.") : "");
      button = "Продлить за " + amount;
    } else {
      subject = "Today is the last day of your BelFed subscription";
      preheader = "Access to the private channel closes tonight";
      headline = "Last day of access";
      bodyHtml =
        para("Hi! Today is the final day of your BelFed Analytics subscription.") +
        para("After it ends you lose access to the private channel with live alerts on our trades and to the analytics on the site.") +
        para("Renewing takes under a minute: " + amount + "/month via Tribute. Renew today and your access will not be interrupted.") +
        (founding ? para("Your Founding Member discount of 30% stays with you forever.") : "");
      button = "Renew for " + amount;
    }
  } else {
    // post_3d
    if (lang === "ru") {
      subject = "Возвращайтесь в BelFed Analytics";
      preheader = "Ваша подписка завершилась, вернуть доступ можно в один клик";
      headline = "Подписка завершилась";
      bodyHtml =
        para("Здравствуйте! Ваша подписка BelFed Analytics завершилась, и вы больше не получаете:") +
        `<ul style="margin:0 0 16px 0;padding-left:20px;font-size:15px;line-height:1.8;color:#1a1a1a">
          <li>оповещения о наших торговых сделках в реальном времени</li>
          <li>перспективные идеи и главные тренды рынков</li>
          <li>аналитику ведущих инвестдомов</li>
          <li>разборы любого актива по запросу</li>
        </ul>` +
        para("Вернуть доступ можно в любой момент: " + amount + "/мес через Tribute.") +
        (founding ? para("За вами закреплено место Founding Member со скидкой 30% навсегда, " + amount + "/мес.") : "");
      button = "Вернуть доступ";
    } else {
      subject = "Come back to BelFed Analytics";
      preheader = "Your subscription ended, reactivate in one click";
      headline = "Your subscription has ended";
      bodyHtml =
        para("Hi! Your BelFed Analytics subscription has ended, so you no longer receive:") +
        `<ul style="margin:0 0 16px 0;padding-left:20px;font-size:15px;line-height:1.8;color:#1a1a1a">
          <li>live alerts on our trades</li>
          <li>the most promising ideas and key market trends</li>
          <li>research from leading investment houses</li>
          <li>on-demand breakdowns of any asset</li>
        </ul>` +
        para("You can come back any time: " + amount + "/month via Tribute.") +
        (founding ? para("Your Founding Member seat with the 30% lifetime discount is still reserved: " + amount + "/month.") : "");
      button = "Reactivate access";
    }
  }

  const inner = `
    <div style="font-family:'Courier New',monospace;font-size:11px;letter-spacing:2px;color:#666;margin-bottom:8px">${kicker}</div>
    <h1 style="margin:0 0 20px 0;font-size:24px;font-weight:700;letter-spacing:-0.3px">${escapeHtml(headline)}</h1>
    ${bodyHtml}
    <div style="margin-top:26px">${ctaButton(button, tributeUrl)}</div>
  `;

  return {
    subject,
    preheader,
    html: emailShell({ lang, preheader, innerHtml: inner, unsubscribeToken }),
  };
}

async function sendViaBrevo(opts: {
  toEmail: string;
  subject: string;
  html: string;
  lang: "ru" | "en";
  unsubscribeToken: string | null;
}): Promise<{ ok: boolean; messageId?: string; error?: string }> {
  if (!BREVO_API_KEY) return { ok: false, error: "BREVO_API_KEY missing" };
  const site = opts.lang === "en" ? SITE_EN : SITE_RU;
  const headers: Record<string, string> = {};
  if (opts.unsubscribeToken) {
    const unsubUrl = `${site}/unsubscribe.html?token=${encodeURIComponent(opts.unsubscribeToken)}`;
    headers["List-Unsubscribe"] = `<${unsubUrl}>`;
    headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click";
  }
  const payload: Record<string, any> = {
    sender:  { email: FROM_EMAIL, name: FROM_NAME },
    replyTo: { email: REPLY_TO,   name: FROM_NAME },
    to:      [{ email: opts.toEmail }],
    subject: opts.subject,
    htmlContent: opts.html,
    tags: ["renewal-reminder"],
  };
  if (Object.keys(headers).length) payload.headers = headers;
  try {
    const r = await fetch("https://api.brevo.com/v3/smtp/email", {
      method: "POST",
      headers: { "accept": "application/json", "api-key": BREVO_API_KEY, "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!r.ok) {
      const txt = await r.text().catch(() => "");
      return { ok: false, error: `brevo ${r.status}: ${txt.slice(0, 300)}` };
    }
    const j = await r.json().catch(() => ({} as Record<string, unknown>));
    return { ok: true, messageId: String((j as any).messageId ?? "") };
  } catch (e) {
    return { ok: false, error: (e as Error).message };
  }
}

interface SubscriberRow {
  id: string;
  unsubscribe_token: string;
  unsubscribed_at: string | null;
  notes: string | null;
}

// Resolves (or creates) the email_subscribers row for a paying member.
async function resolveSubscriber(email: string, lang: "ru" | "en", profileId: string): Promise<SubscriberRow | null> {
  const { data } = await sb.from("email_subscribers")
    .select("id, unsubscribe_token, unsubscribed_at, notes")
    .eq("email", email)
    .order("created_at", { ascending: true })
    .limit(1);
  if (data && data.length) return data[0] as SubscriberRow;

  const { data: created } = await sb.from("email_subscribers")
    .insert({
      email, language: lang, segment: "premium", source: "renewal_reminder",
      profile_id: profileId, confirmed_at: new Date().toISOString(),
    })
    .select("id, unsubscribe_token, unsubscribed_at, notes")
    .maybeSingle();
  return (created as SubscriberRow) ?? null;
}

function classifyReminder(expiresAt: Date, now: Date): ReminderKind | null {
  const diffDays = (expiresAt.getTime() - now.getTime()) / (24 * 60 * 60 * 1000);
  if (diffDays >= 2.5 && diffDays <= 3.5) return "pre_3d";
  if (diffDays >= 0.5 && diffDays <= 1.5) return "pre_1d";
  if (diffDays > -0.5 && diffDays < 0.5) return "on_expiry";
  if (diffDays >= -3.5 && diffDays < -2.5) return "post_3d";
  return null;
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return new Response("Method Not Allowed", { status: 405 });

  const incomingSecret = req.headers.get("x-bot-secret") ?? "";
  if (!BOT_SHARED_SECRET || incomingSecret !== BOT_SHARED_SECRET) {
    return new Response("Unauthorized", { status: 401 });
  }

  const body = await req.json().catch(() => ({}));
  const dryRun = !!body.dry_run;
  const limitToUserId: string | null = body.user_id ?? null;

  // ---- test mode: render / send one preview email, no production writes ----
  const testEmail: string | null = typeof body.test_email === "string" ? body.test_email.trim() : null;
  if (testEmail) {
    const kind = (["pre_3d", "on_expiry", "post_3d"].includes(body.test_kind) ? body.test_kind : "pre_3d") as ReminderKind;
    const lang = body.test_lang === "en" ? "en" : "ru";
    const founding = !!body.test_founding;
    const expiresAt = new Date(Date.now() + (kind === "post_3d" ? -3 : kind === "on_expiry" ? 0 : 3) * 24 * 3600 * 1000);
    const mail = buildEmail({ lang, founding, kind, expiresAt, unsubscribeToken: "preview-token" });
    if (dryRun) {
      return new Response(JSON.stringify({ version: "v12", mode: "test_preview", kind, lang, founding,
        subject: mail.subject, preheader: mail.preheader, html: mail.html }, null, 2),
        { status: 200, headers: { "Content-Type": "application/json" } });
    }
    const res = await sendViaBrevo({ toEmail: testEmail, subject: mail.subject, html: mail.html, lang, unsubscribeToken: null });
    return new Response(JSON.stringify({ version: "v12", mode: "test_send", kind, lang, founding, to: testEmail, result: res }, null, 2),
      { status: res.ok ? 200 : 500, headers: { "Content-Type": "application/json" } });
  }

  const now = new Date();

  // v10: symmetric window [-4d, +4d] to cover pre_3d ... post_3d.
  let query = sb.from("profiles")
    .select("id, telegram_id, telegram_username, email, lang, founding_member, subscription_status, subscription_plan, is_test_profile, subscription_expires_at, subscriptions:subscriptions!subscriptions_user_id_fkey(provider, status, payment_method_id, provider_subscription_id, cancel_at_period_end)")
    .not("telegram_id", "is", null)
    .not("subscription_expires_at", "is", null)
    .gte("subscription_expires_at", new Date(now.getTime() - 4 * 24 * 3600 * 1000).toISOString())
    .lte("subscription_expires_at", new Date(now.getTime() + 4 * 24 * 3600 * 1000).toISOString());

  if (limitToUserId) query = query.eq("id", limitToUserId);

  const { data: candidates, error: qErr } = await query;
  if (qErr) {
    console.error("candidate query failed", qErr);
    return new Response(JSON.stringify({ error: qErr.message }), { status: 500 });
  }

  const results: any[] = [];
  let sentCount = 0;
  let emailSentCount = 0;
  let skippedCount = 0;
  const errors: any[] = [];

  for (const row of (candidates ?? [])) {
    if ((row as any).subscription_status === "admin") {
      results.push({ user_id: row.id, skipped: "admin_profile" }); skippedCount++; continue;
    }
    if ((row as any).is_test_profile) {
      results.push({ user_id: row.id, skipped: "test_profile" }); skippedCount++; continue;
    }
    // v11: trial funnel is owned by trial-bot-reminders / trial-email-reminders
    // / telegram-winback-reminder. Never double-message trial users here.
    if ((row as any).subscription_status === "trial" || (row as any).subscription_plan === "trial") {
      results.push({ user_id: row.id, skipped: "trial_handled_by_trial_funnel" }); skippedCount++; continue;
    }

    const subs = ((row as any).subscriptions ?? []) as any[];
    const live = subs.filter((s) => s.status === "active" || s.status === "past_due");

    // v10: skip ONLY when renewal is genuinely automatic.
    const ykAuto = live.find((s) => s.provider === "yookassa" && !!s.payment_method_id);
    if (ykAuto) {
      results.push({ user_id: row.id, skipped: "yookassa_auto_renew_saved_pm" });
      skippedCount++; continue;
    }
    const starsAuto = live.find((s) => s.provider === "telegram_stars" && !!s.provider_subscription_id);
    if (starsAuto) {
      results.push({ user_id: row.id, skipped: "telegram_stars_auto_renew" });
      skippedCount++; continue;
    }
    const tributeAuto = live.find((s) => s.provider === "tribute" && !s.cancel_at_period_end);
    if (tributeAuto) {
      results.push({ user_id: row.id, skipped: "tribute_auto_renew" });
      skippedCount++; continue;
    }

    const providers = Array.from(new Set(subs.map((s) => s.provider))).join(",") || "none";

    const expiresAt = new Date(row.subscription_expires_at);
    const kind = classifyReminder(expiresAt, now);
    if (!kind) {
      results.push({ user_id: row.id, skipped: "outside_reminder_window" });
      skippedCount++; continue;
    }

    if (kind === "pre_1d" && PRE_1D_EXCLUDE_TELEGRAM_IDS.has(String(row.telegram_id))) {
      results.push({ user_id: row.id, telegram_id: row.telegram_id, kind, skipped: "excluded_has_dedicated_one_time_reminder" });
      skippedCount++; continue;
    }

    const { data: existing } = await sb.from("renewal_reminders_sent")
      .select("id, tg_message_id, email_message_id").eq("user_id", row.id).eq("reminder_kind", kind)
      .eq("expires_at", row.subscription_expires_at).maybeSingle();

    const lang = (row.lang === "en") ? "en" : "ru";
    const needTg = !existing?.id;

    // ---- email eligibility ------------------------------------------------
    const rawEmail = typeof (row as any).email === "string" ? (row as any).email.trim().toLowerCase() : "";
    let emailReason: string | null = null;
    let subscriber: SubscriberRow | null = null;
    let needEmail = false;

    if (!EMAIL_KINDS.has(kind)) {
      emailReason = "kind_is_telegram_only";
    } else if (!rawEmail) {
      emailReason = "no_email_on_profile";
    } else if (existing?.email_message_id) {
      emailReason = "email_already_sent";
    } else {
      subscriber = await resolveSubscriber(rawEmail, lang, row.id);
      if (subscriber?.unsubscribed_at && (subscriber.notes ?? "") !== "auto_expired") {
        emailReason = "manual_unsubscribe";
      } else if (!subscriber) {
        emailReason = "subscriber_row_unavailable";
      } else {
        needEmail = true;
      }
    }

    if (!needTg && !needEmail) {
      results.push({ user_id: row.id, kind, skipped: "already_sent", email_skip_reason: emailReason });
      skippedCount++; continue;
    }

    const msg = buildMessage({ lang, founding: !!row.founding_member, kind, expiresAt });
    const mail = needEmail
      ? buildEmail({ lang, founding: !!row.founding_member, kind, expiresAt,
          unsubscribeToken: subscriber?.unsubscribe_token ?? null })
      : null;

    if (dryRun) {
      results.push({ user_id: row.id, telegram_id: row.telegram_id, kind, lang, providers,
        founding: row.founding_member, would_send_telegram: needTg, would_send_email: needEmail,
        email_skip_reason: emailReason, email_to: needEmail ? rawEmail : null,
        dry_run_text: msg.text, dry_run_button: msg.button, dry_run_url: msg.url,
        dry_run_email_subject: mail?.subject ?? null });
      continue;
    }

    // ---- Telegram ---------------------------------------------------------
    let tgMessageId: number | null = existing?.tg_message_id ?? null;
    let tgOk = !needTg;
    if (needTg) {
      const sendRes: any = await tg("sendMessage", {
        chat_id: Number(row.telegram_id), text: msg.text, disable_web_page_preview: true,
        reply_markup: { inline_keyboard: [[{ text: msg.button, url: msg.url }]] },
      });
      if (sendRes?.ok) {
        tgOk = true;
        tgMessageId = sendRes?.result?.message_id ?? null;
        sentCount++;
      } else {
        errors.push({ user_id: row.id, telegram_id: row.telegram_id, kind, channel: "telegram", error: sendRes?.description ?? "unknown" });
      }
    }

    // ---- Email ------------------------------------------------------------
    let emailMessageId: string | null = null;
    let emailError: string | null = null;
    if (needEmail && mail && subscriber) {
      const res = await sendViaBrevo({
        toEmail: rawEmail, subject: mail.subject, html: mail.html, lang,
        unsubscribeToken: subscriber.unsubscribe_token,
      });
      if (res.ok) {
        emailMessageId = res.messageId ?? "";
        emailSentCount++;
      } else {
        emailError = res.error ?? "unknown";
        errors.push({ user_id: row.id, kind, channel: "email", error: emailError });
      }
      await sb.from("email_sends").insert({
        subscriber_id: subscriber.id, email: rawEmail, language: lang, segment: "renewal",
        status: res.ok ? "sent" : "error", brevo_message_id: res.ok ? (res.messageId ?? null) : null,
        error: res.ok ? null : emailError, sent_at: res.ok ? new Date().toISOString() : null,
      });
    }

    // ---- Bookkeeping ------------------------------------------------------
    if (existing?.id) {
      if (needEmail) {
        await sb.from("renewal_reminders_sent").update({
          email_message_id: emailMessageId, email_error: emailError,
          email_sent_at: emailMessageId !== null ? new Date().toISOString() : null,
        }).eq("id", existing.id);
      }
    } else if (tgOk || emailMessageId !== null) {
      await sb.from("renewal_reminders_sent").insert({
        user_id: row.id, reminder_kind: kind, expires_at: row.subscription_expires_at,
        tg_message_id: tgMessageId, email_message_id: emailMessageId,
        email_error: emailError, email_sent_at: emailMessageId !== null ? new Date().toISOString() : null,
      });
    }

    results.push({
      user_id: row.id, telegram_id: row.telegram_id, kind, lang, providers,
      telegram_sent: needTg ? tgOk : "already_sent",
      email_sent: needEmail ? (emailMessageId !== null) : false,
      email_skip_reason: emailReason, email_error: emailError,
    });
  }

  return new Response(JSON.stringify({
    version: "v13", sent: sentCount, emails_sent: emailSentCount, skipped: skippedCount,
    errors: errors.length, error_details: errors, dry_run: dryRun,
    candidates_evaluated: (candidates ?? []).length, details: results,
  }, null, 2), { status: 200, headers: { "Content-Type": "application/json" } });
});
