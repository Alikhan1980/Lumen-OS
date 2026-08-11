-- Identity: the profile row that hangs off a Supabase auth user, plus the
-- preferences and onboarding state the app needs before it can show anything.
--
-- Supabase owns `auth.users`. We never write to it and never duplicate what it
-- holds -- no password column, no email verification flag, no session table.
-- Those belong to GoTrue, which is the whole reason for using it. What lives
-- here is application state keyed by the same id.
--
-- Every table in this migration and the ones after it follows one shape:
--
--     user_id uuid not null references auth.users(id) on delete cascade
--
-- plus RLS with a policy of `user_id = auth.uid()`. That pairing is what makes
-- one user's data unreachable by another even when the application layer has a
-- bug: the check is in the database, under the query planner, not in a Python
-- `if`. See 0005_rls.sql, which turns it on for everything at once so a table
-- added without a policy fails loudly rather than silently going public.

-- Extensions -----------------------------------------------------------------

create extension if not exists "pgcrypto" with schema extensions;
create extension if not exists "citext" with schema extensions;

-- Helpers --------------------------------------------------------------------

-- `updated_at` maintained by the database rather than by every caller, so a
-- route that forgets cannot leave a stale timestamp behind.
create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

-- Profiles -------------------------------------------------------------------

create table if not exists public.profiles (
  -- Same id as auth.users. A separate surrogate key would let the two drift.
  id            uuid primary key references auth.users(id) on delete cascade,
  display_name  text not null default '',
  avatar_url    text,
  -- IANA name. Reminders are wall-clock things, so the server has to know the
  -- user's zone to resolve "17:00" the same way agent/reminders.py does locally.
  timezone      text not null default 'UTC',
  -- Cleared to false when onboarding is reset; gates the router in the client.
  onboarded_at  timestamptz,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),

  constraint profiles_display_name_len check (char_length(display_name) <= 120),
  constraint profiles_avatar_url_len   check (avatar_url is null or char_length(avatar_url) <= 2048),
  constraint profiles_timezone_len     check (char_length(timezone) between 1 and 64)
);

create trigger profiles_touch
  before update on public.profiles
  for each row execute function public.touch_updated_at();

-- Preferences ----------------------------------------------------------------

-- One row per user rather than a key/value table: the set of preferences is
-- known at build time, and columns give us defaults and check constraints that
-- a jsonb blob would not.
create table if not exists public.user_preferences (
  user_id             uuid primary key references auth.users(id) on delete cascade,

  -- AI preferences. Mirrors what agent/prompts.py already understands, so the
  -- server can hand the agent a style without a translation table.
  response_style      text not null default 'default',
  show_thinking       boolean not null default false,
  auto_approve_tools  boolean not null default false,

  -- Notifications.
  email_notifications boolean not null default true,
  reminder_push       boolean not null default true,
  weekly_digest       boolean not null default false,

  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),

  constraint user_preferences_style check (
    response_style in ('default', 'brief', 'detailed', 'friendly', 'formal')
  )
);

create trigger user_preferences_touch
  before update on public.user_preferences
  for each row execute function public.touch_updated_at();

-- Provisioning ---------------------------------------------------------------

-- A user with no profile row is a user the app cannot render. Doing this in a
-- trigger rather than in the signup route means it also holds for users created
-- by a social login, by the Supabase dashboard, or by an admin script -- every
-- path into auth.users, not just the one path we happen to have written.
create or replace function public.provision_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.profiles (id, display_name)
  values (
    new.id,
    -- Social providers put a name in the identity payload; email signup puts
    -- one there too because our signup route sends it. Fall back to the local
    -- part of the address rather than showing an empty account menu.
    coalesce(
      nullif(trim(new.raw_user_meta_data ->> 'display_name'), ''),
      nullif(trim(new.raw_user_meta_data ->> 'full_name'), ''),
      nullif(trim(new.raw_user_meta_data ->> 'name'), ''),
      split_part(coalesce(new.email, ''), '@', 1),
      'there'
    )
  )
  on conflict (id) do nothing;

  insert into public.user_preferences (user_id)
  values (new.id)
  on conflict (user_id) do nothing;

  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.provision_new_user();
