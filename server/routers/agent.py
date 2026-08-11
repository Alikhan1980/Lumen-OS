"""The agent endpoint.

Everything a turn touches -- the conversation it appends to, the Google account
its tools reach, the reminders it can see -- is resolved from the verified user
on the request. The client sends a message and, optionally, which of *its own*
conversations to continue. It cannot send a user id, and the model cannot
produce one that matters: see services/agent_runtime.py.

The conversation id is the one identifier that does arrive from the client, and
it is handled the way every such identifier should be. It is used in a query on
the caller's scoped connection, so a guessed id belonging to somebody else
simply matches no row, and the endpoint answers 404 rather than 403 -- which
would otherwise confirm the id exists.

**There is deliberately no endpoint here that runs a turn yet.** The identity
half is done and tested -- `agent_runtime.build_context` resolves a user's
credentials and toolset, and `agent_runtime.run_turn` binds them around the
existing agent loop -- but running a turn server-side needs one product
decision that authentication does not: where the AI provider key comes from.
Today it lives in the OS credential store on the user's own machine, which is
what "bring your own key" means and is the basis of the claim in PRIVACY.md
that no request passes through a server of ours. Serving turns from here means
either per-user keys held server-side (another encrypted secret to custody, and
another thing to be careful with) or a single key the operator pays for and
meters. Both are defensible; neither is an auth question. Until it is decided,
the desktop app keeps running turns locally, now under an authenticated
identity.
"""

from __future__ import annotations

from fastapi import APIRouter

from ..deps import VerifiedUser
from ..errors import NotFound
from ..observability import logger
from ..services import agent_runtime

log = logger("agent")

router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.get("/capabilities")
async def capabilities(user: VerifiedUser) -> dict:
    """What this user's agent can currently do.

    Built from their grants, so the answer differs per user. The client renders
    it so somebody can see why the agent said it cannot read their mail.
    """
    _, toolset = await agent_runtime.build_context(user_id=user.user_id)
    return {
        "google_connected": toolset.google_connected,
        "google_account": toolset.google_account,
        "tools": sorted(toolset.allowed),
    }


@router.get("/conversations")
async def list_conversations(user: VerifiedUser) -> dict:
    rows = await user.db.fetch(
        """
        select id, title, provider_id, model, created_at, updated_at
          from public.conversations
         where archived_at is null
         order by updated_at desc
         limit 100
        """
    )
    return {"conversations": [dict(row) for row in rows]}


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, user: VerifiedUser) -> dict:
    """One conversation and its messages.

    No ownership check in this handler, and none is needed: the connection
    cannot see another user's conversation, so `where id = $1` either finds the
    caller's own row or finds nothing.
    """
    conversation = await user.db.fetchrow(
        """
        select id, title, provider_id, model, created_at, updated_at
          from public.conversations
         where id = $1::uuid
        """,
        conversation_id,
    )
    if conversation is None:
        raise NotFound("Conversation not found.")

    messages = await user.db.fetch(
        """
        select role, content, blocks, created_at
          from public.messages
         where conversation_id = $1::uuid
         order by id
        """,
        conversation_id,
    )
    return {
        "conversation": dict(conversation),
        "messages": [dict(row) for row in messages],
    }


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, user: VerifiedUser) -> dict:
    deleted = await user.db.fetchval(
        "delete from public.conversations where id = $1::uuid returning id",
        conversation_id,
    )
    if deleted is None:
        raise NotFound("Conversation not found.")
    return {"status": "deleted"}


@router.post("/conversations")
async def create_conversation(user: VerifiedUser) -> dict:
    """Start a conversation.

    `user_id` is written from `auth.uid()` rather than from anything the client
    sent, and the insert policy would reject any other value anyway.
    """
    row = await user.db.fetchrow(
        """
        insert into public.conversations (user_id, title)
             values ((select auth.uid()), '')
        returning id, title, created_at
        """
    )
    return {"conversation": dict(row)}
