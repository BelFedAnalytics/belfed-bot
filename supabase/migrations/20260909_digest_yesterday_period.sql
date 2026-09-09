-- BelFed service digest: add the "yesterday" period to the Telegram resolver.
--
-- The renderer already labels a full-day window that starts yesterday 00:00 as
-- "Что было на сервисе за вчера" / "What happened yesterday", so only the
-- resolver needs to know the new period. p_to is no longer pinned to now():
-- for "yesterday" the window has to close at today 00:00 local time, otherwise
-- the digest would silently include today's events.
--
-- Day boundaries are computed in the language timezone (Europe/Moscow for ru,
-- Europe/London for en) and the -1 day step is taken on the local timestamp,
-- so a DST switch cannot shift the window by an hour.

CREATE OR REPLACE FUNCTION public.build_digest_for_telegram(
  p_telegram_id text,
  p_kind        text DEFAULT 'day'::text
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE SECURITY DEFINER
SET search_path TO 'public'
AS $function$
declare
  v_lang        text;
  v_status      text;
  v_tz          text;
  v_kind        text := lower(coalesce(p_kind, 'day'));
  v_render_kind text;
  v_day_start   timestamptz;
  v_from        timestamptz;
  v_to          timestamptz;
  v_msg         jsonb;
begin
  if v_kind not in ('day', 'yesterday', 'week') then
    v_kind := 'day';
  end if;

  select case when lower(coalesce(p.lang, 'ru')) = 'en' then 'en' else 'ru' end,
         lower(coalesce(p.subscription_status, ''))
    into v_lang, v_status
  from public.profiles p
  where p.telegram_id = p_telegram_id
  order by (case lower(coalesce(p.subscription_status, ''))
              when 'admin' then 0 when 'active' then 1 when 'trial' then 2 else 3 end)
  limit 1;

  if v_lang is null then
    return jsonb_build_object('status', 'not_linked');
  end if;

  if v_status not in ('active', 'trial', 'admin') then
    return jsonb_build_object('status', 'no_access', 'lang', v_lang);
  end if;

  v_tz := case when v_lang = 'en' then 'Europe/London' else 'Europe/Moscow' end;
  v_day_start := (date_trunc('day', now() at time zone v_tz)) at time zone v_tz;

  if v_kind = 'week' then
    v_from := now() - interval '7 days';
    v_to   := now();
  elsif v_kind = 'yesterday' then
    v_from := (date_trunc('day', (now() at time zone v_tz) - interval '1 day')) at time zone v_tz;
    v_to   := v_day_start;
  else
    v_from := v_day_start;
    v_to   := now();
  end if;

  -- 'yesterday' uses the daily layout; the header is derived from the window.
  v_render_kind := case when v_kind = 'week' then 'week' else 'day' end;

  v_msg := public.build_service_digest_message(v_lang, v_from, v_to, v_render_kind);

  -- v_msg carries kind = v_render_kind, so the requested kind is re-applied last
  -- and the caller always sees the period it asked for.
  return jsonb_build_object('status', 'ok', 'lang', v_lang)
         || v_msg
         || jsonb_build_object('kind', v_kind);
end;
$function$;

REVOKE ALL ON FUNCTION public.build_digest_for_telegram(text, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.build_digest_for_telegram(text, text) TO service_role;
