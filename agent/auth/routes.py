"""The loopback endpoints the chat page calls to sign in.

These sit between the page and the API server, and the indirection is the
security design rather than plumbing: because the page talks to localhost and
localhost talks to the API, the access token lives in this process and never in
the browser. There is no `localStorage` to steal from and no token in a URL.

Each handler returns plain data the page can render. Errors come back in the
same envelope the API uses, so the page has one error shape to handle:

    {"error": {"code": "...", "message": "..."}}

Every endpoint is behind the page's existing access key check, the same as the
rest of the loopback API -- see `_authorized` in agent/web.py.
"""

from __future__ import annotations

import contextlib
import json
import webbrowser

from ..logs import logger
from .session import AuthError, Session

log = logger("auth-routes")


def _error(message: str, code: str = "error") -> tuple[int, dict]:
    return 400, {"error": {"code": code, "message": message}}


def _status_for(code: str) -> int:
    return {
        "unauthenticated": 401,
        "invalid_credentials": 401,
        "email_not_verified": 403,
        "forbidden": 403,
        "not_found": 404,
        "rate_limited": 429,
        "network": 503,
        "service_unavailable": 503,
    }.get(code, 400)


def handle(session: Session, path: str, method: str, body: dict) -> tuple[int, dict]:
    """Route one call. Returns (status, payload).

    A flat dispatch rather than a framework: this is nine endpoints inside a
    desktop app, and the existing loopback server is a `BaseHTTPRequestHandler`
    with no routing of its own.
    """
    try:
        return _dispatch(session, path, method, body)
    except AuthError as exc:
        return _status_for(exc.code), {"error": {"code": exc.code, "message": exc.message}}
    except Exception as exc:
        # Never let an internal error reach the page as a traceback.
        log.exception("auth route failed: %s", type(exc).__name__)
        return 500, {
            "error": {
                "code": "internal_error",
                "message": "Something went wrong. Please try again.",
            }
        }


def _dispatch(session: Session, path: str, method: str, body: dict) -> tuple[int, dict]:
    if path == "/api/auth/state" and method == "GET":
        return 200, _state(session)

    if path == "/api/auth/signup":
        result = session.sign_up(
            name=str(body.get("name") or "").strip(),
            email=str(body.get("email") or "").strip(),
            password=str(body.get("password") or ""),
        )
        # With email confirmation off the server signs the user straight in, so
        # return the same shape /state does and let the page go to onboarding.
        # With it on there is no session yet and the page shows "check your
        # email" instead.
        if session.is_signed_in():
            return 200, _state(session)
        return 200, {
            "signed_in": False,
            "status": result.get("status", "pending_verification"),
            "message": result.get("message", ""),
        }

    if path == "/api/auth/login":
        session.sign_in(
            email=str(body.get("email") or "").strip(),
            password=str(body.get("password") or ""),
            remember=bool(body.get("remember", True)),
        )
        return 200, _state(session)

    if path == "/api/auth/logout":
        session.sign_out(everywhere=bool(body.get("everywhere")))
        return 200, {"signed_in": False}

    if path == "/api/auth/resend":
        email = str(body.get("email") or "").strip()
        if not email:
            return _error("Enter your email address.", "invalid_request")
        return 200, session.resend_verification(email)

    if path == "/api/auth/forgot":
        email = str(body.get("email") or "").strip()
        if not email:
            return _error("Enter your email address.", "invalid_request")
        return 200, session.forgot_password(email)

    if path == "/api/auth/password":
        # The server's own wording is passed through rather than replaced: it
        # is the side that knows a change also signs the other devices out,
        # and that is the part the user needs told.
        return 200, session.change_password(
            current=str(body.get("current_password") or ""),
            new=str(body.get("new_password") or ""),
        )

    if path == "/api/auth/onboarding":
        payload = {k: v for k, v in body.items() if k in {"timezone", "response_style", "complete"}}
        session.api("POST", "/api/account/onboarding", payload)
        session.load_profile()
        return 200, _state(session)

    if path == "/api/auth/oauth":
        provider = str(body.get("provider") or "").strip().lower()
        if provider not in {"google", "apple"}:
            return _error("Unknown sign-in method.", "unknown_provider")
        url = session.oauth_url(provider)
        if not url:
            return _error("That sign-in method is not available.", "unknown_provider")
        # The system browser, not an embedded view: a user has to be able to
        # see the address bar to know they are typing their password into
        # Google and not into us.
        webbrowser.open(url)
        return 200, {"opened": True}

    return 404, {"error": {"code": "not_found", "message": "Not found."}}


def _state(session: Session) -> dict:
    """What the page needs to choose a screen. Carries no token, ever."""
    if not session.is_signed_in():
        # A returning user is signed in already and does not know it yet: the
        # refresh token is in the credential store and this is the first call
        # that asks. `restore` tries once per process and is cheap after that,
        # so this stays a plain check rather than a startup step the page has
        # to wait for. A failure here is not an error -- it means "not signed
        # in", which is exactly what the state below will say.
        with contextlib.suppress(AuthError):
            session.restore()

    state = session.state()

    try:
        state["config"] = session.config()
    except AuthError:
        # The sign-in screen still works with password auth if the API is
        # briefly unreachable; it just cannot draw the federated buttons.
        state["config"] = {"providers": {"password": True}}

    account = state.get("account") or {}
    state["needs_onboarding"] = bool(state.get("signed_in")) and not account.get("onboarded")
    return state


def is_auth_path(path: str) -> bool:
    return path.startswith("/api/auth/")


def json_bytes(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")
