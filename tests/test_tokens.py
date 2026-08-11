"""Token verification: the list of things that must be rejected.

Everything downstream trusts `verify()` completely -- a Principal it returns
becomes a Postgres identity and decides whose Google account a tool reaches. So
these tests are less about the happy path than about the ways a forged or
borrowed token could be made to look valid.
"""

from __future__ import annotations

import time

import jwt
import pytest

from server.security.tokens import TokenError, bearer_from_header, verify
from server.settings import settings

from .conftest import make_token

pytestmark = pytest.mark.asyncio


async def test_accepts_a_valid_token():
    principal = await verify(make_token("user-1", email="a@example.com"))
    assert principal.user_id == "user-1"
    assert principal.email == "a@example.com"
    assert principal.email_verified is True
    assert principal.is_authenticated_role


async def test_rejects_expired():
    with pytest.raises(TokenError):
        await verify(make_token("user-1", expires_in=-120))


async def test_rejects_wrong_issuer():
    """A token from another Supabase project must not authenticate here."""
    with pytest.raises(TokenError):
        await verify(make_token("user-1", issuer="https://someone-else.supabase.co/auth/v1"))


async def test_rejects_wrong_audience():
    with pytest.raises(TokenError):
        await verify(make_token("user-1", audience="some-other-service"))


async def test_rejects_wrong_signature():
    """The classic: a token whose claims are right and whose key is not."""
    with pytest.raises(TokenError):
        await verify(make_token("user-1", secret="not-the-real-secret"))


async def test_rejects_alg_none():
    """`alg: none` is a signature-free token. It must never be accepted."""
    forged = jwt.encode(
        {
            "sub": "user-1",
            "aud": "authenticated",
            "iss": settings().jwt_issuer,
            "role": "authenticated",
            "exp": int(time.time()) + 3600,
        },
        key="",
        algorithm="none",
    )
    with pytest.raises(TokenError):
        await verify(forged)


async def test_rejects_service_role_token():
    """A service-role key is a valid signature but is not a user.

    Letting one through would give the holder a Postgres session whose
    `auth.uid()` is null, matching nothing -- or, with a differently written
    policy, matching everything.
    """
    with pytest.raises(TokenError):
        await verify(make_token("user-1", role="service_role"))


async def test_rejects_anon_token():
    with pytest.raises(TokenError):
        await verify(make_token("user-1", role="anon"))


async def test_rejects_token_without_subject():
    now = int(time.time())
    token = jwt.encode(
        {
            "aud": "authenticated",
            "iss": settings().jwt_issuer,
            "role": "authenticated",
            "exp": now + 3600,
        },
        settings().supabase_jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(TokenError):
        await verify(token)


async def test_rejects_garbage():
    for candidate in ("", "not-a-token", "a.b.c", "Bearer x"):
        with pytest.raises(TokenError):
            await verify(candidate)


async def test_unverified_email_is_reported_not_assumed():
    principal = await verify(make_token("user-1", verified=False))
    assert principal.email_verified is False


async def test_bearer_parsing():
    assert bearer_from_header("Bearer abc") == "abc"
    assert bearer_from_header("bearer abc") == "abc"
    for bad in (None, "", "abc", "Basic abc", "Bearer "):
        with pytest.raises(TokenError):
            bearer_from_header(bad)
