"""Connect, inspect, and disconnect integrations.

Nothing in this file returns a token, and nothing accepts a user id. The user is
always `user.user_id`, which came out of a signature check; the connect flow
carries its own identity in a server-side `state` row rather than in anything
the browser hands back.

The callback is the one endpoint here with no authentication on it, because the
request arriving is a redirect from Google and carries whatever session the
user's browser happens to have -- which may be none, on a different browser, or
belong to somebody else on a shared machine. Trusting it would be the bug. The
state row is the authentication: it was written under a verified user id and it
is single-use.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request
from starlette.responses import RedirectResponse

from ..deps import VerifiedUser, request_agent, request_ip
from ..errors import AppError, NotFound
from ..observability import record_event
from ..schemas import Strict
from ..services import google_oauth, permissions
from ..settings import settings

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


class ConnectIn(Strict):
    # Which capabilities to ask Google for. Omitted means the read-only default
    # set in services/permissions.py -- least privilege unless asked otherwise.
    capabilities: list[str] | None = None


@router.get("")
async def list_integrations(user: VerifiedUser) -> dict:
    """What is connected, and exactly what it can do.

    Read on the user's own scoped connection, so the RLS policy is what decides
    which rows come back. `integration_secrets` is not joined and could not be:
    the `authenticated` role has no grant on that table at all.
    """
    rows = await user.db.fetch(
        """
        select provider, account_email, scopes, status, connected_at,
               last_used_at, needs_reauth_at
          from public.integration_connections
         where revoked_at is null
         order by provider
        """
    )

    granted: dict[str, list[str]] = {
        row["provider"]: list(row["scopes"] or []) for row in rows
    }
    connected = {
        row["provider"]: {
            "provider": row["provider"],
            "account_email": row["account_email"],
            "status": row["status"],
            "connected_at": row["connected_at"],
            "last_used_at": row["last_used_at"],
            "needs_reauth": row["status"] == "needs_reauth",
        }
        for row in rows
    }

    available = [
        {
            "provider": "google",
            "label": "Google",
            "description": "Gmail, Calendar, Drive, Contacts and Tasks.",
            "configured": bool(settings().google_client_id),
            "connection": connected.get("google"),
            # The full menu with a `granted` flag on each entry, so the screen
            # can offer capabilities that are not yet held without needing to
            # know the catalogue itself.
            "capabilities": permissions.describe(granted.get("google", [])),
        }
    ]
    return {"integrations": available}


@router.post("/google/connect")
async def connect_google(body: ConnectIn, user: VerifiedUser, request: Request) -> dict:
    """Start a Google connect. Returns a URL for the client to open.

    Verified accounts only. An unverified address means we do not know that the
    person holding this session owns it, and attaching a real mailbox to it
    would be handing somebody else's data to whoever guessed the address.
    """
    url = await google_oauth.start_connect(
        user_id=user.user_id, capabilities=body.capabilities
    )
    await record_event(
        event="integration.connect_started",
        user_id=user.user_id,
        ip=request_ip(request),
        user_agent=request_agent(request),
        detail={"provider": "google"},
    )
    return {"url": url}


@router.get("/google/callback")
async def google_callback(
    request: Request,
    state: Annotated[str, Query(max_length=512)] = "",
    code: Annotated[str, Query(max_length=2048)] = "",
    error: Annotated[str, Query(max_length=200)] = "",
) -> RedirectResponse:
    """Google's redirect target. Unauthenticated by necessity -- see module docs.

    Always redirects back into the app rather than rendering anything: this URL
    is in the browser's history and in the referrer of whatever loads next, so
    it should not be a page, and it must not carry the outcome in a way that
    could be replayed.
    """
    app_url = settings().app_url

    if error:
        # The user pressed Cancel on Google's consent screen, most often.
        return RedirectResponse(f"{app_url}/settings/integrations?connect=cancelled", status_code=303)

    if not state or not code:
        return RedirectResponse(f"{app_url}/settings/integrations?connect=failed", status_code=303)

    try:
        result = await google_oauth.complete_connect(state=state, code=code)
    except AppError:
        return RedirectResponse(f"{app_url}/settings/integrations?connect=failed", status_code=303)

    await record_event(event="integration.connected", detail={"provider": "google"})
    # No account address in the query string: it would end up in browser history
    # and in the referrer header of the next request.
    _ = result
    return RedirectResponse(f"{app_url}/settings/integrations?connect=ok", status_code=303)


@router.post("/google/reauthorize")
async def reauthorize_google(body: ConnectIn, user: VerifiedUser) -> dict:
    """Send the user back through consent, keeping the existing connection row.

    Used when a grant has gone stale, and when adding a capability: Google
    issues a token for the union of what is asked for and what is already held
    (`include_granted_scopes`), so this widens a grant without dropping the
    parts already in place.
    """
    url = await google_oauth.start_connect(user_id=user.user_id, capabilities=body.capabilities)
    return {"url": url}


@router.delete("/google")
async def disconnect_google(user: VerifiedUser, request: Request) -> dict:
    """Disconnect Google: revoke at Google, then delete the stored credential.

    The effect is immediate for the agent as well. Tool access is resolved per
    request through `credentials_for`, which reads the live connection row --
    once this returns, the next tool call finds nothing and the Google tools are
    no longer offered to the model at all.
    """
    disconnected = await google_oauth.disconnect(user_id=user.user_id, provider="google")
    if not disconnected:
        raise NotFound("No Google account is connected.")

    await record_event(
        event="integration.disconnected",
        user_id=user.user_id,
        ip=request_ip(request),
        user_agent=request_agent(request),
        detail={"provider": "google"},
    )
    return {"status": "disconnected"}


@router.get("/permissions")
async def permission_catalogue() -> dict:
    """Every capability the app can ask for, whether or not anything is connected.

    Public: it is a description of the product, not of any user. Useful for a
    privacy page and for the consent screen shown before a connect starts.
    """
    return {"google": permissions.describe([])}
