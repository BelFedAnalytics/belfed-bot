// deno-lint-ignore-file no-explicit-any
// Supabase Edge Function: bot-claim-trial v27
//
// TWO MODES (dispatched by body.mode):
//
//   1) mode="trial" (default, backward compatible):
//      Called by the Telegram bot (bot.py) on /start trial deep-link.
//      Creates a "lite" profile (ghost-email tg_<id>@belfed.local) with a
//      14-day trial via RPC claim_trial_by_telegram, then returns a one-shot
//      invite link to the paid Telegram group (RU or EN).
//
//   2) mode="promo":
//      Called by the Telegram bot on /start promo_<CODE> deep-link.
//      Redeems a promo_codes row: extends the profile's trial by extra_days,
//      stamps promo_codes.used_by_profile_id + used_at, and returns a fresh
//      one-shot invite link. BFWB-* winback codes additionally forfeit the
//      Founding-Member seat by setting profiles.founding_member_eligible=false.
//      Profile must already exist (created earlier via mode=trial or the web
//      onboarding flow); otherwise returns 404 profile_not_found.
//
// AUTH: shared secret in header `x-bot-secret` must match BOT_SHARED_SECRET env.
//
// REQUIRED SECRETS:
//   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
//   TELEGRAM_BOT_TOKEN              — for createChatInviteLink call
//   TRADING_CHANNEL_ID              — RU paid chat_id
//   TRADING_CHANNEL_ID_EN           — EN paid chat_id
//   BOT_SHARED_SECRET               — random string, also set on bot server
//   TRIAL_DAYS                      — optional override of canonical 14-day trial length

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL          = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE_KEY      = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const BOT_TOKEN             = Deno.env.get("TELEGRAM_BOT_TOKEN")!;
const TRADING_CHANNEL_ID    = Deno.env.get("TRADING_CHANNEL_ID")!;
const TRADING_CHANNEL_ID_EN = Deno.env.get("TRADING_CHANNEL_ID_EN") ?? "";
const BOT_SHARED_SECRET     = Deno.env.get("BOT_SHARED_SECRET")!;
const TRIAL_DAYS            = (() => {
  const n = parseInt(Deno.env.get("TRIAL_DAYS") ?? "", 10);
  return Number.isFinite(n) && n > 0 ? n : 14;
})();

