"""Account deletion.

Its own module because it is the one operation that reaches across every table
and out to third parties, and because it must be readable end to end by
somebody checking that "delete my account" means what it says.

The order is: revoke outward, then delete inward. Revoking first means that if
the delete fails halfway, the worst outcome is an account that still exists with
its integrations disconnected -- recoverable, and visible to the user. Deleting
first and failing to revoke would leave live grants on the user's Google account
pointing at an application that no longer has a record of them, which nobody
can then clean up from our side.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import db
from ..observability import logger, record_event, safe
from . import google_oauth

log = logger("accounts")


@dataclass
class DeletionOutcome:
    revoked: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


async def delete_user_completely(
    *, user_id: str, ip: str | None = None, user_agent: str | None = None
) -> DeletionOutcome:
    """Revoke every integration, then delete the auth user.

    Deleting the auth user cascades: every table in the schema references
    `auth.users(id)` with `on delete cascade`, so profiles, preferences,
    conversations, messages, tasks, reminders, connections and the encrypted
    secrets all go in the same transaction Postgres runs for the delete. No
    table is enumerated here on purpose -- a list in code is a list that goes
    stale the next time somebody adds a table.
    """
    outcome = DeletionOutcome()

    async with db.service_connection(reason="account deletion: list integrations") as connection:
        rows = await connection.fetch(
            "select connection_id, provider from public.connections_to_revoke($1)", user_id
        )

    for row in rows:
        provider = row["provider"]
        try:
            await google_oauth.revoke_connection(
                connection_id=str(row["connection_id"]), user_id=user_id
            )
            outcome.revoked.append(provider)
        except Exception as exc:
            # A third party being down must not trap someone in an account they
            # asked to leave. Recorded, reported to the user, deletion proceeds.
            log.warning(
                "could not revoke on delete %s",
                safe(user_id=user_id, provider=provider, reason=type(exc).__name__),
            )
            outcome.failed.append(provider)

    await record_event(
        event="account.deleting",
        user_id=user_id,
        ip=ip,
        user_agent=user_agent,
        detail={"provider": ",".join(outcome.revoked) or "none"},
    )

    # Last, and irreversible. Everything above still leaves an account the user
    # could sign into; after this there is nothing to sign into.
    from .gotrue import admin

    await admin().delete_user(user_id=user_id)

    # The auth_events rows for this user survive with user_id set to null
    # (`on delete set null`), so the security log keeps its shape for anyone
    # investigating an incident, without still naming a deleted person.
    log.info("account deleted %s", safe(user_id=user_id, outcome="ok"))
    return outcome
