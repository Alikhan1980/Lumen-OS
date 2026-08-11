"""User A must not reach any of User B's data. Proved against a real Postgres.

This is the only suite that needs infrastructure, and it needs it because the
guarantee under test *is* the database. A fake cannot demonstrate row-level
security; it can only demonstrate that the code meant well.

    Run it against a throwaway Postgres:

        docker run -d --name lumen-test -e POSTGRES_PASSWORD=postgres \
            -p 55432:5432 postgres:16
        set TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/postgres
        pytest tests/test_isolation.py -v

    Or against `supabase start`, which gives you the real thing including the
    auth schema:

        set TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54322/postgres

Without `TEST_DATABASE_URL` the whole module skips, so the everyday suite stays
dependency-free.

The tests connect as the `authenticated` role with a claims blob installed the
same way `server/db.py` installs it in production. That matters: this is not a
simulation of the production path, it is the production path with a different
caller.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

asyncpg = pytest.importorskip("asyncpg")

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to a scratch Postgres to run the isolation suite",
)

MIGRATIONS = sorted((Path(__file__).resolve().parent.parent / "supabase" / "migrations").glob("*.sql"))

# Stands in for Supabase's own `auth` schema when testing against a bare
# Postgres. `supabase start` provides the real one, in which case this is a
# no-op -- hence every statement being idempotent.
AUTH_SHIM = """
create schema if not exists auth;

create table if not exists auth.users (
    id                  uuid primary key default gen_random_uuid(),
    email               text unique,
    email_confirmed_at  timestamptz,
    raw_user_meta_data  jsonb not null default '{}'::jsonb,
    created_at          timestamptz not null default now()
);

