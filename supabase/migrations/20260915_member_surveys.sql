-- Founding Member feedback waves: one row per (profile, wave).
--
-- Answers are stored as short stable codes in jsonb, never as button captions:
-- captions differ per language and get reworded over time, codes stay
-- comparable across waves and locales.
--
-- The row is created when the first card is sent and updated on every tap, so a
-- survey abandoned midway is still data (which question people stop at).

create table if not exists public.member_surveys (
  id            bigint generated always as identity primary key,
  profile_id    uuid not null references public.profiles(id) on delete cascade,
  telegram_id   bigint,
  wave          text not null check (wave in ('fm0', 'fm7')),
  lang          text not null default 'ru' check (lang in ('ru', 'en')),
  answers       jsonb not null default '{}'::jsonb,
  open_answer   text,
  sent_by       text not null default 'manual',
  started_at    timestamptz not null default now(),
  completed_at  timestamptz,
  updated_at    timestamptz not null default now()
);

create unique index if not exists member_surveys_profile_wave_uidx
  on public.member_surveys (profile_id, wave);

create index if not exists member_surveys_wave_idx
  on public.member_surveys (wave, completed_at);

comment on table  public.member_surveys is
  'Founding Member survey waves (fm0 = day of payment, fm7 = one week later).';
comment on column public.member_surveys.answers is
  'Short answer codes, e.g. {"q1":["analytics","assets"],"q2":"x","q5":["crypto"]}.';
comment on column public.member_surveys.completed_at is
  'Set when the last required question is answered; null means abandoned midway.';

create or replace function public.member_surveys_touch()
returns trigger
language plpgsql
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists member_surveys_touch_trg on public.member_surveys;
create trigger member_surveys_touch_trg
  before update on public.member_surveys
  for each row execute function public.member_surveys_touch();

-- Bot and edge functions talk to this table with the service role only.
alter table public.member_surveys enable row level security;
revoke all on table public.member_surveys from anon, authenticated;
grant select, insert, update on table public.member_surveys to service_role;

-- Analytics view: one row per wave with distributions and completion rate.
create or replace view public.fm_survey_summary as
select
  wave,
  lang,
  count(*)                                             as started,
  count(completed_at)                                  as completed,
  round(100.0 * count(completed_at) / nullif(count(*), 0), 1) as completion_pct,
  count(open_answer)                                   as with_open_answer,
  jsonb_agg(answers order by started_at)               as raw_answers
from public.member_surveys
group by wave, lang;

revoke all on public.fm_survey_summary from anon, authenticated;
grant select on public.fm_survey_summary to service_role;
