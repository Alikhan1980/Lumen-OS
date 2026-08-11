"""Google OAuth.

Two modes, and `service()` is the seam between them.

**Desktop (unchanged).** One browser sign-in covers every Google tool. The
resulting token is bound to whichever account approved it and is stored per
Windows user, so two people running the same build never see each other's data.
The same sign-in also proves *who* the user is to the model proxy, via the
OpenID Connect ID token Google mints alongside the access token. That is why
`openid` and `userinfo.email` are in SCOPES even though no Google API here
needs them — see `fresh_id_token`.

**Server (multi-user).** When `agent/context.py` has a user bound, credentials
come from that context instead: the API server holds the refresh token, mints a
short-lived access token per turn, and hands in a callable that produces it.
Nothing in this module reads `token.json` on that path, and nothing caches a
client across users.

The cache is the part to be careful with. `functools.cache` on `service()` was
correct when there was exactly one identity in the process and wrong the moment
there were two — a memoised Gmail client would be handed to whoever asked next.
So the memo is keyed by identity, and on the server path the key includes the
user, which makes a cross-user hit impossible rather than unlikely.
"""

from __future__ import annotations

import functools
import json
import os
import threading

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .. import context as agent_context
from ..config import ACCOUNT_PATH, CREDENTIALS_DIR, TOKEN_PATH, client_secret_path

# Google echoes the granted scope list back in a different order than it was
# requested, and expands `openid` into its own entry. oauthlib treats any
# difference as a scope-change attack and raises. Relaxing this is the standard
# and safe fix: we still verify the ID token's signature and audience server
# side, which is what actually establishes identity.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

SCOPES = [
    # Identity: who signed in. Not used by any Google API call — this is what
    # the proxy checks to decide whose usage it is paying for.
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    # Gmail: read, send, draft, label, trash. Not permanent deletion.
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    # Calendar: read and write events.
    "https://www.googleapis.com/auth/calendar",
    # Drive: read, create, share.
    "https://www.googleapis.com/auth/drive",
    # Contacts: lookup only.
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/contacts.other.readonly",
    # Tasks: read and write.
    "https://www.googleapis.com/auth/tasks",
]

MISSING_SECRET_HELP = f"""This build has no Google OAuth client bundled.

Either rebuild it with client_secret.json in the project root (see
DISTRIBUTION.md), or drop your own Desktop OAuth client at:

  {CREDENTIALS_DIR / "client_secret.json"}

then run the app again.
"""


class GoogleAuthError(RuntimeError):
    pass


def _run_flow() -> Credentials:
    secret = client_secret_path()
    if secret is None:
        raise GoogleAuthError(MISSING_SECRET_HELP)
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    # port=0 picks a free port; the loopback redirect is what makes this work
    # for a Desktop OAuth client with no server involved.
    # The URL must be printed, not just auto-opened: the default browser may be
    # signed into the wrong Google account, or may not open at all. The local
    # server only lives for the duration of this call, so a URL from an earlier
    # run is always dead — that shows up as "This site can't be reached".
    return flow.run_local_server(
        port=0,
        prompt="consent",
        authorization_prompt_message=(
            "\nOpening your browser to sign in to Google.\n"
            "Keep this window open until it finishes.\n\n"
            "If the browser does not open, or opens the wrong Google account, "
            "paste this URL into the browser you want to use:\n\n{url}\n"
        ),
        success_message="Signed in. You can close this tab and return to the app.",
    )


def get_credentials(force_login: bool = False) -> Credentials:
    """The desktop path's credentials, from the local token file.

    Refuses outright when a user is bound. Two things would go wrong otherwise,
    and both are severe: it would hand the host's own Google account to whoever
    is mid-turn, and `_run_flow()` would try to open a browser and start a
    consent screen on the server. Server callers go through
    `context.google_credentials()` instead.
    """
    if agent_context.current() is not None:
        raise GoogleAuthError(
            "get_credentials() is the single-user desktop path and cannot be "
            "used while a request context is bound. Use the credentials from "
            "agent.context.current() instead."
        )

    creds: Credentials | None = None

    if TOKEN_PATH.exists() and not force_login:
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        except ValueError:
            creds = None  # malformed, or written under an older scope set

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            creds = None  # revoked, expired past recovery, or scopes changed

    if not creds or not creds.valid:
        creds = _run_flow()

    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return creds


