"""FastAPI dependencies: the gate every protected route goes through.

There are exactly three ways a route can be written:

    async def handler(user: CurrentUser)              # signed in
    async def handler(user: VerifiedUser)             # signed in, email verified
    async def handler(db: ServiceDb)                  # unauthenticated surface

and a route that takes none of them is public. That is a small enough
vocabulary that "is this endpoint protected?" is answerable by reading its
signature, which is the point -- an authorization scheme you have to trace
through a middleware stack to understand is one people get wrong.

`CurrentUser` yields a `Principal` produced by a signature check, plus a
database connection already scoped to that principal. Handlers never see a raw
user id from the request, and there is no dependency that produces one. If a
handler wants to know who is calling, the only answer available to it is the
verified one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

import asyncpg
from fastapi import Depends, Request

from . import db
from .errors import EmailNotVerified, Unauthorized
from .observability import logger, safe
from .security import tokens
from .security.tokens import Principal, TokenError
from .settings import settings

log = logger("auth")


async def current_principal(request: Request) -> Principal:
    """Verify the bearer token, or refuse.

    Every failure -- missing header, wrong scheme, bad signature, expired,
    wrong issuer, wrong audience -- produces the same 401 with the same
    message. The distinction is useful to an attacker and to nobody else.
    """
    try:
        raw = tokens.bearer_from_header(request.headers.get("Authorization"))
        principal = await tokens.verify(raw)
    except TokenError as exc:
        # The reason is logged (redacted) and not returned.
        log.info("rejected token %s", safe(path=request.url.path, reason=str(exc)))
        raise Unauthorized() from exc

    # Stash for the audit trail. Read-only; nothing derives authorization from
    # request.state.
    request.state.user_id = principal.user_id
    return principal


async def scoped_db(
    principal: Annotated[Principal, Depends(current_principal)],
) -> AsyncIterator[asyncpg.Connection]:
    """A connection that can only see this user's rows, for the request's life."""
    async with db.user_connection(principal) as connection:
        yield connection


class Caller:
    """What a protected handler receives: who, and a connection scoped to them.

    Bundled into one object so a handler cannot accidentally pair one user's
    principal with another user's connection -- there is no way to obtain the
    two separately.
    """

    __slots__ = ("db", "principal")

    def __init__(self, principal: Principal, connection: asyncpg.Connection):
        self.principal = principal
        self.db = connection

    @property
    def user_id(self) -> str:
        return self.principal.user_id

    @property
    def email(self) -> str | None:
        return self.principal.email

    @property
    def email_verified(self) -> bool:
        return self.principal.email_verified


async def _caller(
    principal: Annotated[Principal, Depends(current_principal)],
    connection: Annotated[asyncpg.Connection, Depends(scoped_db)],
) -> Caller:
    return Caller(principal, connection)


async def _verified_caller(
    caller: Annotated[Caller, Depends(_caller)],
) -> Caller:
    """As above, but refuses an unverified address.

    Gates the surface where an unverified account could do damage to somebody
    else: connecting integrations, running the agent, sending mail. Reading and
    editing your own profile stays open, because a user who cannot see their own
    account settings cannot fix a mistyped address.
    """
    if settings().require_verified_email and not caller.email_verified:
        raise EmailNotVerified()
    return caller


async def _service_db() -> AsyncIterator[asyncpg.Connection]:
    """A connection for the unauthenticated surface -- rate limiting, mainly.

    Takes no arguments on purpose. A plain `str` parameter on a dependency is
    read by FastAPI as a *query parameter*: an earlier version of this function
    had `reason: str = ...` for the audit log, and the effect was to publish
    `?reason=` on /login, /signup, /refresh and every other route that depends
    on it. Harmless in itself, but it advertised internals in the OpenAPI
    schema and let a caller write into a log field.
    """
    async with db.service_connection(reason="unauthenticated endpoint") as connection:
        yield connection


CurrentUser = Annotated[Caller, Depends(_caller)]
VerifiedUser = Annotated[Caller, Depends(_verified_caller)]
ServiceDb = Annotated[asyncpg.Connection, Depends(_service_db)]


def request_ip(request: Request) -> str:
    from .security.ratelimit import client_ip

    return client_ip(
        request.headers.get("X-Forwarded-For"),
        request.client.host if request.client else None,
    )


def request_agent(request: Request) -> str:
    return (request.headers.get("User-Agent") or "")[:400]
