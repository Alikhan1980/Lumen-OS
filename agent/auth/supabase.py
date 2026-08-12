"""Accounts backed directly by Supabase Auth, with no server of our own.

`session.Session` talks to the FastAPI app in `server/`, which is the right
shape when that server is deployed somewhere. It is the wrong shape for a
desktop app people download: `server/` would have to be running on *their*
machine, where it never is, so the accounts layer silently switches itself off
and the app is ungated.

This subclass points the same session at the Supabase project's own auth API
instead. Supabase is already hosted, so the account gate works on any machine
that can reach the internet, and no deployment is involved.

The anon key ships inside the build. That is what it is for -- it is the public
half of a Supabase project and appears in the JavaScript of every Supabase web
app. Row-level security is the thing that actually restricts what a caller can
touch, which is why `supabase/migrations/0004_rls.sql` matters more than hiding
this key ever could. The service-role key is the dangerous one and is never
bundled, read or referenced here.
"""

from __future__ import annotations

import contextlib
import time

import httpx

from ..logs import logger
from .session import (
    TIMEOUT,
    Account,
    AuthError,
    Session,
    SessionExpired,
)

log = logger("auth.supabase")


def _message(payload: dict, fallback: str) -> str:
    """Pull a human message out of whichever error shape GoTrue used.

    Supabase has shipped three: `{"msg": ...}` on older releases,
    `{"error": ..., "error_description": ...}` on OAuth-style failures, and
    `{"message": ..., "error_code": ...}` on current ones. Checking all three
    is cheaper than pinning a version.
    """
    for field in ("msg", "message", "error_description", "error"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _code(payload: dict, status: int) -> str:
    raw = payload.get("error_code") or payload.get("code") or ""
    if isinstance(raw, str) and raw:
        return raw
    return "unauthenticated" if status == 401 else "error"


class SupabaseSession(Session):
    """A Session whose backend is Supabase Auth rather than `server/`."""

    def __init__(self, url: str, anon_key: str):
        super().__init__(url)
        self.anon_key = anon_key

    # ------------------------------------------------------------- transport

    def _headers(self, token: str | None) -> dict[str, str]:
        # apikey identifies the project on every call; Authorization carries
        # the user when there is one and the anon key when there is not,
        # which is what GoTrue expects for signup and password recovery.
        return {
            "apikey": self.anon_key,
            "Authorization": f"Bearer {token or self.anon_key}",
            "Content-Type": "application/json",
        }

    def _call(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        authenticated: bool = True,
        retry_on_401: bool = True,
    ) -> dict:
        token = self._valid_access_token() if authenticated else None
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                response = client.request(
                    method,
                    f"{self.api_url}/auth/v1{path}",
                    json=json_body,
                    headers=self._headers(token),
                )
        except httpx.HTTPError as exc:
            raise AuthError(
                f"Could not reach the accounts service at {self.api_url}. "
                "Check your connection and try again.",
                code="unreachable",
            ) from exc

        if response.status_code == 401 and authenticated and retry_on_401 and self._refresh():
            return self._call(
                method, path, json_body=json_body, authenticated=True, retry_on_401=False
            )

        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        if response.status_code >= 400:
            raise AuthError(
                _message(payload, "Something went wrong. Please try again."),
                code=_code(payload, response.status_code),
            )
        return payload

    # ----------------------------------------------------------------- state

    def _adopt(self, payload: dict) -> None:
        """Become the user in a GoTrue token response.

        Same job as the base class, different field names: GoTrue reports
        verification as a timestamp on the user rather than a boolean, and
        carries the display name in `user_metadata`.
        """
        user = payload.get("user") or {}
        with self._lock:
            self._access_token = payload.get("access_token")
            self._expires_at = time.time() + int(payload.get("expires_in") or 3600)
            metadata = user.get("user_metadata") or {}
            self._account = Account(
                user_id=str(user.get("id") or ""),
                email=user.get("email"),
                email_verified=bool(user.get("email_confirmed_at")),
            )
            self._account.display_name = str(metadata.get("name") or "")
            # There is no profile table behind this backend, so there is no
            # separate onboarding flag to fetch. Having an account is the
            # whole of what this gate asks for.
            self._account.onboarded = True
            refresh = payload.get("refresh_token")
        if refresh:
            self._save_refresh(refresh)

    def _refresh(self) -> bool:
        stored = self._load_refresh()
        if not stored:
            return False
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                response = client.post(
                    f"{self.api_url}/auth/v1/token?grant_type=refresh_token",
                    json={"refresh_token": stored},
                    headers=self._headers(None),
                )
        except httpx.HTTPError:
            # Offline. Keep the stored token: it is probably still valid, and
            # dropping it would sign the user out over a flaky network.
            log.info("refresh failed: supabase unreachable")
            return False

        if response.status_code >= 400:
            self._forget()
            return False
        try:
            payload = response.json()
        except ValueError:
            return False
        self._adopt(payload)
        return True

    # ------------------------------------------------------------- the calls

    def sign_up(self, *, name: str, email: str, password: str) -> dict:
        payload = self._call(
            "POST",
            "/signup",
            json_body={"email": email, "password": password, "data": {"name": name}},
            authenticated=False,
        )
        if payload.get("access_token"):
            self._adopt(payload)
            return payload
        # Email confirmation is on: the account exists but there is no session
        # until they click the link. The page shows "check your email".
        return {"status": "pending_verification", "signed_in": False}

    def sign_in(self, *, email: str, password: str, remember: bool = True) -> dict:
        payload = self._call(
            "POST",
            "/token?grant_type=password",
            json_body={"email": email, "password": password},
            authenticated=False,
        )
        self._adopt(payload)
        return self.state()

    def load_profile(self) -> Account:
        payload = self._call("GET", "/user")
        with self._lock:
            if self._account is None:
                raise SessionExpired()
            metadata = payload.get("user_metadata") or {}
            self._account.email = payload.get("email") or self._account.email
            self._account.email_verified = bool(payload.get("email_confirmed_at"))
            self._account.display_name = str(metadata.get("name") or "")
            return self._account

    def sign_out(self, *, everywhere: bool = False, local_only: bool = False) -> None:
        if not local_only:
            with contextlib.suppress(AuthError):
                self._call(
                    "POST",
                    "/logout?scope=" + ("global" if everywhere else "local"),
                    retry_on_401=False,
                )
        self._forget()
        with self._lock:
            self._access_token = None
            self._expires_at = 0.0
            self._account = None

    def resend_verification(self, email: str) -> dict:
        return self._call(
            "POST",
            "/resend",
            json_body={"type": "signup", "email": email},
            authenticated=False,
        )

    def forgot_password(self, email: str) -> dict:
        return self._call(
            "POST", "/recover", json_body={"email": email}, authenticated=False
        )

    def change_password(self, *, current: str, new: str) -> dict:
        # GoTrue's update-user takes the new password and authenticates by the
        # bearer token, so there is nothing to send the current one to. Verify
        # it first, otherwise anyone at an unlocked machine could change it.
        account = self.account
        if account is None or not account.email:
            raise SessionExpired()
        self._call(
            "POST",
            "/token?grant_type=password",
            json_body={"email": account.email, "password": current},
            authenticated=False,
        )
        return self._call("PUT", "/user", json_body={"password": new})

    def oauth_url(self, provider: str) -> str:
        return f"{self.api_url}/auth/v1/authorize?provider={provider}"

    def config(self) -> dict:
        """What the sign-in page needs to draw itself.

        Static rather than fetched: GoTrue's settings endpoint reports which
        providers a project has, but the page only needs to know that email
        and password work, and a failed request here would block the screen
        that lets someone sign in at all.
        """
        return {
            "environment": "production",
            "require_verified_email": True,
            "providers": {"password": True, "google": False, "apple": False},
            "password_policy": {"min_length": 8},
        }
