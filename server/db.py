"""Database access, in two flavours -- and the difference between them matters.

`user_connection(principal)` is what routes use. It opens a transaction, drops
the session to the `authenticated` Postgres role, and installs the caller's
*verified* JWT claims as `request.jwt.claims`. From that point on every
statement is filtered by the RLS policies in 0004_rls.sql, which resolve
`auth.uid()` from exactly those claims. A route that forgets `where user_id = $1`
returns the caller's own rows anyway, because the database will not show it
anything else. That is the property the whole design rests on: application code
is no longer the thing standing between one user and another user's data.

`service_connection()` bypasses all of it. It runs as the owning role, which has
BYPASSRLS, and can read every row belonging to everybody. It exists because
three jobs genuinely cannot be done as the user:

  * rate limiting, which has to work before anyone is authenticated;
  * OAuth token custody, which is stored in a table the user's own role is
    granted nothing on, on purpose;
  * account deletion, which has to reach `auth.users`.

Every use is logged. If you are adding a fourth caller, that is the moment to
check whether the work really cannot be done through `user_connection`.

The claims installed are rebuilt field by field from a `Principal` that came out
of a signature check -- never forwarded wholesale from the request. There is no
code path that puts a client-supplied string into `request.jwt.claims`.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from .errors import UpstreamUnavailable
from .observability import logger, safe
from .security.tokens import Principal
from .settings import settings

log = logger("db")

# The role RLS policies are written against. Matches the `to authenticated`
# clause on every policy in 0004_rls.sql.
USER_ROLE = "authenticated"

_pool: asyncpg.Pool | None = None


async def connect() -> asyncpg.Pool:
    """Open the pool. Called once, from the application's lifespan."""
    global _pool  # noqa: PLW0603 - one pool per process, opened once at startup
    if _pool is not None:
        return _pool

    config = settings()
    _pool = await asyncpg.create_pool(
        dsn=config.database_url,
        min_size=config.db_pool_min,
        max_size=config.db_pool_max,
        # A pooled connection that has been recycled cannot carry a stale
        # `SET` from a previous request -- but statement caching across a
        # `SET ROLE` boundary has bitten people before, and the cost of not
        # caching prepared statements here is small next to the cost of being
        # wrong about it.
        statement_cache_size=0,
        command_timeout=config.db_statement_timeout_ms / 1000,
        server_settings={
            "application_name": "lumen-api",
            "statement_timeout": str(config.db_statement_timeout_ms),
            # A transaction left open by a crashed request must not hold locks
            # forever; this bounds it.
            "idle_in_transaction_session_timeout": "30000",
        },
    )
    log.info("database pool open %s", safe(count=config.db_pool_max))
    return _pool


async def disconnect() -> None:
    global _pool  # noqa: PLW0603 - see connect()
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise UpstreamUnavailable()
    return _pool


def _claims_for(principal: Principal) -> str:
    """The claims Postgres will see, rebuilt from verified fields only.

    Not the raw token payload. Anything the token happened to carry that we did
    not verify and do not need is dropped here rather than handed to a policy
    that might one day read it.
    """
    return json.dumps(
        {
            "sub": principal.user_id,
            "role": USER_ROLE,
            "aud": USER_ROLE,
            "email": principal.email or "",
            "session_id": principal.session_id or "",
            "exp": principal.expires_at,
        },
        separators=(",", ":"),
    )


@asynccontextmanager
async def user_connection(principal: Principal):
    """A connection that can only see `principal`'s rows.

    Everything runs in one transaction: `SET LOCAL` is transaction-scoped, so
    the role and the claims are guaranteed to be gone when the connection goes
    back to the pool, whether the request succeeded, failed, or was cancelled.
    A `SET` that leaked across requests would be the worst possible bug in this
    file -- the next request on that connection would run as the previous user.
    """
    if not principal.is_authenticated_role:
        # Defence in depth: verify() already refuses non-user tokens.
        raise UpstreamUnavailable()

    async with pool().acquire() as connection:
        transaction = connection.transaction()
        await transaction.start()
        try:
            # set_config with is_local=true is the parameterisable form of
            # SET LOCAL. It has to be: a claims blob interpolated into SQL text
            # would be an injection point, and this one carries an email
            # address the user chose.
            await connection.execute(
                """
                select
                  set_config('role', $1, true),
                  set_config('request.jwt.claims', $2, true)
                """,
                USER_ROLE,
                _claims_for(principal),
            )
            yield connection
        except Exception:
            await transaction.rollback()
            raise
        else:
            await transaction.commit()


@asynccontextmanager
async def service_connection(*, reason: str):
    """A connection that bypasses RLS. Four callers, all listed in the docstring.

    `reason` is required and is logged. It is not decoration: a grep for
    `service_connection(` is the audit of everything in this codebase that can
    read across users, and a reason string makes that audit readable.
    """
    log.debug("service connection %s", safe(reason=reason))
    async with pool().acquire() as connection:
        yield connection


@asynccontextmanager
async def service_transaction(*, reason: str):
    """`service_connection` wrapped in a transaction, for multi-statement work."""
    async with service_connection(reason=reason) as connection, connection.transaction():
        yield connection


# ------------------------------------------------------------------ helpers


def row_to_dict(row: asyncpg.Record | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rows_to_list(rows) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


async def healthy() -> bool:
    """Cheap liveness probe for /healthz. Never raises."""
    try:
        async with service_connection(reason="health check") as connection:
            return await connection.fetchval("select 1") == 1
    except Exception:
        return False