_id_token_lock = threading.Lock()


def fresh_id_token() -> str:
    """A currently-valid OpenID Connect ID token for the signed-in account.

    This is the credential the proxy authenticates: a JWT signed by Google
    carrying the user's verified email, an audience equal to this app's OAuth
    client, and about an hour of life. Unlike an API key it cannot be replayed
    as someone else and cannot be minted by the client.

    `id_token` is only populated when Google issues one, which happens on the
    initial consent and on every refresh. A token loaded from disk may not carry
    one, so refresh whenever it is missing or stale rather than trusting what
    was serialised.
    """
    with _id_token_lock:
        creds = get_credentials()
        if not creds.id_token or creds.expired:
            creds.refresh(Request())
            TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        if not creds.id_token:
            raise GoogleAuthError(
                "Google did not return an identity token. Sign out and sign in "
                "again so the openid scope is granted."
            )
        return creds.id_token


@functools.cache
def _local_service(api: str, version: str):
    """Memoised client for the desktop path's single signed-in account."""
    return build(api, version, credentials=get_credentials(), cache_discovery=False)


def service(api: str, version: str):
    """A Google API client for whoever this turn belongs to.

    On the server, that is the user bound in `agent/context.py`, and the client
    is built fresh from an access token the API server just minted. Building
    per call rather than memoising is deliberate: a cached client holds a
    credentials object, and a credentials object is exactly the thing that must
    not outlive the request it was made for.

    With no context bound this is the original desktop behaviour, memo and all.
    """
    context = agent_context.current()
    if context is None or context.google_credentials is None:
        return _local_service(api, version)

    return build(api, version, credentials=context.google_credentials(), cache_discovery=False)


def reset_service_cache() -> None:
    _local_service.cache_clear()


def refresh_account_email() -> str | None:
    """Ask Google who we are and cache it, so the UI can show it for free."""
    try:
        profile = service("gmail", "v1").users().getProfile(userId="me").execute()
    except Exception:
        return None
    email = profile.get("emailAddress")
    if email:
        ACCOUNT_PATH.parent.mkdir(parents=True, exist_ok=True)
        ACCOUNT_PATH.write_text(json.dumps({"email": email}), encoding="utf-8")
    return email


def cached_account_email() -> str | None:
    """The signed-in address recorded at sign-in. No network call.

    Reads the local file only when no user is bound. On the server that file
    holds whichever Google account the *operator* happened to sign in with in a
    checkout, and returning it during someone's turn would show one user another
    account's address — a small leak, but a leak, and the kind that ends up in a
    prompt. When a user is bound, ask Google as that user instead.
    """
    context = agent_context.current()
    if context is not None:
        try:
            profile = service("gmail", "v1").users().getProfile(userId="me").execute()
        except Exception:
            return None
        return profile.get("emailAddress")

    if not ACCOUNT_PATH.exists():
        return None
    try:
        return json.loads(ACCOUNT_PATH.read_text(encoding="utf-8")).get("email")
    except (ValueError, OSError):
        return None


def sign_out() -> None:
    """Forget this machine's Google account. Does not revoke on Google's side."""
    TOKEN_PATH.unlink(missing_ok=True)
    ACCOUNT_PATH.unlink(missing_ok=True)
    reset_service_cache()


def is_signed_in() -> bool:
    """Whether Google is connected for whoever this call is for.

    On the server the answer is "is a credentials factory bound", not "does a
    token file exist on this host" — the host's own token, if any, belongs to
    nobody in particular and must never stand in for a user's.
    """
    context = agent_context.current()
    if context is not None:
        return context.google_credentials is not None
    return TOKEN_PATH.exists()
