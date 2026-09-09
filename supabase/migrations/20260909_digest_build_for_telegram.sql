-- BelFed service digest, live definition dumped from production 2026-09-09 (Supabase obujqvqqmyfcfflhqvud).
-- Apply order: 01 .. 07. Access: service_role only.

CREATE OR REPLACE FUNCTION public.build_digest_for_telegram(p_telegram_id text, p_kind text DEFAULT 'day'::text)
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_lang   text;
  v_status text;
  v_tz     text;
  v_kind   text := lower(coalesce(p_kind, 'day'));
  v_from   timestamptz;
  v_to     timestamptz := now();
  v_msg    jsonb;
begin
  if v_kind not in ('day', 'week') then
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

  if v_kind = 'week' then
    v_from := now() - interval '7 days';
  else
    v_from := (date_trunc('day', now() at time zone v_tz)) at time zone v_tz;
  end if;

  v_msg := public.build_service_digest_message(v_lang, v_from, v_to, v_kind);

  return jsonb_build_object('status', 'ok', 'lang', v_lang, 'kind', v_kind) || v_msg;
end;
$function$

REVOKE ALL ON FUNCTION public.build_digest_for_telegram(text, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.build_digest_for_telegram(text, text) TO service_role;
