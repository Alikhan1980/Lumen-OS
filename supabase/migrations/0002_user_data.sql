-- The user's own content: conversations with the agent, tasks, and reminders.
--
-- The reminders tables mirror agent/reminders.py so the existing local module
-- ports across without a rewrite of its logic. Two differences, both forced by
-- moving to a server:
--
--   * `user_id`, on every row, non-null, cascading from auth.users.
--   * Real timestamptz instead of ISO strings in TEXT. SQLite had no date type;
--     Postgres does, and the notifier's "what is due" query wants an index it
--     can actually use.
--
-- The wall-clock/instant pair is kept exactly as the local module has it, and
-- for the same reason: `due_local` is what the user asked for ("17:00"),
-- `due_utc` is the instant that resolves to today. A daily 5pm reminder stays
-- at 5pm through a DST change because recurrence advances the wall clock and
-- re-resolves the instant, rather than adding 24 hours to a UTC timestamp.

-- Conversations --------------------------------------------------------------

create table if not exists public.conversations (
  id          uuid primary key default extensions.gen_random_uuid(),
  user_id     uuid not null references auth.users(id) on delete cascade,
  title       text not null default '',
  -- Which provider/model answered. Recorded per conversation rather than per
  -- message because a conversation that switched models mid-way is worth being
  -- able to see.
  provider_id text,
  model       text,
  archived_at timestamptz,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),

  constraint conversations_title_len check (char_length(title) <= 300)
);

-- The list view: this user's conversations, newest first, archived ones out.
create index if not exists conversations_owner_recent
  on public.conversations (user_id, updated_at desc)
  where archived_at is null;

create trigger conversations_touch
  before update on public.conversations
  for each row execute function public.touch_updated_at();

create table if not exists public.messages (
  id              bigint generated always as identity primary key,
  conversation_id uuid not null references public.conversations(id) on delete cascade,
  -- Denormalised from the conversation on purpose. RLS policies that have to
  -- join to find the owner are both slower and easier to get wrong; with the
  -- owner on the row, the policy on this table is the same one-liner as
  -- everywhere else. The trigger below is what keeps it honest.
  user_id         uuid not null references auth.users(id) on delete cascade,
  role            text not null,
  content         text not null default '',
  -- Tool calls and their results, as the provider-neutral blocks in
  -- agent/providers/base.py serialise them.
  blocks          jsonb,
  created_at      timestamptz not null default now(),

  constraint messages_role check (role in ('user', 'assistant', 'system', 'tool'))
);

create index if not exists messages_thread
  on public.messages (conversation_id, id);

-- A message can only ever be filed under a conversation its own owner holds.
-- Without this, a caller who guessed a conversation id could write a row whose
-- user_id was their own -- passing the RLS check -- into someone else's thread.
create or replace function public.enforce_message_owner()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  owner uuid;
begin
  select user_id into owner
    from public.conversations
   where id = new.conversation_id;

  if owner is null then
    raise exception 'conversation not found'
      using errcode = 'foreign_key_violation';
  end if;

  if owner <> new.user_id then
    raise exception 'message owner does not match conversation owner'
      using errcode = 'insufficient_privilege';
  end if;

  return new;
end;
$$;

create trigger messages_owner_matches
  before insert or update on public.messages
  for each row execute function public.enforce_message_owner();

-- Tasks ----------------------------------------------------------------------

create table if not exists public.tasks (
  id           uuid primary key default extensions.gen_random_uuid(),
  user_id      uuid not null references auth.users(id) on delete cascade,
  title        text not null,
  notes        text not null default '',
  status       text not null default 'pending',
  due_at       timestamptz,
  completed_at timestamptz,
  -- Set when this task mirrors a Google Tasks item, so a sync can match them up
  -- without keeping a second mapping table.
  external_id  text,
  source       text not null default 'local',
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),

  constraint tasks_title_len check (char_length(title) between 1 and 500),
  constraint tasks_status check (status in ('pending', 'completed', 'cancelled')),
  constraint tasks_source check (source in ('local', 'google'))
);

create index if not exists tasks_owner_due
  on public.tasks (user_id, status, due_at);

-- One row per external task per user. Scoped by user_id so two users mirroring
-- the same shared Google list do not collide with each other.
create unique index if not exists tasks_external_unique
  on public.tasks (user_id, source, external_id)
  where external_id is not null;

create trigger tasks_touch
  before update on public.tasks
  for each row execute function public.touch_updated_at();

-- Reminders ------------------------------------------------------------------

create table if not exists public.reminders (
  id            uuid primary key default extensions.gen_random_uuid(),
  user_id       uuid not null references auth.users(id) on delete cascade,
  title         text not null,
  notes         text not null default '',
  -- The instant the next occurrence fires.
  due_utc       timestamptz not null,
  -- The wall clock the user asked for, and the zone it means. Kept as text in
  -- the same shape agent/reminders.py writes.
  due_local     text not null,
  tz            text not null default '',
  recurrence    text not null default 'none',
  tags          text not null default '',
  status        text not null default 'pending',
  notified_utc  timestamptz,
  snoozed_from  timestamptz,
  completed_utc timestamptz,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),

  constraint reminders_title_len check (char_length(title) between 1 and 500),
  constraint reminders_status check (status in ('pending', 'completed', 'cancelled')),
  constraint reminders_recurrence check (
    recurrence in ('none', 'daily', 'weekdays', 'weekly', 'monthly', 'yearly')
  )
);

-- What the notifier sweeps: pending reminders that have come due. Partial, so
-- the index stays small no matter how much completed history accumulates.
create index if not exists reminders_due
  on public.reminders (user_id, due_utc)
  where status = 'pending';

create trigger reminders_touch
  before update on public.reminders
  for each row execute function public.touch_updated_at();

create table if not exists public.reminder_log (
  id             bigint generated always as identity primary key,
  reminder_id    uuid not null references public.reminders(id) on delete cascade,
  -- Denormalised owner, same reasoning as messages.user_id above.
  user_id        uuid not null references auth.users(id) on delete cascade,
  kind           text not null,
  at_utc         timestamptz not null default now(),
  occurrence_utc timestamptz,
  detail         text not null default '',

  constraint reminder_log_kind check (
    kind in ('fired', 'completed', 'snoozed', 'cancelled', 'created', 'updated')
  )
);

create index if not exists reminder_log_ref
  on public.reminder_log (reminder_id, at_utc desc);

create or replace function public.enforce_reminder_log_owner()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  owner uuid;
begin
  select user_id into owner
    from public.reminders
   where id = new.reminder_id;

  if owner is null then
    raise exception 'reminder not found'
      using errcode = 'foreign_key_violation';
  end if;

  if owner <> new.user_id then
    raise exception 'log owner does not match reminder owner'
      using errcode = 'insufficient_privilege';
  end if;

  return new;
end;
$$;

create trigger reminder_log_owner_matches
  before insert or update on public.reminder_log
  for each row execute function public.enforce_reminder_log_owner();