const cors = {
  "Access-Control-Allow-Origin":  "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type, x-bot-secret",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const json = (b: unknown, s = 200) =>
  new Response(JSON.stringify(b), { status: s, headers: { ...cors, "Content-Type": "application/json" } });

function pickChannelId(lang: string): string {
  if (lang === "en" && TRADING_CHANNEL_ID_EN) return TRADING_CHANNEL_ID_EN;
  return TRADING_CHANNEL_ID;
}

async function createInviteLink(chatId: string, expireSeconds = 24 * 3600): Promise<string | null> {
  const url = `https://api.telegram.org/bot${BOT_TOKEN}/createChatInviteLink`;
  const expireDate = Math.floor(Date.now() / 1000) + expireSeconds;
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chat_id: chatId,
      expire_date: expireDate,
      member_limit: 1,
      creates_join_request: false,
      name: `claim-${Date.now()}`,
    }),
  });
  if (!r.ok) {
    const text = await r.text();
    console.error("createChatInviteLink failed:", r.status, text, "chat_id=", chatId);
    return null;
  }
  const j = await r.json();
  return j?.result?.invite_link ?? null;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "POST")    return new Response("Method Not Allowed", { status: 405, headers: cors });

  const incomingSecret = req.headers.get("x-bot-secret") ?? "";
  if (!BOT_SHARED_SECRET || incomingSecret !== BOT_SHARED_SECRET) {
    return json({ ok: false, error: "unauthorized" }, 401);
  }

  let body: any;
  try {
    body = await req.json();
  } catch {
    return json({ ok: false, error: "invalid_json" }, 400);
  }

  const telegramIdRaw = body?.telegram_id;
  const telegramId = telegramIdRaw == null ? "" : String(telegramIdRaw).trim();
  const telegramUsername = body?.telegram_username ? String(body.telegram_username).trim() : null;
  const langRaw = body?.lang ? String(body.lang).trim().toLowerCase() : "ru";
  let lang = (langRaw === "en") ? "en" : "ru";
  const mode = body?.mode ? String(body.mode).trim().toLowerCase() : "trial";

  if (!telegramId || !/^\d+$/.test(telegramId)) {
    return json({ ok: false, error: "telegram_id_required" }, 400);
  }

  const admin = createClient(SUPABASE_URL, SERVICE_ROLE_KEY, { auth: { persistSession: false } });

  // ═════════════════════════════════════════════════════════════
  // MODE: promo (BFWB-* and other future promo codes)
  // ═════════════════════════════════════════════════════════════
  if (mode === "promo") {
    const promoCodeRaw = body?.promo_code ? String(body.promo_code).trim() : "";
    const promoCode = promoCodeRaw.toUpperCase();
    if (!promoCode) return json({ ok: false, error: "promo_code_required" }, 400);

    const { data: promo, error: promoErr } = await admin
      .from("promo_codes")
      .select("id, code, extra_days, for_telegram_id, for_email, used_at, used_by_profile_id, bfwb_variant")
      .eq("code", promoCode)
      .maybeSingle();

    if (promoErr) return json({ ok: false, error: "db_error", detail: promoErr.message }, 500);
    if (!promo) return json({ ok: false, error: "promo_not_found" }, 404);
    if (promo.used_at) return json({ ok: false, error: "promo_already_used" }, 409);

    if (promo.for_telegram_id && String(promo.for_telegram_id).trim() !== telegramId) {
      return json({ ok: false, error: "promo_not_for_this_user" }, 403);
    }

    const { data: profile, error: profErr } = await admin
      .from("profiles")
      .select("id, telegram_id, lang, subscription_expires_at, trial_end, subscription_status, founding_member, founding_member_eligible")
      .eq("telegram_id", telegramId)
      .maybeSingle();

    if (profErr) return json({ ok: false, error: "db_error", detail: profErr.message }, 500);
    if (!profile) return json({ ok: false, error: "profile_not_found" }, 404);

    const extraDays = Number(promo.extra_days) > 0 ? Number(promo.extra_days) : 7;
    const nowMs = Date.now();
    const existingMs = Math.max(
      profile.subscription_expires_at ? Date.parse(profile.subscription_expires_at) : 0,
      profile.trial_end ? Date.parse(profile.trial_end) : 0,
    );
    const baseMs = Math.max(nowMs, existingMs);
    const newExpires = new Date(baseMs + extraDays * 24 * 3600 * 1000).toISOString();

    const updateFields: Record<string, unknown> = {
      subscription_expires_at: newExpires,
      trial_end: newExpires,
      subscription_status: "trial",
    };
    if (promoCode.startsWith("BFWB-") || promo.bfwb_variant) {
      updateFields.founding_member_eligible = false;
    }

    const { error: updProfErr } = await admin.from("profiles").update(updateFields).eq("id", profile.id);
    if (updProfErr) return json({ ok: false, error: "db_error", detail: updProfErr.message }, 500);

    const { data: stampedRows, error: stampErr } = await admin
      .from("promo_codes")
      .update({ used_at: new Date().toISOString(), used_by_profile_id: profile.id })
      .eq("id", promo.id).is("used_at", null).select("id");
    if (stampErr) return json({ ok: false, error: "db_error", detail: stampErr.message }, 500);
    if (!stampedRows || stampedRows.length === 0) return json({ ok: false, error: "promo_already_used" }, 409);

    const effectiveLang = profile.lang === "en" ? "en" : (lang === "en" ? "en" : "ru");
    const chatId = pickChannelId(effectiveLang);
    const inviteLink = await createInviteLink(chatId);

    if (inviteLink) {
      try {
        await admin.from("telegram_access_log").insert({
          user_id: profile.id,
          telegram_id: parseInt(telegramId, 10),
          chat_id: parseInt(chatId, 10),
          action: "invite",
          result: "ok",
          detail: `promo ${promoCode} redeemed (+${extraDays}d); invite=${inviteLink}`,
        });
      } catch (e) { console.warn("access log insert failed:", e); }
    }

    return json({
      ok: true,
      user_id: profile.id,
      trial_end: newExpires,
      extra_days: extraDays,
      promo_code: promoCode,
      bfwb_variant: promo.bfwb_variant ?? null,
      founding_member_eligible_after: (updateFields.founding_member_eligible === false) ? false : (profile.founding_member_eligible !== false),
      invite_link: inviteLink,
      lang: effectiveLang,
    });
  }

  // ═════════════════════════════════════════════════════════════
  // MODE: trial
  // ═════════════════════════════════════════════════════════════
  let source = body?.source ? String(body.source).trim() : "telegram_direct";
  const intentToken = body?.intent_token ? String(body.intent_token).trim() : "";
  let trialIntent: any = null;

  // A signed web intent is authoritative for locale, attribution, email and
  // consent. Telegram's language_code is only a fallback for direct bot starts.
  if (intentToken) {
    const { data: intent, error: intentErr } = await admin
      .from("trial_intents")
      .select(
        "token, intent_type, lang, source, email, privacy_consent_at, terms_consent_at, consent_ip, consent_ua, consent_locale",
      )
      .eq("token", intentToken)
      .eq("intent_type", "trial")
      .is("consumed_at", null)
      .gt("expires_at", new Date().toISOString())
      .maybeSingle();

    if (intentErr) {
      console.error("trial intent lookup failed:", intentErr);
      return json({ ok: false, error: "db_error", detail: intentErr.message }, 500);
    }
    if (!intent) {
      return json({ ok: false, error: "invalid_or_expired_intent" }, 409);
    }

    trialIntent = intent;
    lang = intent.lang === "en" ? "en" : "ru";
    source = intent.source ? String(intent.source).trim() : source;
  }

  const logAttempt = async (phase: "started" | "succeeded" | "failed", extra: { userId?: string | null; errorCode?: string | null; } = {}) => {
    try {
      await admin.from("trial_activation_attempts").insert({
        telegram_id: parseInt(telegramId, 10),
        source, lang, phase,
        user_id: extra.userId ?? null,
        error_code: extra.errorCode ?? null,
      });
    } catch (e) {
      console.warn("activation-attempt log insert failed:", (e as any)?.message ?? e);
    }
  };

  await logAttempt("started");

  const { data: claimRes, error: claimErr } = await admin.rpc("claim_trial_by_telegram", {
    p_telegram_id: telegramId,
    p_telegram_username: telegramUsername,
    p_trial_days: TRIAL_DAYS,
    p_source: source,
    p_lang: lang,
    p_email: trialIntent?.email ?? null,
    p_privacy_consent_at: trialIntent?.privacy_consent_at ?? null,
    p_terms_consent_at: trialIntent?.terms_consent_at ?? null,
    p_consent_ip: trialIntent?.consent_ip ?? null,
    p_consent_ua: trialIntent?.consent_ua ?? null,
    p_consent_locale: trialIntent?.consent_locale ?? null,
  });

  if (claimErr) {
    console.error("claim_trial RPC failed:", claimErr);
    await logAttempt("failed", { errorCode: (claimErr as any)?.code ?? "db_error" });
    return json({ ok: false, error: "db_error", detail: claimErr.message }, 500);
  }

  const result = claimRes as any;
  const effectiveLang = (result?.lang === "en") ? "en" : "ru";
  const chatId = pickChannelId(effectiveLang);
  const activeFallback = (
    result?.error === "trial_already_used" &&
    result?.subscription_status === "active"
  );

  if (trialIntent && (result?.ok || activeFallback)) {
    const { error: consumeErr } = await admin
      .from("trial_intents")
      .update({
        consumed_at: new Date().toISOString(),
        consumed_by_telegram_id: parseInt(telegramId, 10),
        consumed_by_user_id: result?.user_id ?? null,
        profile_id: result?.user_id ?? null,
      })
      .eq("token", intentToken)
      .is("consumed_at", null);

    if (consumeErr) {
      console.error("trial intent consume failed:", consumeErr);
      await logAttempt("failed", {
        userId: result?.user_id ?? null,
        errorCode: "intent_consume_failed",
      });
      return json({
        ok: false,
        error: "intent_consume_failed",
        lang: effectiveLang,
        source,
      }, 500);
    }
  }

  if (!result?.ok) {
    if (activeFallback) {
      await logAttempt("succeeded", { userId: result?.user_id ?? null });
      const inviteLink = await createInviteLink(chatId);
      return json({
        ok: true,
        user_id: result.user_id,
        already_active: true,
        invite_link: inviteLink,
        lang: effectiveLang,
        source,
      });
    }
    await logAttempt("failed", { userId: result?.user_id ?? null, errorCode: result?.error ?? "claim_failed" });
    return json({
      ok: false,
      error: result?.error ?? "claim_failed",
      user_id: result?.user_id ?? null,
      trial_end: result?.trial_end ?? null,
      subscription_status: result?.subscription_status ?? null,
      lang: effectiveLang,
      source,
    }, 409);
  }

  await logAttempt("succeeded", { userId: result?.user_id ?? null });

  const inviteLink = await createInviteLink(chatId);
  if (!inviteLink) {
    return json({
      ok: true,
      user_id: result.user_id,
      trial_end: result.trial_end,
      created: result.created ?? false,
      already_active: result.already_active ?? false,
      invite_link: null,
      lang: effectiveLang,
      source,
      warning: "invite_link_creation_failed",
    });
  }

  try {
    await admin.from("telegram_access_log").insert({
      user_id: result.user_id,
      telegram_id: parseInt(telegramId, 10),
      chat_id: parseInt(chatId, 10),
      action: "invite",
      result: "ok",
      detail: `trial granted via ${source} [lang=${effectiveLang}]; invite=${inviteLink}`,
    });
  } catch (e) {
    console.warn("access log insert failed:", e);
  }

  return json({
    ok: true,
    user_id: result.user_id,
    trial_end: result.trial_end,
    created: result.created ?? false,
    already_active: result.already_active ?? false,
    invite_link: inviteLink,
    lang: effectiveLang,
    source,
  });
});
