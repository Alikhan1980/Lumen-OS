-- Row-level security. This file is the actual isolation guarantee; everything
-- else is convenience on top of it.
--
-- The shape is default-deny:
--
--   1. Revoke every table privilege from `anon` and `authenticated`. Supabase
--      grants those roles broad access to `public` when a project is created,
--      so a new table is reachable until someone says otherwise. Revoking first
--      means a table added later and forgotten is invisible, not public.
--   2. Enable RLS on every table, and FORCE it, so even a table owner is
--      subject to its own policies.
--   3. Grant back exactly the verbs each table needs, then write the policy.
--
-- `auth.uid()` is Supabase's reader for the `sub` claim of the verified JWT the
-- connection is carrying. The backend sets that claim per transaction from a
-- token it has cryptographically verified itself (server/db.py). It is never
-- read from a request body, a header the client controls, or anything the model
-- can produce -- which is the requirement that "the AI cannot specify another
-- user's ID" reduces to.

-- 1. Default deny ------------------------------------------------------------

revoke all on all tables in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
revoke all on all functions in schema public from anon, authenticated;

-- And for anything added after this migration runs.
alter default privileges in schema public
  revoke all on tables from anon, authenticated;
alter default privileges in schema public
  revoke all on sequences from anon, authenticated;

-- 2. RLS on, forced, everywhere ----------------------------------------------

do $$
declare
  t record;
begin
  for t in
    select tablename
      from pg_tables
     where schemaname = 'public'
  loop
    execute format('alter table public.%I enable row level security', t.tablename);
    execute format('alter table public.%I force row level security', t.tablename);
  end loop;
end;
$$;

-- 3. Per-table grants and policies -------------------------------------------

-- profiles: a user reads and edits their own. No insert (the provisioning
-- trigger owns that) and no delete (account deletion cascades from auth.users,
-- so a client-side delete would only ever half-remove someone).
grant select, update on public.profiles to authenticated;

create policy profiles_select_own on public.profiles
  for select to authenticated
  using (id = (select auth.uid()));

create policy profiles_update_own on public.profiles
  for update to authenticated
  using (id = (select auth.uid()))
  with check (id = (select auth.uid()));

-- user_preferences: same, plus insert so a user provisioned before this table
-- existed can still get a row.
grant select, insert, update on public.user_preferences to authenticated;

create policy user_preferences_select_own on public.user_preferences
  for select to authenticated
  using (user_id = (select auth.uid()));

create policy user_preferences_insert_own on public.user_preferences
  for insert to authenticated
  with check (user_id = (select auth.uid()));

create policy user_preferences_update_own on public.user_preferences
  for update to authenticated
  using (user_id = (select auth.uid()))
  with check (user_id = (select auth.uid()));

-- conversations / messages / tasks / reminders / reminder_log: the user's own
-- content, full CRUD, scoped every time.
--
-- Both USING and WITH CHECK are spelled out on every write policy. USING picks
-- which existing rows the statement may touch; WITH CHECK validates the row it
-- would leave behind. With only USING, a user could update one of their own
-- rows and set user_id to somebody else -- handing it over rather than reading
-- it, but a breach of isolation either way.
do $$
declare
  t text;
begin
  foreach t in array array['conversations', 'messages', 'tasks', 'reminders', 'reminder_log']
  loop
    execute format('grant select, insert, update, delete on public.%I to authenticated', t);

    execute format($p$
      create policy %1$s_select_own on public.%1$I
        for select to authenticated
        using (user_id = (select auth.uid()))
    $p$, t);

    execute format($p$
      create policy %1$s_insert_own on public.%1$I
        for insert to authenticated
        with check (user_id = (select auth.uid()))
    $p$, t);

    execute format($p$
      create policy %1$s_update_own on public.%1$I
        for update to authenticated
        using (user_id = (select auth.uid()))
        with check (user_id = (select auth.uid()))
    $p$, t);

    execute format($p$
      create policy %1$s_delete_own on public.%1$I
        for delete to authenticated
        using (user_id = (select auth.uid()))
    $p$, t);
  end loop;
end;
$$;

-- Identity columns (messages.id, reminder_log.id) do not need a separate
-- sequence grant -- Postgres treats the sequence as owned by the column. This
-- is here for the ordinary `serial`-style sequence a later migration might add
-- without noticing the blanket revoke above.
grant usage, select on all sequences in schema public to authenticated;

-- integration_connections: readable by its owner, because the Connected
-- Accounts screen is built from it. Writes are server-only and deliberately
-- have no policy: disconnecting has to revoke the grant at Google and shred the
-- stored token, and a client that could just flip `revoked_at` would leave a
-- live credential sitting in the database looking revoked.
grant select on public.integration_connections to authenticated;

create policy integration_connections_select_own on public.integration_connections
  for select to authenticated
  using (user_id = (select auth.uid()));

-- integration_secrets: no grant, no policy, for anybody but the service role.
-- A user cannot read their own refresh token. Neither can a bug in a route that
-- forgot to strip a field, because the row is not in that connection's
-- universe at all.
revoke all on public.integration_secrets from anon, authenticated;

-- oauth_states and rate_limits: server bookkeeping. Same treatment.
revoke all on public.oauth_states from anon, authenticated;
revoke all on public.rate_limits from anon, authenticated;

-- auth_events: a user can read their own security history ("new sign-in from
-- ...", "password changed"). Nobody writes through this connection; the server
-- records events on the service connection so a client cannot forge an entry
-- or erase one.
grant select on public.auth_events to authenticated;

create policy auth_events_select_own on public.auth_events
  for select to authenticated
  using (user_id = (select auth.uid()));

-- 4. A tripwire for the next table -------------------------------------------

-- Called by the test suite and safe to call in a deploy check. Returns the
-- tables that are readable by `authenticated` but have no policy constraining
-- which rows -- i.e. tables that are accidentally world-readable to any signed
-- in user. Should always return zero rows.
create or replace function public.rls_gaps()
returns table (table_name text, problem text)
language sql
stable
as $$
  select c.relname::text,
         case
           when not c.relrowsecurity then 'RLS not enabled'
           when not c.relforcerowsecurity then 'RLS not forced'
           else 'granted to authenticated with no policy'
         end
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
   where n.nspname = 'public'
     and c.relkind = 'r'
     and (
       not c.relrowsecurity
       or not c.relforcerowsecurity
       or (
         has_table_privilege('authenticated', c.oid, 'SELECT')
         and not exists (
           select 1 from pg_policy p where p.polrelid = c.oid
         )
       )
     );
$$;
