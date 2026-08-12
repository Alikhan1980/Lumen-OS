"""The desktop client's half of authentication.

Holds the signed-in session, keeps it fresh, and makes authenticated calls to
the API. Nothing here decides anything about authorisation -- the server does
all of that -- but where the tokens are kept is this file's problem, and getting
it wrong on a desktop app is the classic way a well-built backend gets undone.

**Where the refresh token lives.** In the operating system's credential store,
next to the AI provider keys, through the same `agent/providers/keystore.py` the
app already uses. Not in `.env`, not in a JSON file beside the app, and not in
anything the chat page can read. On Windows that is the Credential Manager,
encrypted per Windows user, which means two people sharing a machine cannot
reach each other's session even before the server gets involved.

**Where the access token lives.** In memory only, for the life of the process.
It is short-lived by design, so persisting it would add risk and save nothing --
a restart can always mint a new one from the refresh token.

**What the page is given.** Nothing. The chat page talks to the local loopback
server, which attaches the Authorization header itself. So an access token never
appears in a URL, in page JavaScript, or in the DOM, and there is no XSS in the
chat UI that yields a token to an attacker.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from dataclasses import dataclass

import httpx

from ..logs import logger
from ..providers.keystore import KeystoreError, keystore

log = logger("auth")

# The account name under which the refresh token is filed in the OS store.
REFRESH_ACCOUNT = "lumen-refresh-token"
# Non-secret: which server this session belongs to, so switching environments
# does not silently reuse a token minted by a different deployment.
ENDPOINT_ACCOUNT = "lumen-api-endpoint"

# Renew this far ahead of expiry, so a request that takes a moment does not go
# out with a token that dies in flight.
RENEW_MARGIN_SECONDS = 120

TIMEOUT = httpx.Timeout(20.0, connect=8.0)


class AuthError(RuntimeError):
    """Something the user needs to be told about, in words they can act on."""

    def __init__(self, message: str, *, code: str = "error"):
        super().__init__(message)
        self.message = message
        self.code = code


class SessionExpired(AuthError):
    def __init__(self) -> None:
        super().__init__(
            "Your session has expired. Please sign in again.", code="unauthenticated"
        )


@dataclass
class Account:
    user_id: str
    email: str | None
    email_verified: bool
    display_name: str = ""
    onboarded: bool = False

    def as_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "email": self.email,
            "email_verified": self.email_verified,
            "display_name": self.display_name,
            "onboarded": self.onboarded,
        }


class Session:
    """One signed-in user, and the API calls made as them.

    Thread-safe: the web server is threaded, and two requests arriving together
    on an expired token must not both start a refresh. The lock makes the second
    one wait and then find a fresh token already in place.
    """

    def __init__(self, api_url: str):
        self.api_url = api_url.rstrip("/")
        self._lock = threading.RLock()
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._account: Account | None = None
        self._restore_attempted = False

    # ------------------------------------------------------------ persistence

    def _store(self):
        try:
            return keystore()
        except KeystoreError as exc:
            raise AuthError(
                "This computer has no secure place to keep your session. "
                + str(exc),
                code="no_keystore",
            ) from exc

    def _save_refresh(self, token: str) -> None:
        store = self._store()
        store.set(REFRESH_ACCOUNT, token)
        store.set(ENDPOINT_ACCOUNT, self.api_url)

    def _load_refresh(self) -> str | None:
        try:
            store = keystore()
        except KeystoreError:
            return None
        # A token minted by a different deployment will not verify there, and
        # trying it produces a confusing "session expired" on first launch.
        if (store.get(ENDPOINT_ACCOUNT) or self.api_url) != self.api_url:
            return None
        return store.get(REFRESH_ACCOUNT)

    def _forget(self) -> None:
        try:
            store = keystore()
        except KeystoreError:
            return
        store.delete(REFRESH_ACCOUNT)
        store.delete(ENDPOINT_ACCOUNT)

    # ----------------------------------------------------------------- state

    @property
    def account(self) -> Account | None:
        return self._account

    def is_signed_in(self) -> bool:
        with self._lock:
            return self._account is not None and bool(self._access_token)

    def state(self) -> dict:
        """What the page needs to decide which screen to show.

        Deliberately carries no token. The page never holds one -- see the
        module docstring.
        """
        with self._lock:
            if self._account is None:
                return {"signed_in": False}
            return {"signed_in": True, "account": self._account.as_dict()}

    # ------------------------------------------------------------------ calls

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        authenticated: bool = True,
        retry_on_401: bool = True,
    ) -> dict:
        headers = {
            "Content-Type": "application/json",
            # Tells the API this is not a browser, so the refresh token comes
            # back in the body for the credential store rather than in a cookie.
            "X-Lumen-Client": "desktop",
        }
        if authenticated:
            headers["Authorization"] = f"Bearer {self._valid_access_token()}"

        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                response = client.request(
                    method, f"{self.api_url}{path}", json=json_body, headers=headers
                )
        except httpx.ConnectError as exc:
            # Nothing is listening. On a first run that is almost always "the
            # API server has not been started yet" rather than a network fault,
            # and "check your connection" sends people to look at their wifi
            # for a problem that is on their own machine. Say which address
            # failed and what to do about it.
            raise AuthError(self._unreachable_message(), code="api_unreachable") from exc
        except httpx.HTTPError as exc:
            raise AuthError(
                "Could not reach the server. Check your connection and try again.",
                code="network",
            ) from exc

        if (
            response.status_code == 401
            and authenticated
            and retry_on_401
            and self._about_the_session(response)
        ):
            # The access token may simply have aged out between the check and
            # the call. One refresh, one retry, then give up.
            if self._refresh():
                return self._request(
                    method, path, json_body=json_body, authenticated=True, retry_on_401=False
                )
            self.sign_out(local_only=True)
            raise SessionExpired()

        return self._unwrap(response)

    def _unreachable_message(self) -> str:
        """Why nothing answered, in terms of the thing the user can act on.

        A loopback address means the accounts server is simply not running on
        this machine, which is the state every checkout starts in. A remote one
        means something outside their control, so the advice is different.
        """
        from urllib.parse import urlparse

        host = (urlparse(self.api_url).hostname or "").lower()
        # S104: this is a set of hostnames being *recognised*, not an address
        # being bound to. Nothing here opens a socket.
        if host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}:  # noqa: S104
            return (
                f"The accounts server is not running at {self.api_url}. "
                "Start it with:  python -m uvicorn server.main:app --port 8000  "
                "— first-time setup is in SETUP-AUTH.md."
            )
        return (
            f"Could not reach the accounts server at {self.api_url}. "
            "Check your connection and try again."
        )

    @staticmethod
    def _about_the_session(response: httpx.Response) -> bool:
        """Whether a 401 is about the token, or about something the user typed.

        Not every 401 is an expired session. Changing a password re-checks the
        current one and answers `invalid_credentials` when it is wrong, and the
        server keeps `unauthenticated` for a token it will not accept. Treating
        the two the same means a mistyped password spends a refresh, and -- if
        that refresh does not land -- signs the user out for a typo, with
        "your session has expired" in place of "that is not your password".

        A 401 with nothing to read is treated as the session, which is the
        safe way round: at worst it costs one refresh.
        """
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            return True
        code = (payload.get("error") or {}).get("code")
        return code in {None, "", "unauthenticated"}

    @staticmethod
    def _unwrap(response: httpx.Response) -> dict:
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {}

        if response.status_code >= 400:
            error = payload.get("error") or {}
            raise AuthError(
                error.get("message") or "Something went wrong. Please try again.",
                code=error.get("code") or "error",
            )
        return payload

    def _valid_access_token(self) -> str:
        with self._lock:
            if self._access_token and time.time() < self._expires_at - RENEW_MARGIN_SECONDS:
                return self._access_token
            if self._refresh():
                return self._access_token  # type: ignore[return-value]
        raise SessionExpired()

    def _adopt(self, payload: dict) -> None:
        """Take a session response and become that user."""
        with self._lock:
            self._access_token = payload.get("access_token")
            self._expires_at = time.time() + int(payload.get("expires_in") or 3600)
            user = payload.get("user") or {}
            self._account = Account(
                user_id=str(user.get("id") or ""),
                email=user.get("email"),
                email_verified=bool(user.get("email_verified")),
            )
            refresh = payload.get("refresh_token")
        if refresh:
            self._save_refresh(refresh)

    def _refresh(self) -> bool:
        """Trade the stored refresh token for a new access token."""
        stored = self._load_refresh()
        if not stored:
            return False

        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                response = client.post(
                    f"{self.api_url}/api/auth/refresh",
                    json={"refresh_token": stored},
                    headers={"X-Lumen-Client": "desktop", "Content-Type": "application/json"},
                )
        except httpx.HTTPError:
            # Offline. Keep the stored token -- it is probably still good, and
            # discarding it would sign the user out for a flaky network.
            log.info("refresh failed: server unreachable")
            return False

        if response.status_code >= 400:
            # Rejected: spent, revoked, or from another deployment. Discard it,
            # otherwise every launch retries a dead credential.
            log.info("refresh rejected; clearing stored session")
            self._forget()
            with self._lock:
                self._access_token = None
                self._account = None
            return False

        self._adopt(response.json())
        return True

    # ------------------------------------------------------------- the flows

    def restore(self) -> bool:
        """Try to resume a session at startup. False means show the sign-in screen.

        Called from the page's first `/api/auth/state`, which is the first thing
        that wants an answer. Someone who signed in yesterday has a refresh token
        in the credential store, and asking for their password again because the
        app was closed would make storing it pointless.

        One attempt per process, deliberately. If the server was down at launch
        the sign-in screen is the honest answer, and a retry on every call would
        make each one sit out a connect timeout first. Signing in by hand still
        works, and succeeds or fails on its own timing.
        """
        with self._lock:
            if self._account is not None and self._access_token:
                return True
            if self._restore_attempted:
                return False
            self._restore_attempted = True
            if not self._refresh():
                return False
        try:
            self.load_profile()
        except AuthError:
            return False
        return True

    def sign_up(self, *, name: str, email: str, password: str) -> dict:
        """Create an account, and sign in if the server hands back a session.

        Two shapes come back, depending on how the project is configured:

        * confirmation required -- 202, no token, the user must click a link;
        * confirmation off -- 201 with a full session, already signed in.

        The second is easy to miss, and missing it means the account is created
        and then the app still shows the sign-in screen. Adopt whatever session
        arrives rather than assuming there is never one.
        """
        payload = self._request(
            "POST",
            "/api/auth/signup",
            json_body={"name": name, "email": email, "password": password},
            authenticated=False,
        )
        if payload.get("access_token"):
            self._adopt(payload)
            self.load_profile()
        return payload

    def sign_in(self, *, email: str, password: str, remember: bool = True) -> dict:
        payload = self._request(
            "POST",
            "/api/auth/login",
            json_body={"email": email, "password": password, "remember": remember},
            authenticated=False,
        )
        self._adopt(payload)
        self.load_profile()
        return self.state()

    def load_profile(self) -> Account:
        payload = self._request("GET", "/api/auth/session")
        with self._lock:
            if self._account is None:
                raise SessionExpired()
            profile = payload.get("profile") or {}
            self._account.display_name = profile.get("display_name") or ""
            self._account.onboarded = bool(payload.get("onboarded"))
            user = payload.get("user") or {}
            self._account.email_verified = bool(user.get("email_verified"))
            return self._account

    def sign_out(self, *, everywhere: bool = False, local_only: bool = False) -> None:
        """Forget this session, and optionally every session on every device.

        Clearing local state happens whatever the server says. A logout that
        fails because the network is down must still log the user out of the
        machine in front of them -- that is the one place they can see.
        """
        if not local_only:
            # Whatever the server says, the local session is cleared below.
            with contextlib.suppress(AuthError):
                self._request(
                    "POST",
                    "/api/auth/logout",
                    json_body={"scope": "global" if everywhere else "local"},
                    retry_on_401=False,
                )

        self._forget()
        with self._lock:
            self._access_token = None
            self._expires_at = 0.0
            self._account = None

    def resend_verification(self, email: str) -> dict:
        return self._request(
            "POST",
            "/api/auth/verify/resend",
            json_body={"email": email},
            authenticated=False,
        )

    def forgot_password(self, email: str) -> dict:
        return self._request(
            "POST",
            "/api/auth/password/forgot",
            json_body={"email": email},
            authenticated=False,
        )

    def change_password(self, *, current: str, new: str) -> dict:
        return self._request(
            "POST",
            "/api/auth/password/change",
            json_body={"current_password": current, "new_password": new},
        )

    def oauth_url(self, provider: str) -> str:
        payload = self._request(
            "GET", f"/api/auth/oauth/{provider}/start", authenticated=False
        )
        return payload.get("url", "")

    def config(self) -> dict:
        """Which sign-in methods this deployment offers. Cached by the caller."""
        return self._request("GET", "/api/auth/config", authenticated=False)

    # --------------------------------------------------------------- proxying

    def api(self, method: str, path: str, body: dict | None = None) -> dict:
        """Make an authenticated call on the signed-in user's behalf.

        The page calls the local server, the local server calls this, and this
        attaches the token. That indirection is what keeps the access token out
        of the browser entirely.
        """
        return self._request(method, path, json_body=body)


DEFAULT_API_URL = "http://127.0.0.1:8000"


def accounts_enabled() -> bool:
    """Whether this app requires an account at all.

    Two backends can provide one, and either is enough:

    * **Supabase**, bundled into the build -- the one that works on a machine
      someone downloaded the app onto, because the project is already hosted.
    * **`LUMEN_API_URL`**, the FastAPI app in `server/` -- for developing
      against a local or self-hosted deployment. Takes precedence when set.

    With neither, there are no accounts: the single-user desktop app it was
    before there was a server, with reminders, provider keys and Google
    connections all local to the machine and nothing to sign up for.

    The address is the switch for the server backend, rather than a separate
    flag beside it, because there is nothing to sign in *to* without one -- and
    a flag that can disagree with the address is a way to be pointed at a server
    you are not using.
    """
    if (os.getenv("LUMEN_API_URL") or "").strip():
        return True
    from ..config import supabase_config

    return supabase_config() is not None


_shared: Session | None = None
_shared_lock = threading.Lock()


def shared(api_url: str | None = None) -> Session:
    """The process-wide session for the desktop app.

    One per process because a desktop app has one user at the keyboard. The
    server does not use this at all -- it derives identity per request.

    An explicit address, or LUMEN_API_URL, means someone is pointing this at a
    deployment of `server/` and gets the session that speaks to it. Otherwise
    the bundled Supabase project answers, which is the path every downloaded
    copy takes.
    """
    global _shared  # noqa: PLW0603 -- one desktop session per process
    with _shared_lock:
        if _shared is not None:
            return _shared

        explicit = api_url or (os.getenv("LUMEN_API_URL") or "").strip()
        if explicit:
            _shared = Session(explicit)
            return _shared

        from ..config import supabase_config
        from .supabase import SupabaseSession

        config = supabase_config()
        _shared = SupabaseSession(*config) if config else Session(DEFAULT_API_URL)
        return _shared


def reset_shared() -> None:
    """Test seam."""
    global _shared  # noqa: PLW0603 -- see shared()
    with _shared_lock:
        _shared = None
