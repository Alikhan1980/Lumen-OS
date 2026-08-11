"""Verification of the access tokens Supabase issues.

This is the single place where "who is this request from?" is decided, and the
answer comes from a cryptographic signature check every time. Nothing else in
the server is allowed to conclude a user id from anything else -- not a header,
not a body field, not a query parameter, and emphatically not a tool argument
the model produced. Everything downstream takes a `Principal` produced here.

Supabase signs with either:

* an asymmetric key (ES256 / RS256), published at the project's JWKS endpoint.
  This is the current default and the one to prefer -- the server needs only a
  public key, so a compromised API host cannot mint tokens; or
* the legacy shared HS256 secret.

Both are supported, asymmetric first. The JWKS is cached, and a token carrying
an unknown `kid` triggers at most one refetch per cooldown, so a burst of bogus
tokens cannot be used to hammer the auth server through us.

What is checked, all of it mandatory: signature, `exp`, `nbf`/`iat` with a small
clock skew allowance, the issuer, and the audience. Skipping the audience check
is the classic way to let a token minted for a different Supabase project (or a
different aud within one) authenticate here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt import PyJWKSet
from jwt.exceptions import InvalidTokenError

from ..settings import settings

# Supabase puts this in `aud` for a signed-in user.
EXPECTED_AUDIENCE = "authenticated"

# Tolerance for clock drift between this host and the auth server.
LEEWAY_SECONDS = 30

# How long a fetched key set is trusted before a routine refresh.
JWKS_TTL_SECONDS = 600
# Floor between refetches triggered by an unknown `kid`, so unknown-key tokens
# cannot be turned into a request amplifier against the auth server.
JWKS_MIN_REFETCH_SECONDS = 30

_ASYMMETRIC = ("ES256", "RS256", "EdDSA")


class TokenError(Exception):
    """The token is absent, malformed, expired, or not for us.

    Callers turn this into a flat 401. The reason never reaches the client:
    telling an attacker whether a token failed on signature or on expiry is free
    information about what to try next.
    """


@dataclass(frozen=True)
class Principal:
    """A verified caller. The only trustworthy statement of who is asking."""

    user_id: str
    email: str | None
    email_verified: bool
    # GoTrue's id for this login. Lets an audit row say which session did
    # something, and lets "log out everywhere" be meaningful.
    session_id: str | None
    role: str
    # The raw token, kept so the request's database transaction can present the
    # same verified claims to Postgres for RLS. Never logged, never returned.
    token: str
    expires_at: int

    @property
    def is_authenticated_role(self) -> bool:
        return self.role == EXPECTED_AUDIENCE


class _JwksCache:
    """The project's public keys, refreshed on a timer and on an unknown kid."""

    def __init__(self) -> None:
        self._keys: PyJWKSet | None = None
        self._fetched_at = 0.0
        self._last_attempt = 0.0

    async def _fetch(self) -> None:
        url = settings().jwks_url
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            self._keys = PyJWKSet.from_dict(response.json())
            self._fetched_at = time.monotonic()

    async def key_for(self, kid: str | None) -> Any:
        now = time.monotonic()
        stale = self._keys is None or (now - self._fetched_at) > JWKS_TTL_SECONDS

        if stale and (now - self._last_attempt) > JWKS_MIN_REFETCH_SECONDS:
            self._last_attempt = now
            try:
                await self._fetch()
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                # A stale-but-present key set still verifies tokens signed by a
                # key we already have, so an auth-server blip does not sign
                # everyone out. With nothing cached there is no way to proceed.
                if self._keys is None:
                    raise TokenError("key set unavailable") from exc

        if self._keys is None:
            raise TokenError("key set unavailable")

        try:
            return self._keys[kid].key if kid else self._keys.keys[0].key
        except (KeyError, IndexError, AttributeError):
            pass

        # Unknown kid: the project may have just rotated. One more fetch,
        # rate-limited by the same cooldown.
        if (now - self._last_attempt) > JWKS_MIN_REFETCH_SECONDS:
            self._last_attempt = now
            try:
                await self._fetch()
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                raise TokenError("unknown signing key") from exc
            try:
                return self._keys[kid].key if kid else self._keys.keys[0].key
            except (KeyError, IndexError, AttributeError) as exc:
                raise TokenError("unknown signing key") from exc

        raise TokenError("unknown signing key")


_jwks = _JwksCache()


def _claims_to_principal(claims: dict, token: str) -> Principal:
    user_id = claims.get("sub")
    if not user_id or not isinstance(user_id, str):
        raise TokenError("token carries no subject")

    role = claims.get("role") or ""
    if role != EXPECTED_AUDIENCE:
        # An anon or service-role token is a valid signature but not a user.
        # Letting one through here would make every `auth.uid()` policy in the
        # database resolve to null and quietly match nothing -- or, worse,
        # match a policy written with a null-tolerant comparison.
        raise TokenError("token is not a user token")

    # GoTrue reports verification through the presence of a confirmation
    # timestamp on the user record, and mirrors it into the token's metadata.
    # Treat "absent" as "not verified" rather than as "unknown".
    metadata = claims.get("user_metadata") or {}
    app_metadata = claims.get("app_metadata") or {}
    verified = bool(
        claims.get("email_verified")
        or metadata.get("email_verified")
        or app_metadata.get("email_verified")
    )

    return Principal(
        user_id=user_id,
        email=claims.get("email") or None,
        email_verified=verified,
        session_id=claims.get("session_id"),
        role=role,
        token=token,
        expires_at=int(claims.get("exp") or 0),
    )


async def verify(token: str) -> Principal:
    """Verify an access token and return who it belongs to.

    Raises TokenError for every failure mode. There is no path through this
    function that returns a Principal without a passing signature check.
    """
    if not token or not isinstance(token, str):
        raise TokenError("no token")

    config = settings()

    try:
        header = jwt.get_unverified_header(token)
    except InvalidTokenError as exc:
        raise TokenError("malformed token") from exc

    algorithm = header.get("alg")
    if algorithm not in (*_ASYMMETRIC, "HS256"):
        # Rejects `alg: none` and anything exotic. The algorithm a token claims
        # is attacker-controlled, so the allowlist has to be ours.
        raise TokenError("unsupported signing algorithm")

    if algorithm in _ASYMMETRIC:
        key: Any = await _jwks.key_for(header.get("kid"))
        algorithms = [algorithm]
    else:
        if not config.supabase_jwt_secret:
            raise TokenError("no HS256 secret configured")
        key = config.supabase_jwt_secret
        algorithms = ["HS256"]

    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=algorithms,
            audience=EXPECTED_AUDIENCE,
            issuer=config.jwt_issuer,
            leeway=LEEWAY_SECONDS,
            options={
                "require": ["exp", "sub", "aud"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": True,
                "verify_iss": True,
            },
        )
    except InvalidTokenError as exc:
        raise TokenError("token rejected") from exc

    return _claims_to_principal(claims, token)


def bearer_from_header(value: str | None) -> str:
    """Pull the token out of an Authorization header, or raise."""
    if not value:
        raise TokenError("no authorization header")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise TokenError("authorization header is not a bearer token")
    return token.strip()
