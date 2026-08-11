"""A thin client for Supabase Auth (GoTrue).

This is the only module that talks to the auth server, and it is deliberately
thin: it speaks HTTP, normalises the response shapes, and maps upstream failures
onto our own exceptions. It makes no policy decisions -- whether to reveal that
an account exists, how many attempts to allow, what a password must look like --
because those belong in the route, where the whole picture is visible.

Why proxy GoTrue at all instead of letting the client call it directly?

  * One door to rate limit, log and audit. A client that can reach GoTrue
    directly can be brute-forced past any limiter we run.
  * The anon key stays on the server. It is not a secret in the strict sense,
    but it is a standing invitation to the auth endpoints, and a shipped
    desktop binary is a poor place to put one.
  * Our own error vocabulary. GoTrue's messages are written for developers and
    occasionally distinguish cases we deliberately refuse to distinguish, such
    as "user not found" versus "wrong password".

Passwords pass through this module and are never logged, stored, or included in
an exception. The redacting filter in observability.py is the backstop, not the
plan.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

import httpx

from ..errors import UpstreamUnavailable
from ..observability import logger, mask_email, safe
from ..settings import settings

log = logger("gotrue")

TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class GoTrueError(Exception):
    """An upstream refusal we are expected to interpret.

    `code` is GoTrue's machine-readable error code when it sent one, and
    `status` its HTTP status. Route handlers branch on these; users never see
    either.
    """

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}")
        self.status = status
        self.code = code
        self.message = message

    @property
    def is_invalid_credentials(self) -> bool:
        return self.status in (400, 401) and self.code in {
            "invalid_credentials",
            "invalid_grant",
            "",
        }

    @property
    def is_already_registered(self) -> bool:
        return self.code in {"user_already_exists", "email_exists"} or (
            self.status == 422 and "already" in self.message.lower()
        )

    @property
    def is_not_verified(self) -> bool:
        return self.code == "email_not_confirmed"

    @property
    def is_weak_password(self) -> bool:
        return self.code == "weak_password"

    @property
    def is_rate_limited(self) -> bool:
        return self.status == 429 or self.code == "over_email_send_rate_limit"

    @property
    def is_same_password(self) -> bool:
        return self.code == "same_password"


@dataclass(frozen=True)
class Session:
    """A signed-in session as GoTrue reports it."""

    access_token: str
    refresh_token: str
    expires_in: int
    user_id: str
    email: str | None
    email_verified: bool

    def client_payload(self) -> dict:
        """What may cross the wire to a client.

        The access token, because the client needs it to call this API, and
        nothing else that is secret. The refresh token is handled separately by
        the route -- it goes into an httpOnly cookie for browsers, and is
        returned only to the desktop client, which stores it in the OS
        credential manager rather than in anything a page can read.
        """
        return {
            "access_token": self.access_token,
            "expires_in": self.expires_in,
            "user": {
                "id": self.user_id,
                "email": self.email,
                "email_verified": self.email_verified,
            },
        }


def _session_from(payload: dict) -> Session | None:
    access = payload.get("access_token")
    if not access:
        return None
    user = payload.get("user") or {}
    return Session(
        access_token=access,
        refresh_token=payload.get("refresh_token") or "",
        expires_in=int(payload.get("expires_in") or 3600),
        user_id=str(user.get("id") or ""),
        email=user.get("email"),
        email_verified=bool(user.get("email_confirmed_at") or user.get("confirmed_at")),
    )


class GoTrue:
    """HTTP calls against the project's auth server."""

    def __init__(self, *, service_role: bool = False):
        config = settings()
        self._base = config.gotrue_url
        key = config.supabase_service_key if service_role else config.supabase_anon_key
        self._headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        bearer: str | None = None,
    ) -> dict:
        headers = dict(self._headers)
        if bearer:
            # Acting as the user rather than as the project.
            headers["Authorization"] = f"Bearer {bearer}"

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                response = await client.request(
                    method, f"{self._base}{path}", json=json, params=params, headers=headers
                )
        except httpx.HTTPError as exc:
            log.warning("auth server unreachable %s", safe(path=path, reason=type(exc).__name__))
            raise UpstreamUnavailable() from exc

        if response.status_code >= 400:
            payload: dict[str, Any] = {}
            # An error body that is not JSON (a gateway's HTML page, say) still
            # has to produce our own error shape rather than a decode failure.
            with contextlib.suppress(ValueError):
                payload = response.json()
            code = str(
                payload.get("error_code") or payload.get("error") or ""
            ).strip()
            message = str(
                payload.get("msg")
                or payload.get("message")
                or payload.get("error_description")
                or ""
            ).strip()
            log.info(
                "auth refused %s",
                safe(path=path, status=response.status_code, code=code or "unknown"),
            )
            raise GoTrueError(response.status_code, code, message)

        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    # ------------------------------------------------------------------ signup

    async def sign_up(
        self, *, email: str, password: str, display_name: str, redirect_to: str
    ) -> Session | None:
        """Create an account.

        Returns a Session when the project allows immediate sign-in, and None
        when it is configured to require email confirmation first -- which is
        how we run it. Callers must treat None as success.
        """
        payload = await self._request(
            "POST",
            "/signup",
            json={
                "email": email,
                "password": password,
                # Lands in raw_user_meta_data, which the provisioning trigger in
                # 0001_identity.sql reads to seed the profile.
                "data": {"display_name": display_name},
            },
            params={"redirect_to": redirect_to} if redirect_to else None,
        )
        log.info("signup accepted %s", safe(outcome="ok"))
        return _session_from(payload)

    # ------------------------------------------------------------------- login

    async def sign_in(self, *, email: str, password: str) -> Session:
        payload = await self._request(
            "POST",
            "/token",
            params={"grant_type": "password"},
            json={"email": email, "password": password},
        )
        session = _session_from(payload)
        if session is None:
            raise GoTrueError(500, "no_session", "auth server returned no session")
        return session

    async def refresh(self, *, refresh_token: str) -> Session:
        payload = await self._request(
            "POST",
            "/token",
            params={"grant_type": "refresh_token"},
            json={"refresh_token": refresh_token},
        )
        session = _session_from(payload)
        if session is None:
            raise GoTrueError(401, "invalid_grant", "refresh rejected")
        return session

    async def sign_out(self, *, access_token: str, scope: str = "local") -> None:
        """End a session.

        `scope` is GoTrue's: "local" ends this one, "global" ends every session
        the user has anywhere, "others" ends all but this one. "global" is what
        "log out of all devices" means, and it revokes the refresh tokens
        server-side rather than merely forgetting them on the client.
        """
        if scope not in {"local", "global", "others"}:
            scope = "local"
        try:
            await self._request(
                "POST", "/logout", params={"scope": scope}, bearer=access_token
            )
        except GoTrueError as exc:
            # An already-dead session is a successful logout as far as the user
            # is concerned. Never surface this.
            if exc.status not in (401, 403, 404):
                raise

    # -------------------------------------------------------------- email flows

    async def send_reset(self, *, email: str, redirect_to: str) -> None:
        await self._request(
            "POST",
            "/recover",
            json={"email": email},
            params={"redirect_to": redirect_to} if redirect_to else None,
        )

    async def resend_verification(self, *, email: str, redirect_to: str) -> None:
        await self._request(
            "POST",
            "/resend",
            json={"type": "signup", "email": email},
            params={"redirect_to": redirect_to} if redirect_to else None,
        )

    async def verify_otp(self, *, email: str, token: str, kind: str) -> Session:
        """Exchange an emailed code for a session.

        Used by the reset flow: the link in the email carries a one-time token
        which GoTrue trades for a short-lived session, and the new password is
        then set with `update_password`. The token is single-use and expiring on
        GoTrue's side -- we do not mint, store or validate it ourselves, which
        is precisely why the reset flow is not a place we can get wrong.
        """
        payload = await self._request(
            "POST",
            "/verify",
            json={"type": kind, "email": email, "token": token},
        )
        session = _session_from(payload)
        if session is None:
            raise GoTrueError(401, "invalid_token", "verification rejected")
        return session

    # ------------------------------------------------------------------- user

    async def get_user(self, *, access_token: str) -> dict:
        return await self._request("GET", "/user", bearer=access_token)

    async def update_password(self, *, access_token: str, password: str) -> None:
        await self._request("PUT", "/user", json={"password": password}, bearer=access_token)

    async def update_email(self, *, access_token: str, email: str, redirect_to: str) -> None:
        """Start an email change.

        GoTrue sends a confirmation to the *new* address (and, when the project
        is configured for it, to the old one too) and does not apply the change
        until it is clicked. So this returning successfully does not mean the
        address has changed -- it means an email is on its way.
        """
        await self._request(
            "PUT",
            "/user",
            json={"email": email},
            params={"redirect_to": redirect_to} if redirect_to else None,
            bearer=access_token,
        )

    # ------------------------------------------------------------------ social

    def authorize_url(self, *, provider: str, redirect_to: str) -> str:
        """Where to send a browser to sign in with Google or Apple.

        GoTrue handles the handshake and redirects back with a code the client
        exchanges. Building the URL is all we do -- no secret of ours is
        involved, which is why this one is not an async call.
        """
        from urllib.parse import urlencode

        query = urlencode({"provider": provider, "redirect_to": redirect_to})
        return f"{self._base}/authorize?{query}"

    # ------------------------------------------------------------------- admin

    async def delete_user(self, *, user_id: str) -> None:
        """Remove the auth user, cascading every table that references it.

        Service-role only. This is irreversible and is the last step of account
        deletion, after integrations have been revoked.
        """
        await self._request("DELETE", f"/admin/users/{user_id}")
        log.info("account deleted %s", safe(user_id=user_id))


def anon() -> GoTrue:
    return GoTrue()


def admin() -> GoTrue:
    """The service-role client. Only for `delete_user`."""
    return GoTrue(service_role=True)


def log_attempt(action: str, email: str | None, outcome: str) -> None:
    """One place that knows an address may be logged only masked."""
    log.info(
        "%s %s",
        action,
        safe(action=action, outcome=outcome, reason=mask_email(email) if email else "-"),
    )
