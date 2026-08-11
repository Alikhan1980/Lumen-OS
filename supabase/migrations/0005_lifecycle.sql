-- Account deletion and housekeeping.
--
-- Deletion is a cascade from auth.users: every user-owned table in 0001-0003
-- references it with `on delete cascade`, so removing the auth user removes the
-- lot in one transaction. That is deliberate -- a hand-written "delete from
-- each table" list is a list somebody forgets to extend when they add a table,
-- and the failure mode is orphaned personal data that the user was told had
-- been erased.
--
-- What the cascade cannot do is revoke an OAuth grant at Google, which is a
-- network call. So the server does this in order:
--
--   1. read the live connections (service connection),
--   2. revoke each one at its provider,
--   3. delete the auth user, and let the cascade run.
--
-- Step 2 failing does not block step 3: the user asked to be deleted, and
-- leaving their account alive because Google was down would be the wrong call.
-- The failure is recorded in auth_events so it can be retried by hand.

-- What to revoke before deleting an account. Returns connection ids only; the
-- secrets are fetched separately by the one module allowed to decrypt them.
create or replace function public.connections_to_revoke(p_user_id uuid)
returns table (connection_id uuid, provider text)
language sql
stable
security definer
set search_path = public
as $$
  select id, provider
    from public.integration_connections
   where user_id = p_user_id
     and revoked_at is null;
$$;

revoke all on function public.connections_to_revoke(uuid) from anon, authenticated;

-- Housekeeping ---------------------------------------------------------------

-- Expired handshakes and stale rate-limit windows. Both tables are pure churn:
-- nothing reads a consumed oauth_state or a closed rate-limit window, and both
-- would otherwise grow forever.
--
-- Schedule with pg_cron once the project is on a plan that has it:
--     select cron.schedule('lumen-sweep', '*/15 * * * *', 'select public.sweep_expired()');
create or replace function public.sweep_expired()
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  delete from public.oauth_states
   where expires_at < now() - interval '1 hour';

  delete from public.rate_limits
   where window_start < now() - interval '1 day';
end;
$$;

revoke all on function public.sweep_expired() from anon, authenticated;

-- Audit events age out too. Kept longer than the churn tables because their
-- whole purpose is answering a question weeks after the fact, but not kept
-- forever: an unbounded log of who signed in from roughly where is a liability,
-- not an asset.
create or replace function public.prune_auth_events(p_keep_days integer default 90)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  removed integer;
begin
  delete from public.auth_events
   where created_at < now() - make_interval(days => p_keep_days);
  get diagnostics removed = row_count;
  return removed;
end;
$$;

revoke all on function public.prune_auth_events(integer) from anon, authenticated;
