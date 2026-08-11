"""HTTP-level protections: response headers, HTTPS enforcement, and CSRF.

Three separate concerns, kept together because they are all "things the edge of
the application does to every request" and all easy to leave half-applied when
they live next to the routes they protect.

On CSRF specifically: the API's primary authentication is a bearer token in an
`Authorization` header, which a cross-site form post cannot set -- so most of
this surface is not CSRF-able by construction. The exception is the refresh
cookie, which the browser *does* attach automatically. That one endpoint gets a
double-submit token, and the cookie is `SameSite=Strict` as well, so the attack
has to defeat both.
"""

from __future__ import annotations

import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..settings import settings

CSRF_COOKIE = "lumen_csrf"
CSRF_HEADER = "X-Lumen-CSRF"
REFRESH_COOKIE = "lumen_refresh"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Headers on every response, including error responses.

    Applied as middleware rather than per-route so a handler that raises before
    it returns still gets them -- an error page is as good a place to inject
    script as any other.
    """

    async def dispatch(self, request: Request, call_next):
        config = settings()

        if config.require_https and request.url.scheme != "https":
            # Behind a terminating proxy this is decided by the forwarded proto,
            # which Uvicorn applies when run with --proxy-headers.
            return JSONResponse(
                {"error": {"code": "https_required", "message": "Use HTTPS."}},
                status_code=400,
            )

        response: Response = await call_next(request)

        # This service returns JSON and never HTML, so the CSP can be maximally
        # restrictive: nothing is allowed to load at all.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=()"
        )
        # Nothing here should ever sit in a shared cache, and several responses
        # carry tokens.
        response.headers.setdefault("Cache-Control", "no-store, private")
        response.headers.setdefault("Pragma", "no-cache")

        if config.require_https:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains; preload",
            )

        return response


def set_refresh_cookie(response: Response, refresh_token: str, max_age: int) -> None:
    """Store the refresh token where page JavaScript cannot read it.

    `httponly` is the point: an XSS in the client can still *use* the cookie by
    calling /api/auth/refresh, but it cannot exfiltrate the token itself, which
    is the difference between an attacker with a foothold while the page is open
    and an attacker with a durable credential.
    """
    config = settings()
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=max_age,
        httponly=True,
        secure=config.require_https,
        samesite="strict",
        path="/api/auth",
        domain=config.cookie_domain or None,
    )


def clear_refresh_cookie(response: Response) -> None:
    config = settings()
    response.delete_cookie(
        REFRESH_COOKIE,
        path="/api/auth",
        domain=config.cookie_domain or None,
    )


def issue_csrf(response: Response) -> str:
    """Mint a CSRF token and put it in a readable cookie.

    Deliberately *not* httponly: the client has to read it to echo it back in a
    header. That is safe -- knowing the token is useless to another origin,
    which cannot read the cookie at all.
    """
    token = secrets.token_urlsafe(32)
    config = settings()
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=60 * 60 * 24 * 30,
        httponly=False,
        secure=config.require_https,
        samesite="strict",
        path="/",
        domain=config.cookie_domain or None,
    )
    return token


def csrf_ok(request: Request) -> bool:
    """Double-submit check, for the endpoints that authenticate by cookie.

    A cross-origin caller can cause the cookie to be sent but cannot read it, so
    it cannot produce a matching header. Compared in constant time out of habit
    rather than necessity.
    """
    cookie = request.cookies.get(CSRF_COOKIE) or ""
    header = request.headers.get(CSRF_HEADER) or ""
    if not cookie or not header:
        return False
    return secrets.compare_digest(cookie, header)
