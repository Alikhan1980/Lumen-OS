"""Authentication endpoints.

The security decisions all live here rather than in the GoTrue client, because
they need the whole picture: what the caller asked, what the upstream said, and
what it is safe to tell them.

Three run through everything below:

**Do not confirm whether an address has an account.** Signup, forgot-password
and resend-verification return the same message whether or not the address is
known. Login returns one message for a wrong password and for an unknown
address. The exception is deliberate and noted at the call site: a *correct*
password against an unverified account reports that specifically, because at
that point the caller has already proved they own the credentials.

**Count every attempt, on more than one axis.** Per IP catches one host
spraying many accounts; per account catches many hosts against one. A limiter
with only the first is defeated by a botnet, and one with only the second is
defeated by a list of addresses.

**Never mint a credential here.** Every token in this file was issued by GoTrue
and is verified by signature before it means anything. There is no code path
that constructs a session, elevates a role, or trusts a user id from a request.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from starlette.responses import JSONResponse

from ..deps import CurrentUser, ServiceDb, request_agent, request_ip
from ..errors import (
    GENERIC_RESET_SENT,
    GENERIC_VERIFY_SENT,
    INVALID_CREDENTIALS,
    AppError,
    RateLimited,
    Unauthorized,
)
from ..observability import record_event
from ..schemas import (
    ChangePasswordIn,
    ForgotPasswordIn,
    LoginIn,
    LogoutIn,
    RefreshIn,
    ResendVerificationIn,
    ResetPasswordIn,
    SignupIn,
)
from ..security import http as secure_http
from ..security import passwords, ratelimit
from ..security.passwords import PasswordRejected
from ..services import gotrue
from ..services.gotrue import GoTrueError, Session
from ..settings import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])

_VERIFY_SENT = "Check your email to confirm your address, then sign in."


class _ResponseCarrier(Exception):
    """Lets a handler return a specific response from inside an except block.

    Only used by /refresh, which has to both clear a cookie and report 401.
    Registered as an exception handler in main.py.
    """

    def __init__(self, response: Response):
        super().__init__("response")
        self.response = response


# Where the emails GoTrue sends point back to. Built from configuration, never
# from anything in the request -- a caller-supplied `redirect_to` is how you end
# up mailing your users a link to somebody else's site with a live token on it.
def _email_redirect(path: str) -> str:
    return f"{settings().app_url}{path}"


def _session_response(
    session: Session,
    *,
    remember: bool = True,
    status_code: int = 200,
    extra: dict | None = None,
) -> JSONResponse:
    """Return the access token in the body, the refresh token in a cookie.

    The split is the point. A browser client keeps the access token in memory
    where a reload loses it and page script can hold it only while running; the
    refresh token goes into an httpOnly, SameSite=Strict, path-scoped cookie
    that script cannot read at all. An XSS then buys an attacker the life of one
    access token, not a durable credential.

    The desktop client is not a browser and gets the refresh token in the body
    as well, because it stores it in the OS credential manager -- see
    agent/auth/session.py. It identifies itself with X-Lumen-Client.
    """
    payload = session.client_payload()
    if extra:
        payload.update(extra)

    response = JSONResponse(payload, status_code=status_code)
    if session.refresh_token:
        secure_http.set_refresh_cookie(
            response,
            session.refresh_token,
            # 30 days when remembered, or the life of the browser session.
            max_age=60 * 60 * 24 * 30 if remember else 0,
        )
    secure_http.issue_csrf(response)
    return response


def _wants_refresh_in_body(request: Request) -> bool:
    """True for the packaged desktop app, which has no usable cookie store."""
    return (request.headers.get("X-Lumen-Client") or "").strip().lower() == "desktop"


# --------------------------------------------------------------------- config


@router.get("/config")
async def config() -> dict:
    """What the sign-in screen needs to draw itself. No secrets -- see settings."""
    return settings().public_config()


# --------------------------------------------------------------------- signup


@router.post("/signup", status_code=202)
async def signup(
    body: SignupIn,
    request: Request,
    db: ServiceDb,
) -> Response:
    ip = request_ip(request)

    decision = await ratelimit.check(
        db,
        action="signup",
        ip=ip,
        limit=settings().signup_max_per_ip,
    )
    if not decision.allowed:
        raise RateLimited(decision.retry_after_seconds)

    # Policy check before the network call: a rejected password should cost the
    # auth server nothing, and the message we give is better than GoTrue's.
    try:
        password = passwords.validate(body.password, email=body.email, name=body.name)
    except PasswordRejected as exc:
        raise AppError(str(exc), code="weak_password", status_code=422) from exc

    try:
        session = await gotrue.anon().sign_up(
            email=body.email,
            password=password,
            display_name=body.name,
            redirect_to=_email_redirect("/auth/verified"),
        )
    except GoTrueError as exc:
        if exc.is_rate_limited:
            raise RateLimited(60) from exc
        if exc.is_weak_password:
            raise AppError(
                "Choose a stronger password.", code="weak_password", status_code=422
            ) from exc
        if exc.is_already_registered:
            # The same 202 an unused address gets. An attacker with a list of
            # addresses learns nothing about which of them are customers; the
            # real owner of the address gets an email either way (GoTrue sends
            # a "someone tried to sign up as you" notice), which is the only
            # party who should find out.
            gotrue.log_attempt("signup", body.email, "duplicate")
            await record_event(event="signup.duplicate", ip=ip, user_agent=request_agent(request)
            )
            return JSONResponse(
                {"status": "pending_verification", "message": _VERIFY_SENT}, status_code=202
            )
        raise

    await record_event(event="signup.created",
        user_id=session.user_id if session else None,
        ip=ip,
        user_agent=request_agent(request),
    )

    # With email confirmation on -- which is how the project should be
    # configured -- GoTrue returns no session and the user must click the link.
    if session is None or not session.access_token:
        return JSONResponse(
            {"status": "pending_verification", "message": _VERIFY_SENT}, status_code=202
        )

    return _session_response(
        session,
        status_code=201,
        extra={"status": "signed_in"},
    )


# ---------------------------------------------------------------------- login


@router.post("/login")
async def login(body: LoginIn, request: Request, db: ServiceDb) -> Response:
    ip = request_ip(request)
    config = settings()

    decision = await ratelimit.check_all(
        db,
        [
            {"action": "login", "ip": ip, "limit": config.login_max_per_ip},
            {
                "action": "login",
                "account": body.email,
                "limit": config.login_max_per_account,
            },
        ],
    )
    if not decision.allowed:
        await record_event(event="login.rate_limited", ip=ip, user_agent=request_agent(request)
        )
        raise RateLimited(decision.retry_after_seconds)

    try:
        session = await gotrue.anon().sign_in(email=body.email, password=body.password)
    except GoTrueError as exc:
        if exc.is_rate_limited:
            raise RateLimited(60) from exc
        if exc.is_not_verified:
            # Only reachable with the correct password, so this discloses
            # nothing to somebody who does not already hold the credentials --
            # and without it, a user who never clicked the link is stuck with
            # "incorrect email or password" for a password that is correct.
            gotrue.log_attempt("login", body.email, "unverified")
            raise AppError(
                "Confirm your email address first. We can send the link again.",
                code="email_not_verified",
                status_code=403,
            ) from exc
        if exc.is_invalid_credentials:
            gotrue.log_attempt("login", body.email, "rejected")
            await record_event(event="login.failed", ip=ip, user_agent=request_agent(request)
            )
            raise AppError(
                INVALID_CREDENTIALS, code="invalid_credentials", status_code=401
            ) from exc
        raise

    await record_event(event="login.success",
        user_id=session.user_id,
        ip=ip,
        user_agent=request_agent(request),
    )

    extra = {"status": "signed_in"}
    if _wants_refresh_in_body(request):
        extra["refresh_token"] = session.refresh_token
    return _session_response(session, remember=body.remember, extra=extra)


# -------------------------------------------------------------------- refresh


@router.post("/refresh")
async def refresh(body: RefreshIn, request: Request, db: ServiceDb) -> Response:
    """Trade a refresh token for a fresh access token.

    Two ways in. A browser sends the httpOnly cookie automatically, so this is
    the one endpoint that is CSRF-able and the one that checks a CSRF token.
    The desktop client posts the token in the body and skips the check, which is
    correct: there is no ambient credential for another origin to abuse.
    """
    token = (body.refresh_token or "").strip()
    from_cookie = False

    if not token:
        token = (request.cookies.get(secure_http.REFRESH_COOKIE) or "").strip()
        from_cookie = bool(token)

    if not token:
        raise Unauthorized()

    if from_cookie and not secure_http.csrf_ok(request):
        raise Unauthorized()

    ip = request_ip(request)
    decision = await ratelimit.check(db, action="refresh", ip=ip, limit=240)
    if not decision.allowed:
        raise RateLimited(decision.retry_after_seconds)

    try:
        session = await gotrue.anon().refresh(refresh_token=token)
    except GoTrueError as exc:
        # A refresh token that GoTrue rejects is spent, revoked, or forged.
        # Clear the cookie so the client stops retrying with a dead credential.
        response = JSONResponse(
            {
                "error": {
                    "code": "unauthenticated",
                    "message": "Your session has expired. Please sign in again.",
                }
            },
            status_code=401,
        )
        secure_http.clear_refresh_cookie(response)
        raise _ResponseCarrier(response) from exc

    extra = {"status": "signed_in"}
    if _wants_refresh_in_body(request):
        extra["refresh_token"] = session.refresh_token
    return _session_response(session, extra=extra)


# --------------------------------------------------------------------- logout


@router.post("/logout")
async def logout(body: LogoutIn, request: Request, user: CurrentUser) -> Response:
    """End this session, or every session.

    `scope="global"` is "log out of all devices": GoTrue revokes every refresh
    token for the user, so a stolen one stops working. Access tokens already
    issued stay valid until they expire -- they are stateless by design, and
    their lifetime is short for exactly this reason.
    """
    await gotrue.anon().sign_out(access_token=user.principal.token, scope=body.scope)

    await record_event(event="logout.global" if body.scope == "global" else "logout",
        user_id=user.user_id,
        ip=request_ip(request),
        user_agent=request_agent(request),
    )

    response = JSONResponse({"status": "signed_out"})
    secure_http.clear_refresh_cookie(response)
    return response


# ----------------------------------------------------------------- session/me


@router.get("/session")
async def session_state(user: CurrentUser) -> dict:
    """Who the bearer token belongs to, according to the token itself.

    Cheap: no round trip to the auth server, because the claims were verified
    on the way in. This is what the client calls on startup to decide whether to
    show the app or the sign-in screen.
    """
    row = await user.db.fetchrow(
        """
        select p.display_name, p.avatar_url, p.timezone, p.onboarded_at
          from public.profiles p
         where p.id = (select auth.uid())
        """
    )
    return {
        "user": {
            "id": user.user_id,
            "email": user.email,
            "email_verified": user.email_verified,
        },
        "profile": {
            "display_name": row["display_name"] if row else "",
            "avatar_url": row["avatar_url"] if row else None,
            "timezone": row["timezone"] if row else "UTC",
        },
        "onboarded": bool(row and row["onboarded_at"]),
    }


# ---------------------------------------------------------- email verification


@router.post("/verify/resend", status_code=202)
async def resend_verification(
    body: ResendVerificationIn, request: Request, db: ServiceDb
) -> dict:
    decision = await ratelimit.check(
        db,
        action="resend",
        account=body.email,
        limit=settings().resend_max_per_account,
    )
    if not decision.allowed:
        raise RateLimited(decision.retry_after_seconds)

    try:
        await gotrue.anon().resend_verification(
            email=body.email, redirect_to=_email_redirect("/auth/verified")
        )
    except GoTrueError as exc:
        # Includes "already confirmed" and "no such user". Both get the same
        # non-committal answer as success.
        if not exc.is_rate_limited:
            gotrue.log_attempt("verify.resend", body.email, "declined")

    return {"status": "sent", "message": GENERIC_VERIFY_SENT}


# -------------------------------------------------------------- password reset


@router.post("/password/forgot", status_code=202)
async def forgot_password(
    body: ForgotPasswordIn, request: Request, db: ServiceDb
) -> dict:
    """Start a reset. Always answers the same way.

    The reset token itself is GoTrue's: single-use, expiring, generated with a
    CSPRNG on its side. We do not create one, store one, or write one anywhere
    -- which is why "reset tokens must never appear in logs" is satisfied by
    construction rather than by a filter.
    """
    decision = await ratelimit.check(
        db,
        action="reset",
        account=body.email,
        limit=settings().reset_max_per_account,
    )
    if not decision.allowed:
        # Even the refusal is silent about whether the account exists: the
        # limiter is keyed on a hash of the address, and this response is
        # identical for a known and an unknown one.
        raise RateLimited(decision.retry_after_seconds)

    try:
        await gotrue.anon().send_reset(
            email=body.email, redirect_to=_email_redirect("/auth/reset")
        )
    except GoTrueError:
        gotrue.log_attempt("password.forgot", body.email, "declined")

    await record_event(event="password.reset_requested", ip=request_ip(request),
        user_agent=request_agent(request),
    )
    return {"status": "sent", "message": GENERIC_RESET_SENT}


@router.post("/password/reset")
async def reset_password(body: ResetPasswordIn, request: Request, db: ServiceDb) -> Response:
    """Finish a reset: emailed token in, new password set, signed in.

    The token is redeemed at GoTrue, which enforces single use and expiry. A
    replayed link fails there, not here.
    """
    ip = request_ip(request)
    decision = await ratelimit.check(db, action="reset_confirm", ip=ip, limit=20)
    if not decision.allowed:
        raise RateLimited(decision.retry_after_seconds)

    try:
        password = passwords.validate(body.password, email=body.email)
    except PasswordRejected as exc:
        raise AppError(str(exc), code="weak_password", status_code=422) from exc

    client = gotrue.anon()
    try:
        session = await client.verify_otp(email=body.email, token=body.token, kind="recovery")
    except GoTrueError as exc:
        raise AppError(
            "That reset link is invalid or has expired. Request a new one.",
            code="invalid_reset_token",
            status_code=400,
        ) from exc

    try:
        await client.update_password(access_token=session.access_token, password=password)
    except GoTrueError as exc:
        if exc.is_weak_password:
            raise AppError(
                "Choose a stronger password.", code="weak_password", status_code=422
            ) from exc
        raise

    # Every other session is now suspect: if the reset happened because someone
    # else had the old password, leaving their sessions alive defeats the point.
    await client.sign_out(access_token=session.access_token, scope="others")

    await record_event(event="password.reset_completed",
        user_id=session.user_id,
        ip=ip,
        user_agent=request_agent(request),
    )

    extra = {"status": "signed_in"}
    if _wants_refresh_in_body(request):
        extra["refresh_token"] = session.refresh_token
    return _session_response(session, extra=extra)


@router.post("/password/change")
async def change_password(
    body: ChangePasswordIn, request: Request, user: CurrentUser
) -> dict:
    """Change a password while signed in.

    The current password is re-checked by attempting a real sign-in with it,
    rather than trusting that holding a session is proof enough. A session can
    be an unlocked laptop; the password is the thing only the account holder
    should know.
    """
    if not user.email:
        raise AppError(
            "This account signs in with a connected provider and has no password.",
            code="no_password",
            status_code=400,
        )

    try:
        new_password = passwords.validate(body.new_password, email=user.email)
    except PasswordRejected as exc:
        raise AppError(str(exc), code="weak_password", status_code=422) from exc

    client = gotrue.anon()
    try:
        confirmed = await client.sign_in(email=user.email, password=body.current_password)
    except GoTrueError as exc:
        if exc.is_rate_limited:
            raise RateLimited(60) from exc
        raise AppError(
            "That is not your current password.",
            code="invalid_credentials",
            status_code=401,
        ) from exc

    try:
        await client.update_password(
            access_token=confirmed.access_token, password=new_password
        )
    except GoTrueError as exc:
        if exc.is_same_password:
            raise AppError(
                "That is already your password.", code="same_password", status_code=422
            ) from exc
        if exc.is_weak_password:
            raise AppError(
                "Choose a stronger password.", code="weak_password", status_code=422
            ) from exc
        raise

    # Sign every other device out, for the same reason as a reset.
    await client.sign_out(access_token=confirmed.access_token, scope="others")

    await record_event(event="password.changed",
        user_id=user.user_id,
        ip=request_ip(request),
        user_agent=request_agent(request),
    )
    return {
        "status": "changed",
        "message": "Password updated. Other devices have been signed out.",
    }


# --------------------------------------------------------------- social start


@router.get("/oauth/{provider}/start")
async def oauth_start(provider: str, request: Request) -> dict:
    """Where to send the browser for Google or Apple sign-in.

    Sign-in, not integration: this establishes *identity*. Connecting Gmail or
    Drive is a separate consent with separate scopes, handled in
    routers/integrations.py, so that someone who signs in with Google is not
    silently handing over their mailbox as well.
    """
    config = settings()
    available = config.public_config()["providers"]
    # "password" is in that map but is not a redirect flow.
    if provider not in {"google", "apple"} or not available.get(provider):
        raise AppError("That sign-in method is not available.", code="unknown_provider", status_code=404)

    return {
        "url": gotrue.anon().authorize_url(
            provider=provider, redirect_to=_email_redirect("/auth/callback")
        )
    }


# Registered on the app in main.py -- see the comment there.
async def response_carrier_handler(request: Request, exc: _ResponseCarrier) -> Response:
    return exc.response
