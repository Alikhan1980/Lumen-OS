"""Profile, preferences, onboarding, and account deletion.

Every read and write in this file goes through the caller's scoped connection,
so the RLS policies decide which row is touched. That is why there is no
`where user_id = ...` in most of these statements and no ownership check in the
handlers: adding one would suggest the database was not already enforcing it,
and the two would eventually disagree.

`update` statements do carry `where id = auth.uid()` where the statement would
otherwise be an unqualified UPDATE. RLS would restrict it to the caller's rows
anyway, but an unqualified UPDATE that *looks* like it hits every row is a
hazard to the next person reading it.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..deps import CurrentUser, request_agent, request_ip
from ..errors import AppError, NotFound
from ..observability import record_event
from ..schemas import DeleteAccountIn, OnboardingIn, PreferencesIn, ProfileIn
from ..services import accounts, gotrue
from ..services.gotrue import GoTrueError

router = APIRouter(prefix="/api/account", tags=["account"])


@router.get("")
async def get_account(user: CurrentUser) -> dict:
    """Everything the Account screen renders, in one round trip."""
    profile = await user.db.fetchrow(
        """
        select display_name, avatar_url, timezone, onboarded_at, created_at
          from public.profiles
         where id = (select auth.uid())
        """
    )
    if profile is None:
        # The provisioning trigger should make this impossible. If it happens,
        # it is a data problem and not something to paper over with defaults.
        raise NotFound("Account not found.")

    preferences = await user.db.fetchrow(
        """
        select response_style, show_thinking, auto_approve_tools,
               email_notifications, reminder_push, weekly_digest
          from public.user_preferences
         where user_id = (select auth.uid())
        """
    )

    connections = await user.db.fetch(
        """
        select provider, account_email, scopes, status, connected_at, last_used_at
          from public.integration_connections
         where revoked_at is null
         order by provider
        """
    )

    return {
        "user": {
            "id": user.user_id,
            "email": user.email,
            "email_verified": user.email_verified,
            "created_at": profile["created_at"],
        },
        "profile": {
            "display_name": profile["display_name"],
            "avatar_url": profile["avatar_url"],
            "timezone": profile["timezone"],
        },
        "onboarded": bool(profile["onboarded_at"]),
        "preferences": dict(preferences) if preferences else {},
        # Never includes a token, an expiry, or anything that could be replayed.
        # The scope list is here because the permission screen is built from it.
        "connections": [
            {
                "provider": row["provider"],
                "account_email": row["account_email"],
                "scopes": list(row["scopes"] or []),
                "status": row["status"],
                "connected_at": row["connected_at"],
                "last_used_at": row["last_used_at"],
            }
            for row in connections
        ],
    }


@router.patch("/profile")
async def update_profile(body: ProfileIn, user: CurrentUser) -> dict:
    """Update name, avatar or timezone. Absent fields are left alone."""
    updates = body.model_dump(exclude_unset=True, exclude_none=True)
    if not updates:
        raise AppError("Nothing to update.", code="empty_update", status_code=400)

    # Column names come from the model's own field list, never from the request
    # body's keys -- `extra="forbid"` already rejects unknown fields, and this
    # makes the SQL safe even if that ever changed.
    allowed = {"display_name", "avatar_url", "timezone"}
    columns = [name for name in updates if name in allowed]
    assignments = ", ".join(f"{name} = ${index + 1}" for index, name in enumerate(columns))
    values = [updates[name] for name in columns]

    # S608: the interpolated fragment is column names filtered through the
    # `allowed` set above, never anything from the request. Every *value* is a
    # bound parameter.
    row = await user.db.fetchrow(
        f"""
        update public.profiles
           set {assignments}
         where id = (select auth.uid())
        returning display_name, avatar_url, timezone
        """,  # noqa: S608
        *values,
    )
    if row is None:
        raise NotFound("Account not found.")
    return {"profile": dict(row)}


@router.patch("/preferences")
async def update_preferences(body: PreferencesIn, user: CurrentUser) -> dict:
    updates = body.model_dump(exclude_unset=True, exclude_none=True)
    if not updates:
        raise AppError("Nothing to update.", code="empty_update", status_code=400)

    allowed = {
        "response_style",
        "show_thinking",
        "auto_approve_tools",
        "email_notifications",
        "reminder_push",
        "weekly_digest",
    }
    columns = [name for name in updates if name in allowed]
    values = [updates[name] for name in columns]

    # The columns are named in the INSERT as well as in the DO UPDATE. With
    # only the latter, a user whose preferences row is missing would get a row
    # of defaults inserted and their actual change silently dropped -- the
    # conflict never fires, so the SET never runs.
    placeholders = ", ".join(f"${index + 1}" for index in range(len(columns)))
    insert_columns = ", ".join(columns)
    assignments = ", ".join(f"{name} = excluded.{name}" for name in columns)

    # S608: as above -- identifiers from the allowlist, values bound.
    row = await user.db.fetchrow(
        f"""
        insert into public.user_preferences (user_id, {insert_columns})
             values ((select auth.uid()), {placeholders})
        on conflict (user_id) do update
           set {assignments}
        returning response_style, show_thinking, auto_approve_tools,
                  email_notifications, reminder_push, weekly_digest
        """,  # noqa: S608
        *values,
    )
    return {"preferences": dict(row) if row else {}}


@router.post("/onboarding")
async def complete_onboarding(body: OnboardingIn, user: CurrentUser) -> dict:
    """The three questions onboarding asks, and the flag that ends it.

    Integrations are not touched here on purpose: connecting Google is offered
    during onboarding but is never required to finish it. An agent with no
    integrations still has web search, its own reminders and a workspace.
    """
    if body.timezone:
        await user.db.execute(
            "update public.profiles set timezone = $1 where id = (select auth.uid())",
            body.timezone,
        )
    if body.response_style:
        await user.db.execute(
            """
            insert into public.user_preferences (user_id, response_style)
                 values ((select auth.uid()), $1)
            on conflict (user_id) do update set response_style = excluded.response_style
            """,
            body.response_style,
        )
    if body.complete:
        await user.db.execute(
            "update public.profiles set onboarded_at = now() where id = (select auth.uid())"
        )

    return {"onboarded": body.complete}


@router.get("/security")
async def security_history(user: CurrentUser) -> dict:
    """Recent security events on this account.

    Readable by its owner and nobody else -- the policy on auth_events is a
    select-only, own-rows-only one, and there is no write path from a user
    connection, so this list cannot be edited or cleared by whoever is holding
    the session.
    """
    rows = await user.db.fetch(
        """
        select event, ip_prefix, user_agent, created_at
          from public.auth_events
         where user_id = (select auth.uid())
         order by created_at desc
         limit 50
        """
    )
    return {"events": [dict(row) for row in rows]}


@router.delete("")
async def delete_account(body: DeleteAccountIn, request: Request, user: CurrentUser) -> dict:
    """Delete the account and everything attached to it.

    Order matters, and it is:

      1. re-check the password, so a borrowed session cannot do this;
      2. revoke every connected integration at its provider, so the grant is
         gone from the user's Google account and not merely from our database;
      3. delete the auth user, which cascades every table in the schema.

    Step 2 is best-effort. If Google is unreachable we still delete: the user
    asked to be erased, and refusing because a third party is down would be the
    wrong answer. The failure is recorded so it can be retried by hand, and the
    user is told in the response which grants they may want to remove
    themselves.
    """
    if not user.email:
        raise AppError(
            "Account deletion needs a password to confirm.", code="no_password", status_code=400
        )

    client = gotrue.anon()
    try:
        await client.sign_in(email=user.email, password=body.password)
    except GoTrueError as exc:
        raise AppError(
            "That password is not correct.", code="invalid_credentials", status_code=401
        ) from exc

    outcome = await accounts.delete_user_completely(
        user_id=user.user_id,
        ip=request_ip(request),
        user_agent=request_agent(request),
    )

    return {
        "status": "deleted",
        "revoked": outcome.revoked,
        # Named so the client can tell the user "we could not reach Google; you
        # may want to remove access at myaccount.google.com".
        "revocation_failed": outcome.failed,
    }


@router.get("/export")
async def export_account(user: CurrentUser) -> dict:
    """Everything this account holds, as JSON.

    Not a compliance feature bolted on: it runs entirely on the scoped
    connection, so it is also the sharpest possible test of the isolation
    guarantee. If RLS were wrong, this endpoint would be where it showed.
    """
    tables = {
        "profile": "select display_name, avatar_url, timezone, created_at from public.profiles",
        "preferences": "select * from public.user_preferences",
        "conversations": "select id, title, provider_id, model, created_at from public.conversations",
        "messages": "select conversation_id, role, content, created_at from public.messages",
        "tasks": "select id, title, notes, status, due_at, created_at from public.tasks",
        "reminders": (
            "select id, title, notes, due_utc, due_local, tz, recurrence, status, created_at "
            "from public.reminders"
        ),
        # Connections only. The secrets table is not reachable from here, which
        # is the reason an export cannot leak an OAuth token even by accident.
        "connections": (
            "select provider, account_email, scopes, status, connected_at "
            "from public.integration_connections where revoked_at is null"
        ),
    }

    export: dict = {}
    for name, query in tables.items():
        rows = await user.db.fetch(query)
        export[name] = [dict(row) for row in rows]

    await record_event(event="account.exported", user_id=user.user_id)
    return export
