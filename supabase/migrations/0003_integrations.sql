-- Connected integrations, and the OAuth credentials behind them.
--
-- Split across two tables on purpose, and the split is the security control:
--
--   integration_connections -- what the user is allowed to see and manage.
--                              Which provider, which account, which scopes,
--                              connected when, last used when.
--
--   integration_secrets     -- the access and refresh tokens. RLS grants the
--                              `authenticated` role NOTHING on this table, not
--                              even select on its own rows. The only way in is
--                              the backend's service connection.
--
-- That is why a refresh token cannot leak to a frontend by accident. It is not
-- a matter of remembering to strip a field from a response: the query a user's
-- own JWT can run returns zero rows. A route would have to deliberately switch
-- to the admin connection to read one, and there is exactly one module that can
-- (server/db.py: `service_connection`), which makes the audit surface a grep.
--
-- The tokens are also encrypted at rest with AES-256-GCM before they are
-- written (server/security/crypto.py), so a database dump on its own does not
-- yield a working Google credential -- the key lives in the server's
-- environment, not in Postgres.

create table if not exists public.integration_connections (
  id            uuid primary key default extensions.gen_random_uuid(),
  user_id       uuid not null references auth.users(id) on delete cascade,
  provider      text not null,
  -- Which account at that provider was connected -- the Google address, say.
  -- Shown in the UI so a user with two Gmail accounts knows which one this is.
  account_email text,
  -- The provider's own stable id for the account, when it gives one. Survives
  -- an email change in a way the address does not.
  account_id    text,
  -- Exactly the scopes the provider said it granted, which is not always the
  -- set that was asked for. The permission screen renders this, so it has to be
  -- what is actually held rather than what was requested.
  scopes        text[] not null default '{}',
  status        text not null default 'connected',
  connected_at  timestamptz not null default now(),
  last_used_at  timestamptz,
  -- Set when the credential stops working -- a revoked grant, a changed
  -- password -- so the UI can offer "reconnect" instead of failing silently.
  needs_reauth_at timestamptz,
  revoked_at    timestamptz,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),

  constraint integration_connections_provider check (provider in ('google', 'apple', 'microsoft')),
  constraint integration_connections_status check (
    status in ('connected', 'needs_reauth', 'revoked')
  )
);

-- One live connection per provider per user. Revoked rows are kept for the
-- audit trail, so the uniqueness only applies to the live one.
create unique index if not exists integration_connections_live
  on public.integration_connections (user_id, provider)
  where revoked_at is null;

create index if not exists integration_connections_owner
  on public.integration_connections (user_id, provider);

create trigger integration_connections_touch
  before update on public.integration_connections
  for each row execute function public.touch_updated_at();

-- The credentials themselves. Nothing the user's own token can reach.
create table if not exists public.integration_secrets (
  connection_id      uuid primary key
                       references public.integration_connections(id) on delete cascade,
  -- Denormalised owner so account deletion cascades cleanly from either side,
  -- and so the service connection can scope its own queries by user without a
  -- join it might forget.
  user_id            uuid not null references auth.users(id) on delete cascade,
  -- AES-256-GCM ciphertext, base64. Never a bare token, in any column, ever.
  access_token_enc   text,
  refresh_token_enc  text,
  -- Which key version encrypted these, so a key rotation can re-wrap rows in
  -- the background instead of logging everyone out.
  key_version        integer not null default 1,
  access_expires_at  timestamptz,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

create trigger integration_secrets_touch
  before update on public.integration_secrets
  for each row execute function public.touch_updated_at();

-- OAuth handshake state ------------------------------------------------------

-- The `state` parameter for an integration connect, held server-side. This is
-- what stops an attacker from feeding a victim a callback URL that attaches the
-- attacker's Google account to the victim's session.
--
-- Single-use (consumed_at) and short-lived (expires_at). The value stored is a
-- SHA-256 of the state, not the state itself: a leaked database row is then not
-- a usable handshake.
create table if not exists public.oauth_states (
  state_hash    text primary key,
  user_id       uuid not null references auth.users(id) on delete cascade,
  provider      text not null,
  -- PKCE verifier for the code exchange, encrypted like any other secret.
  code_verifier_enc text,
  -- Where to send the browser once the exchange finishes. Validated against an
  -- allowlist before it is written -- never taken raw from the request.
  redirect_to   text,
  requested_scopes text[] not null default '{}',
  created_at    timestamptz not null default now(),
  expires_at    timestamptz not null,
  consumed_at   timestamptz
);

create index if not exists oauth_states_expiry on public.oauth_states (expires_at);

-- Audit ----------------------------------------------------------------------

-- Security-relevant events, kept so a user can answer "was that me?" and so an
-- operator can investigate an incident. Deliberately narrow: an event name, the
-- user, coarse request metadata. No tokens, no request bodies, no message
-- content -- see server/observability/logging.py for the same rule applied to
-- the application log.
create table if not exists public.auth_events (
  id         bigint generated always as identity primary key,
  user_id    uuid references auth.users(id) on delete set null,
  event      text not null,
  -- Truncated to a /24 (or /48 for v6) before it is written. Enough to spot a
  -- credential-stuffing run, not enough to be a location history.
  ip_prefix  text,
  user_agent text,
  detail     jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),

  constraint auth_events_user_agent_len check (user_agent is null or char_length(user_agent) <= 400)
);

create index if not exists auth_events_owner on public.auth_events (user_id, created_at desc);
create index if not exists auth_events_recent on public.auth_events (created_at desc);

-- Rate limiting --------------------------------------------------------------

-- Counters in Postgres rather than in process memory, because the moment there
-- is a second server instance an in-memory limiter stops limiting anything: an
-- attacker just retries until they land on a different instance. This is a
-- fixed-window counter keyed by bucket, which is coarse but shared.
create table if not exists public.rate_limits (
  bucket       text not null,
  window_start timestamptz not null,
  hits         integer not null default 0,
  primary key (bucket, window_start)
);

create index if not exists rate_limits_sweep on public.rate_limits (window_start);

-- Atomic increment-and-read. Doing this in one statement is what makes the
-- limiter correct under concurrency: two simultaneous login attempts cannot
-- both read 4 and both write 5.
create or replace function public.bump_rate_limit(
  p_bucket text,
  p_window_start timestamptz
)
returns integer
language plpgsql
as $$
declare
  current_hits integer;
begin
  insert into public.rate_limits (bucket, window_start, hits)
  values (p_bucket, p_window_start, 1)
  on conflict (bucket, window_start)
    do update set hits = public.rate_limits.hits + 1
  returning hits into current_hits;

  return current_hits;
end;
$$;
