create or replace view public.users_for_founding_renewal_reminder as
select
  p.id as user_id,
  p.telegram_id,
  p.founding_locale as locale,
  s.current_period_end,
  s.id as subscription_id,
  s.payment_method_id,
  s.provider,
  s.provider_subscription_id,
  s.cancel_at_period_end
from public.profiles p
join public.subscriptions s on s.user_id = p.id
where p.founding_member = true
  and p.telegram_id is not null
  and s.status = any (array['active'::text, 'past_due'::text])
  and s.current_period_end is not null
  and s.current_period_end >= (now() + interval '2 days')
  and s.current_period_end < (now() + interval '5 days')
  and p.founding_renewal_reminder_sent_at is null;
