"""The error envelope, and the rule that nothing internal crosses it.

Every failure leaves this service as the same shape:

    {"error": {"code": "invalid_credentials", "message": "Incorrect email or password."}}

`code` is a stable string for the client to branch on. `message` is written for
a person and is safe to display verbatim -- it never contains a stack trace, a
database error, a provider response, an internal id, or a hint about which half
of a credential was wrong.

The catch-all handler is the important part. Any exception that is *not* one of
ours becomes a flat 500 with a generic message and a request id; the real
exception goes to the log, where the redacting filter has already been applied.
Without that handler, FastAPI's default behaviour in a misconfigured deployment
is to render a traceback into the response body.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from .observability import logger, safe

log = logger("errors")


class AppError(Exception):
    """A failure with a message that is meant to be shown to the user."""

    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code

    def payload(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


class Unauthorized(AppError):
    status_code = 401
    code = "unauthenticated"

    def __init__(self, message: str = "Your session has expired. Please sign in again."):
        super().__init__(message)


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"

    def __init__(self, message: str = "You do not have access to that."):
        super().__init__(message)


class NotFound(AppError):
    """Also the correct answer for "exists, but is not yours".

    Returning 403 there would confirm the resource exists, which is a working
    oracle for enumerating other users' ids. 404 says nothing either way.
    """

    status_code = 404
    code = "not_found"

    def __init__(self, message: str = "Not found."):
        super().__init__(message)


class EmailNotVerified(AppError):
    status_code = 403
    code = "email_not_verified"

    def __init__(self, message: str = "Verify your email address to use this."):
        super().__init__(message)


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, retry_after: int, message: str = "Too many attempts. Try again shortly."):
        super().__init__(message)
        self.retry_after = retry_after


class UpstreamUnavailable(AppError):
    status_code = 503
    code = "service_unavailable"

    def __init__(self, message: str = "Something went wrong. Please try again."):
        super().__init__(message)


# Messages used in more than one place, kept together so the wording of the
# non-committal ones cannot drift apart. Two endpoints that phrase "we may or
# may not have sent you an email" differently are an enumeration oracle.
INVALID_CREDENTIALS = "Incorrect email or password."
GENERIC_RESET_SENT = (
    "If an account exists for that address, a password reset link is on its way."
)
GENERIC_VERIFY_SENT = (
    "If that address needs verifying, a new link is on its way."
)
GENERIC_FAILURE = "Something went wrong. Please try again."


def install(app: FastAPI) -> None:
    """Attach the handlers. Called once from the application factory."""

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        headers = {}
        if isinstance(exc, RateLimited):
            headers["Retry-After"] = str(exc.retry_after)
        return JSONResponse(exc.payload(), status_code=exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic's default body echoes the offending input back. For a login
        # form that means the password appears in the response and, if anything
        # logs responses, in a log. Report the field names only.
        fields = sorted(
            {
                str(part)
                for error in exc.errors()
                for part in error.get("loc", ())
                if part not in ("body", "query", "path")
            }
        )
        detail = f"Check these fields: {', '.join(fields)}." if fields else "Check your input."
        return JSONResponse(
            {"error": {"code": "invalid_request", "message": detail}},
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        message = {
            401: "Your session has expired. Please sign in again.",
            403: "You do not have access to that.",
            404: "Not found.",
            405: "That is not something you can do here.",
        }.get(exc.status_code, GENERIC_FAILURE)
        code = {401: "unauthenticated", 403: "forbidden", 404: "not_found"}.get(
            exc.status_code, "error"
        )
        return JSONResponse({"error": {"code": code, "message": message}}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # The one place a bug becomes a response. The client gets a reference it
        # can quote; the detail stays here.
        reference = uuid.uuid4().hex[:12]
        log.exception(
            "unhandled error %s",
            safe(request_id=reference, path=request.url.path, method=request.method),
        )
        return JSONResponse(
            {
                "error": {
                    "code": "internal_error",
                    "message": GENERIC_FAILURE,
                    "reference": reference,
                }
            },
            status_code=500,
        )
