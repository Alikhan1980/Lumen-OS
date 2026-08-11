"""Custody of the user's Google OAuth credentials.

The rule this module exists to keep: **a refresh token never leaves the server.**
Not in a response body, not in a log line, not in an error, not to the desktop
client, not to the model. It goes from Google's token endpoint into
`integration_secrets` encrypted, and comes back out only inside this process to
mint a short-lived access token for a tool call the authenticated owner asked
for.

The flow, and why each piece is there:

* **PKCE plus a server-side `state`.** The state is stored as a SHA-256 hash,
  bound to one user, single-use, and expiring. Without it, an attacker can send
  a victim a crafted callback URL and attach the attacker's Google account to
  the victim's session -- after which the victim's agent reads the attacker's
  mailbox, and anything the victim files goes into the attacker's Drive.
* **`access_type=offline` with `prompt=consent`.** Google only returns a refresh
  token on the first consent unless consent is re-requested; without this a
  reconnect yields an access token that expires in an hour and no way to renew.
* **Revocation on disconnect.** Deleting our copy is not disconnecting. The
  grant lives in the user's Google account until it is revoked at Google's
  endpoint, and until then anybody holding a leaked copy of the token can still
  use it.

Every function here takes a `user_id` that came from a verified JWT. None of
them accept a connection id without also checking it belongs to that user.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

from .. import db
from ..errors import AppError, NotFound, UpstreamUnavailable
from ..observability import logger, safe
from ..security import crypto
from ..settings import settings
from . import permissions

log = logger("google-oauth")

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 -- a URL, not a secret
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"

# How long a half-finished connect may sit before the state is useless.
STATE_TTL = timedelta(minutes=10)
# Renew a little before expiry rather than on it, so a tool call that takes a
# few seconds does not start with a valid token and finish with a dead one.
REFRESH_MARGIN = timedelta(minutes=5)

TIMEOUT = httpx.Timeout(15.0, connect=5.0)


def _context(connection_id: str, field: str) -> str:
    """Additional authenticated data binding a ciphertext to its row and column.

    A refresh token lifted out of one row cannot be pasted into another and
    decrypted: the AAD would not match and AES-GCM would reject it.
    """
    return f"integration_secret:{connection_id}:{field}"


def _hash_state(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LiveCredentials:
    """A usable access token, plus what it is allowed to do.

    Deliberately does not carry the refresh token. Nothing outside this module
    has a reason to hold one, so nothing outside this module is given the
    chance.
    """

    access_token: str
    scopes: tuple[str, ...]
    account_email: str | None
    connection_id: str

    @property
    def allowed_tools(self) -> set[str]:
        return permissions.tools_for_scopes(self.scopes)


# --------------------------------------------------------------------- connect


async def start_connect(
    *, user_id: str, capabilities: list[str] | None = None
) -> str:
    """Begin a connect and return the URL to send the user's browser to."""
    config = settings()
    if not config.google_client_id or not config.google_client_secret:
        raise AppError(
            "Google integrations are not configured on this server.",
            code="integration_unavailable",
            status_code=503,
        )

    wanted = list(capabilities or permissions.DEFAULT_CAPABILITIES)
    scopes = permissions.scopes_for(wanted)

    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )

    async with db.service_connection(reason="oauth: store handshake state") as connection:
        await connection.execute(
            """
            insert into public.oauth_states
                (state_hash, user_id, provider, code_verifier_enc, requested_scopes, expires_at)
            values ($1, $2, 'google', $3, $4, $5)
            """,
            _hash_state(state),
            user_id,
            # The verifier is a secret for the length of the handshake; treat it
            # like one. `state` is the AAD, so a verifier row cannot be reused
            # under a different state.
            crypto.encrypt(verifier, context=f"oauth_state:{_hash_state(state)}"),
            scopes,
            datetime.now(UTC) + STATE_TTL,
        )

    query = urlencode(
        {
            "client_id": config.google_client_id,
            "redirect_uri": config.google_redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            # Both are needed for a refresh token: offline asks for one,
            # consent forces Google to re-issue it on a reconnect instead of
            # assuming we still have the original.
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
    )
    log.info("connect started %s", safe(user_id=user_id, provider="google", scopes=len(scopes)))
    return f"{AUTH_ENDPOINT}?{query}"


async def complete_connect(*, state: str, code: str) -> dict:
    """Finish a connect. Returns a summary safe to show the user.

    The user id comes from the stored state row, never from the callback
    request -- the browser arriving here is coming from Google and carries no
    session of ours worth trusting.
    """
    state_hash = _hash_state(state)

    async with db.service_transaction(reason="oauth: consume handshake state") as connection:
        row = await connection.fetchrow(
            """
            update public.oauth_states
               set consumed_at = now()
             where state_hash = $1
               and consumed_at is null
               and expires_at > now()
            returning user_id, code_verifier_enc, requested_scopes
            """,
            state_hash,
        )

    if row is None:
        # Expired, already used, or never issued. All three are the same answer.
        raise AppError(
            "That connection link has expired. Start again from Connected Accounts.",
            code="oauth_state_invalid",
            status_code=400,
        )

    user_id = str(row["user_id"])
    verifier = crypto.decrypt(row["code_verifier_enc"], context=f"oauth_state:{state_hash}")

    tokens = await _exchange_code(code=code, verifier=verifier)
    granted = tuple((tokens.get("scope") or "").split())
    refresh_token = tokens.get("refresh_token") or ""
    access_token = tokens.get("access_token") or ""

    if not access_token:
        raise UpstreamUnavailable()

    profile = await _fetch_userinfo(access_token)

    connection_id = await _store_connection(
        user_id=user_id,
        account_email=profile.get("email"),
        account_id=profile.get("sub"),
        scopes=list(granted),
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=int(tokens.get("expires_in") or 3600),
    )

    log.info(
        "connected %s",
        safe(user_id=user_id, provider="google", connection_id=connection_id, scopes=len(granted)),
    )
    return {
        "provider": "google",
        "account_email": profile.get("email"),
        "capabilities": permissions.capabilities_from_scopes(list(granted)),
    }


async def _exchange_code(*, code: str, verifier: str) -> dict:
    config = settings()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(
                TOKEN_ENDPOINT,
                data={
                    "code": code,
                    "client_id": config.google_client_id,
                    "client_secret": config.google_client_secret,
                    "redirect_uri": config.google_redirect_uri,
                    "grant_type": "authorization_code",
                    "code_verifier": verifier,
                },
            )
    except httpx.HTTPError as exc:
        raise UpstreamUnavailable() from exc

    if response.status_code >= 400:
        # Google's body echoes back parts of the request. Log the status only.
        log.warning("token exchange refused %s", safe(status=response.status_code))
        raise AppError(
            "Google would not complete the connection. Please try again.",
            code="oauth_exchange_failed",
            status_code=400,
        )
    return response.json()


async def _fetch_userinfo(access_token: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(
                USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"}
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError:
        # Not fatal: the connection works, we just cannot label it with an
        # address. Better than failing a connect the user completed.
        log.info("userinfo unavailable %s", safe(provider="google"))
        return {}


async def _store_connection(
    *,
    user_id: str,
    account_email: str | None,
    account_id: str | None,
    scopes: list[str],
    access_token: str,
    refresh_token: str,
    expires_in: int,
) -> str:
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)

    async with db.service_transaction(reason="oauth: store credentials") as connection:
        # A reconnect replaces the live row rather than adding a second one.
        # The partial unique index on (user_id, provider) where revoked_at is
        # null is what makes this an upsert.
        existing = await connection.fetchval(
            """
            select id from public.integration_connections
             where user_id = $1 and provider = 'google' and revoked_at is null
            """,
            user_id,
        )

        if existing:
            connection_id = str(existing)
            await connection.execute(
                """
                update public.integration_connections
                   set account_email = $2,
                       account_id = coalesce($3, account_id),
                       scopes = $4,
                       status = 'connected',
                       needs_reauth_at = null,
                       connected_at = now()
                 where id = $1
                """,
                existing,
                account_email,
                account_id,
                scopes,
            )
        else:
            connection_id = str(
                await connection.fetchval(
                    """
                    insert into public.integration_connections
                        (user_id, provider, account_email, account_id, scopes)
                    values ($1, 'google', $2, $3, $4)
                    returning id
                    """,
                    user_id,
                    account_email,
                    account_id,
                    scopes,
                )
            )

        # Google omits the refresh token when it believes we already hold one.
        # Keep the stored one in that case rather than writing a null over a
        # working credential -- that mistake turns a reconnect into a silent
        # downgrade to one hour of access.
        refresh_enc = (
            crypto.encrypt(refresh_token, context=_context(connection_id, "refresh_token"))
            if refresh_token
            else None
        )

        await connection.execute(
            """
            insert into public.integration_secrets
                (connection_id, user_id, access_token_enc, refresh_token_enc,
                 key_version, access_expires_at)
            values ($1, $2, $3, $4, $5, $6)
            on conflict (connection_id) do update
               set access_token_enc = excluded.access_token_enc,
                   refresh_token_enc = coalesce(
                       excluded.refresh_token_enc,
                       public.integration_secrets.refresh_token_enc
                   ),
                   key_version = excluded.key_version,
                   access_expires_at = excluded.access_expires_at
            """,
            connection_id,
            user_id,
            crypto.encrypt(access_token, context=_context(connection_id, "access_token")),
            refresh_enc,
            crypto.keyring().current_version,
            expires_at,
        )

    return connection_id


# ------------------------------------------------------------------- using it


async def credentials_for(*, user_id: str, provider: str = "google") -> LiveCredentials:
    """A live access token for this user, refreshing it if it has aged out.

    The only way any tool obtains Google access. It takes a user id that came
    from a verified token and reads only that user's row, so there is no
    argument a caller could pass -- or a model could hallucinate -- that reaches
    somebody else's credential.
    """
    async with db.service_connection(reason="oauth: read credentials for tool call") as connection:
        row = await connection.fetchrow(
            """
            select c.id, c.scopes, c.account_email, c.status,
                   s.access_token_enc, s.refresh_token_enc, s.access_expires_at
              from public.integration_connections c
              join public.integration_secrets s on s.connection_id = c.id
             where c.user_id = $1
               and c.provider = $2
               and c.revoked_at is null
            """,
            user_id,
            provider,
        )

    if row is None:
        raise NotFound(f"No {provider.title()} account is connected.")
    if row["status"] == "needs_reauth":
        raise AppError(
            f"Your {provider.title()} connection needs to be renewed.",
            code="integration_needs_reauth",
            status_code=409,
        )

    connection_id = str(row["id"])
    expires_at = row["access_expires_at"]
    access_token = None

    if row["access_token_enc"] and expires_at and expires_at - REFRESH_MARGIN > datetime.now(UTC):
        try:
            access_token = crypto.decrypt(
                row["access_token_enc"], context=_context(connection_id, "access_token")
            )
        except crypto.DecryptionError:
            # A key that no longer opens this row. Refreshing gets a new token
            # under the current key, which is the repair.
            access_token = None

    if access_token is None:
        access_token = await _refresh_access_token(
            connection_id=connection_id,
            user_id=user_id,
            refresh_token_enc=row["refresh_token_enc"],
        )

    async with db.service_connection(reason="oauth: mark connection used") as connection:
        await connection.execute(
            "update public.integration_connections set last_used_at = now() where id = $1",
            row["id"],
        )

    return LiveCredentials(
        access_token=access_token,
        scopes=tuple(row["scopes"] or ()),
        account_email=row["account_email"],
        connection_id=connection_id,
    )


async def _refresh_access_token(
    *, connection_id: str, user_id: str, refresh_token_enc: str | None
) -> str:
    if not refresh_token_enc:
        await _mark_needs_reauth(connection_id)
        raise AppError(
            "Your Google connection needs to be renewed.",
            code="integration_needs_reauth",
            status_code=409,
        )

    try:
        refresh_token = crypto.decrypt(
            refresh_token_enc, context=_context(connection_id, "refresh_token")
        )
    except crypto.DecryptionError as exc:
        await _mark_needs_reauth(connection_id)
        raise AppError(
            "Your Google connection needs to be renewed.",
            code="integration_needs_reauth",
            status_code=409,
        ) from exc

    config = settings()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(
                TOKEN_ENDPOINT,
                data={
                    "client_id": config.google_client_id,
                    "client_secret": config.google_client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
    except httpx.HTTPError as exc:
        raise UpstreamUnavailable() from exc

    if response.status_code >= 400:
        # `invalid_grant` here means the user revoked us at Google, changed
        # their password, or the token aged out. Not retryable: the only fix is
        # a fresh consent, so say so rather than looping.
        log.info(
            "refresh refused %s",
            safe(user_id=user_id, connection_id=connection_id, status=response.status_code),
        )
        await _mark_needs_reauth(connection_id)
        raise AppError(
            "Your Google connection needs to be renewed.",
            code="integration_needs_reauth",
            status_code=409,
        )

    payload = response.json()
    access_token = payload.get("access_token") or ""
    if not access_token:
        raise UpstreamUnavailable()

    expires_at = datetime.now(UTC) + timedelta(seconds=int(payload.get("expires_in") or 3600))
    rotated = payload.get("refresh_token")

    async with db.service_connection(reason="oauth: store refreshed token") as connection:
        await connection.execute(
            """
            update public.integration_secrets
               set access_token_enc = $2,
                   access_expires_at = $3,
                   refresh_token_enc = coalesce($4, refresh_token_enc),
                   key_version = $5
             where connection_id = $1
            """,
            connection_id,
            crypto.encrypt(access_token, context=_context(connection_id, "access_token")),
            expires_at,
            crypto.encrypt(rotated, context=_context(connection_id, "refresh_token"))
            if rotated
            else None,
            crypto.keyring().current_version,
        )

    return access_token


async def _mark_needs_reauth(connection_id: str) -> None:
    async with db.service_connection(reason="oauth: flag reconnect needed") as connection:
        await connection.execute(
            """
            update public.integration_connections
               set status = 'needs_reauth', needs_reauth_at = now()
             where id = $1
            """,
            connection_id,
        )


# ------------------------------------------------------------------- revoking


async def revoke_connection(*, connection_id: str, user_id: str) -> None:
    """Revoke at the provider, then shred the stored credential.

    Ownership is re-checked here against `user_id` even though every caller has
    already established it. This function can delete a credential; a defence
    that costs one `and user_id = $2` is worth having twice.
    """
    async with db.service_connection(reason="oauth: read token for revocation") as connection:
        row = await connection.fetchrow(
            """
            select s.refresh_token_enc, s.access_token_enc
              from public.integration_secrets s
              join public.integration_connections c on c.id = s.connection_id
             where s.connection_id = $1
               and c.user_id = $2
            """,
            connection_id,
            user_id,
        )

    token: str | None = None
    if row:
        for column, field in (
            (row["refresh_token_enc"], "refresh_token"),
            (row["access_token_enc"], "access_token"),
        ):
            if not column:
                continue
            try:
                token = crypto.decrypt(column, context=_context(connection_id, field))
                break
            except crypto.DecryptionError:
                continue

    revoked_upstream = False
    if token:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                response = await client.post(REVOKE_ENDPOINT, data={"token": token})
            # 200 is revoked; 400 usually means it was already invalid, which is
            # the same end state.
            revoked_upstream = response.status_code in (200, 400)
        except httpx.HTTPError:
            revoked_upstream = False

    async with db.service_transaction(reason="oauth: shred credentials") as connection:
        # The secrets row goes entirely. Marking the connection revoked while
        # leaving an encrypted token behind would mean a bug elsewhere could
        # still find and use it.
        await connection.execute(
            "delete from public.integration_secrets where connection_id = $1", connection_id
        )
        await connection.execute(
            """
            update public.integration_connections
               set revoked_at = now(), status = 'revoked'
             where id = $1 and user_id = $2
            """,
            connection_id,
            user_id,
        )

    log.info(
        "revoked %s",
        safe(
            user_id=user_id,
            connection_id=connection_id,
            outcome="upstream" if revoked_upstream else "local-only",
        ),
    )


async def disconnect(*, user_id: str, provider: str = "google") -> bool:
    """Disconnect whatever live connection this user has for `provider`."""
    async with db.service_connection(reason="oauth: find connection to disconnect") as connection:
        connection_id = await connection.fetchval(
            """
            select id from public.integration_connections
             where user_id = $1 and provider = $2 and revoked_at is null
            """,
            user_id,
            provider,
        )

    if not connection_id:
        return False

    await revoke_connection(connection_id=str(connection_id), user_id=user_id)
    return True