create or replace function auth.uid() returns uuid
language sql stable as $$
  select nullif(
    coalesce(
      current_setting('request.jwt.claim.sub', true),
      (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
    ),
    ''
  )::uuid;
$$;

create or replace function auth.role() returns text
language sql stable as $$
  select coalesce(
    current_setting('request.jwt.claim.role', true),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role')
  );
$$;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then
    create role anon nologin noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    create role service_role nologin noinherit bypassrls;
  end if;
end;
$$;

grant usage on schema public to anon, authenticated, service_role;
grant usage on schema extensions to anon, authenticated, service_role;
"""


@pytest.fixture(scope="module")
async def pool():
    connection_pool = await asyncpg.create_pool(TEST_DATABASE_URL, min_size=1, max_size=4)
    yield connection_pool
    await connection_pool.close()


@pytest.fixture(scope="module")
async def schema(pool):
    """Build the schema from the real migration files, in order.

    Running the actual migrations rather than a test-only DDL script is the
    point: it means these tests also prove the migrations apply cleanly to an
    empty database.
    """
    async with pool.acquire() as connection:
        await connection.execute("drop schema if exists public cascade")
        await connection.execute("create schema public")
        await connection.execute("create schema if not exists extensions")
        await connection.execute(AUTH_SHIM)
        for path in MIGRATIONS:
            await connection.execute(path.read_text(encoding="utf-8"))
    return True


@pytest.fixture
async def users(pool, schema):
    """Two users with a row in every user-owned table."""
    created = {}
    async with pool.acquire() as connection:
        for label in ("a", "b"):
            email = f"{label}-{uuid.uuid4().hex[:8]}@example.com"
            user_id = await connection.fetchval(
                """
                insert into auth.users (email, email_confirmed_at, raw_user_meta_data)
                values ($1, now(), $2::jsonb)
                returning id
                """,
                email,
                json.dumps({"display_name": f"User {label.upper()}"}),
            )
            conversation_id = await connection.fetchval(
                "insert into public.conversations (user_id, title) values ($1, $2) returning id",
                user_id,
                f"{label} conversation",
            )
            await connection.execute(
                """
                insert into public.messages (conversation_id, user_id, role, content)
                values ($1, $2, 'user', $3)
                """,
                conversation_id,
                user_id,
                f"{label} secret message",
            )
            await connection.execute(
                "insert into public.tasks (user_id, title) values ($1, $2)",
                user_id,
                f"{label} task",
            )
            reminder_id = await connection.fetchval(
                """
                insert into public.reminders (user_id, title, due_utc, due_local)
                values ($1, $2, now(), '17:00')
                returning id
                """,
                user_id,
                f"{label} reminder",
            )
            await connection.execute(
                """
                insert into public.reminder_log (reminder_id, user_id, kind)
                values ($1, $2, 'created')
                """,
                reminder_id,
                user_id,
            )
            connection_row = await connection.fetchval(
                """
                insert into public.integration_connections
                    (user_id, provider, account_email, scopes)
                values ($1, 'google', $2, array['https://www.googleapis.com/auth/gmail.modify'])
                returning id
                """,
                user_id,
                f"{label}-google@example.com",
            )
            await connection.execute(
                """
                insert into public.integration_secrets
                    (connection_id, user_id, refresh_token_enc)
                values ($1, $2, $3)
                """,
                connection_row,
                user_id,
                f"v1.fake.{label}-refresh",
            )
            created[label] = {
                "id": user_id,
                "email": email,
                "conversation_id": conversation_id,
                "reminder_id": reminder_id,
                "connection_id": connection_row,
            }

    yield created

    async with pool.acquire() as connection:
        for record in created.values():
            await connection.execute("delete from auth.users where id = $1", record["id"])


class ScopedSession:
    """A connection acting as one user, exactly as server/db.py sets one up."""

    def __init__(self, connection, user_id: str, email: str):
        self.connection = connection
        self.user_id = user_id
        self.email = email

    async def __aenter__(self):
        self.transaction = self.connection.transaction()
        await self.transaction.start()
        await self.connection.execute(
            """
            select set_config('role', 'authenticated', true),
                   set_config('request.jwt.claims', $1, true)
            """,
            json.dumps(
                {
                    "sub": str(self.user_id),
                    "role": "authenticated",
                    "aud": "authenticated",
                    "email": self.email,
                }
            ),
        )
        return self.connection

    async def __aexit__(self, *exc):
        await self.transaction.rollback()


def as_user(pool_connection, record):
    return ScopedSession(pool_connection, record["id"], record["email"])


# ------------------------------------------------------------------- the tests


USER_TABLES = [
    "conversations",
    "messages",
    "tasks",
    "reminders",
    "reminder_log",
    "integration_connections",
]


async def test_migrations_apply_cleanly(schema):
    assert schema is True


async def test_a_sees_only_its_own_rows(pool, users):
    """The headline claim, over every user-owned table at once."""
    async with pool.acquire() as raw, as_user(raw, users["a"]) as connection:
        for table in USER_TABLES:
            rows = await connection.fetch(f"select user_id from public.{table}")
            assert rows, f"{table} returned nothing; the fixture is wrong"
            owners = {str(row["user_id"]) for row in rows}
            assert owners == {str(users["a"]["id"])}, f"{table} leaked another user's rows"


async def test_b_sees_only_its_own_rows(pool, users):
    async with pool.acquire() as raw, as_user(raw, users["b"]) as connection:
        for table in USER_TABLES:
            rows = await connection.fetch(f"select user_id from public.{table}")
            owners = {str(row["user_id"]) for row in rows}
            assert owners == {str(users["b"]["id"])}


async def test_naming_another_users_row_id_returns_nothing(pool, users):
    """A guessed primary key is not a way in.

    This is the case an application-layer check catches only if somebody
    remembered to write it. Here the row is not in the caller's universe at all,
    so the query returns zero rows without anybody having written a check.
    """
    async with pool.acquire() as raw, as_user(raw, users["a"]) as connection:
        row = await connection.fetchrow(
            "select * from public.conversations where id = $1",
            users["b"]["conversation_id"],
        )
        assert row is None


async def test_cannot_read_another_users_messages(pool, users):
    async with pool.acquire() as raw, as_user(raw, users["a"]) as connection:
        rows = await connection.fetch(
            "select content from public.messages where conversation_id = $1",
            users["b"]["conversation_id"],
        )
        assert rows == []


async def test_cannot_update_another_users_row(pool, users):
    async with pool.acquire() as raw, as_user(raw, users["a"]) as connection:
        result = await connection.execute(
            "update public.tasks set title = 'hijacked' where user_id = $1",
            users["b"]["id"],
        )
        assert result.endswith(" 0")


async def test_cannot_delete_another_users_row(pool, users):
    async with pool.acquire() as raw, as_user(raw, users["a"]) as connection:
        result = await connection.execute(
            "delete from public.reminders where user_id = $1", users["b"]["id"]
        )
        assert result.endswith(" 0")


async def test_cannot_insert_a_row_owned_by_someone_else(pool, users):
    """The WITH CHECK half of the policy. Writing *into* another account is as
    much a breach as reading out of one."""
    async with pool.acquire() as raw, as_user(raw, users["a"]) as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(
                "insert into public.tasks (user_id, title) values ($1, 'planted')",
                users["b"]["id"],
            )


async def test_cannot_give_away_a_row_by_changing_its_owner(pool, users):
    """USING alone would allow this: the row is mine to update, and I set the
    owner to someone else. WITH CHECK is what refuses it."""
    async with pool.acquire() as raw, as_user(raw, users["a"]) as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(
                "update public.tasks set user_id = $1 where user_id = $2",
                users["b"]["id"],
                users["a"]["id"],
            )


async def test_cannot_file_a_message_into_another_users_conversation(pool, users):
    """Own the message, borrow the thread. The trigger is what stops it.

    RLS alone would allow this -- the row's `user_id` is the caller's, so the
    policy passes -- which is exactly why enforce_message_owner() exists.
    """
    async with pool.acquire() as raw, as_user(raw, users["a"]) as connection:
        with pytest.raises(asyncpg.PostgresError):
            await connection.execute(
                """
                    insert into public.messages (conversation_id, user_id, role, content)
                    values ($1, $2, 'user', 'planted')
                    """,
                users["b"]["conversation_id"],
                users["a"]["id"],
            )


async def test_oauth_secrets_are_unreachable_by_any_user(pool, users):
    """Not "the API strips this field" -- the table is not readable at all.

    A user cannot read their own refresh token, which means no bug in a route,
    a serialiser or an export can ever return one.
    """
    for label in ("a", "b"):
        async with pool.acquire() as raw, as_user(raw, users[label]) as connection:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.fetch("select * from public.integration_secrets")


async def test_oauth_state_table_is_unreachable(pool, users):
    async with pool.acquire() as raw, as_user(raw, users["a"]) as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetch("select * from public.oauth_states")


async def test_rate_limit_table_is_unreachable(pool, users):
    """Readable counters would let an attacker watch their own budget."""
    async with pool.acquire() as raw, as_user(raw, users["a"]) as connection:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetch("select * from public.rate_limits")


async def test_audit_log_is_readable_but_not_writable(pool, users):
    """A user can review their own security history and cannot edit it."""
    async with pool.acquire() as raw:
        await raw.execute(
            "insert into public.auth_events (user_id, event) values ($1, 'login.success')",
            users["a"]["id"],
        )
        async with as_user(raw, users["a"]) as connection:
            rows = await connection.fetch("select event from public.auth_events")
            assert [row["event"] for row in rows] == ["login.success"]

            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(
                    "insert into public.auth_events (user_id, event) values ($1, 'forged')",
                    users["a"]["id"],
                )
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute("delete from public.auth_events")

    async with pool.acquire() as raw:
        await raw.execute("delete from public.auth_events where user_id = $1", users["a"]["id"])


async def test_no_table_is_left_unprotected(pool, schema):
    """The tripwire from 0004_rls.sql: every table has RLS, forced, with policies.

    Catches the realistic future mistake -- somebody adds a table in a new
    migration and forgets the policy, making it readable by every signed-in
    user.
    """
    async with pool.acquire() as connection:
        gaps = await connection.fetch("select * from public.rls_gaps()")
    assert gaps == [], f"tables without proper RLS: {[dict(g) for g in gaps]}"


async def test_anonymous_role_sees_nothing(pool, users):
    """Signed out means no data, not less data."""
    async with pool.acquire() as raw:
        transaction = raw.transaction()
        await transaction.start()
        try:
            await raw.execute("select set_config('role', 'anon', true)")
            for table in USER_TABLES:
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await raw.fetch(f"select * from public.{table}")
        finally:
            await transaction.rollback()


async def test_deleting_a_user_removes_everything_they_owned(pool, users):
    """Account deletion is a cascade, and the cascade has to actually reach."""
    victim = users["a"]["id"]
    async with pool.acquire() as connection:
        await connection.execute("delete from auth.users where id = $1", victim)

        for table in [*USER_TABLES, "profiles", "user_preferences", "integration_secrets"]:
            column = "id" if table == "profiles" else "user_id"
            remaining = await connection.fetchval(
                f"select count(*) from public.{table} where {column} = $1", victim
            )
            assert remaining == 0, f"{table} still holds rows for the deleted user"

        # The other user is untouched.
        survivor = await connection.fetchval(
            "select count(*) from public.tasks where user_id = $1", users["b"]["id"]
        )
        assert survivor == 1
