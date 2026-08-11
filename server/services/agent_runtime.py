"""Running the agent for one authenticated user.

This is the join between the API and the existing agent core, and the whole
point of it is that the agent gets an identity it cannot argue with.

How a turn is assembled:

1. The request's token is verified; that yields a user id. (`deps.py`)
2. Google credentials are fetched *for that id* from the token custody service,
   which reads one row selected by that id. (`google_oauth.credentials_for`)
3. The tool list is filtered to what this user's grants actually cover.
4. All three are bound into an `AgentContext` for the duration of the turn, and
   the existing `Agent` runs unchanged inside it.

Note what is missing from that list: at no point does a user id come from the
request body, from a header, from the conversation, or from the model. There is
no parameter a tool could accept that would change whose data is reached,
because credentials are resolved from the context rather than passed as
arguments. `assert_no_identity_parameters()` is a startup check that keeps it
that way -- if somebody later adds a tool with a `user_id` argument, the server
refuses to boot rather than shipping the hole.

The agent core itself is untouched. It streams, it calls tools, it asks for
approval; it simply does so inside a context that decides what "your mail"
means.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from agent import context as agent_context
from agent import registry
from agent.core import Agent, Callbacks
from agent.providers.base import ToolDef

from ..errors import AppError
from ..observability import logger, safe
from . import google_oauth, permissions

log = logger("agent-runtime")

# Tools that touch no third-party account and need no grant. Everything else is
# gated on the connection that provides it.
UNGATED_GROUPS = frozenset({"search", "browser", "files", "reminders"})

# Argument names a tool must never accept. A tool taking one of these would be
# taking an identity from the model, which is the one input that must never
# influence whose data is used.
FORBIDDEN_PARAMETERS = frozenset(
    {"user_id", "userid", "account_id", "owner_id", "tenant_id", "on_behalf_of", "as_user"}
)


def assert_no_identity_parameters() -> None:
    """Refuse to start if any tool accepts a caller-supplied identity.

    Called from the application factory. Cheap, runs once, and turns a
    catastrophic class of bug into a failed deploy.
    """
    registry.load_all()
    offenders: list[str] = []
    for tool in registry.all_tools():
        properties = (tool.input_schema or {}).get("properties") or {}
        for name in properties:
            if name.strip().lower() in FORBIDDEN_PARAMETERS:
                offenders.append(f"{tool.name}.{name}")

    if offenders:
        raise RuntimeError(
            "These tools accept an identity as an argument, which would let the "
            "model choose whose data to read: " + ", ".join(sorted(offenders)) + ". "
            "Resolve identity from agent.context instead."
        )


@dataclass(frozen=True)
class UserToolset:
    tools: list[ToolDef]
    allowed: frozenset[str]
    google_connected: bool
    google_account: str | None


async def build_context(*, user_id: str) -> tuple[agent_context.AgentContext, UserToolset]:
    """Assemble the identity and toolset for one user's turn."""
    registry.load_all()

    credentials_factory = None
    granted_scopes: list[str] = []
    google_account: str | None = None

    try:
        live = await google_oauth.credentials_for(user_id=user_id)
    except AppError:
        # Not connected, or needs reconnecting. Not an error for the turn: the
        # agent simply has no Google tools, and says so if asked to use one.
        live = None

    if live is not None:
        granted_scopes = list(live.scopes)
        google_account = live.account_email
        access_token = live.access_token

        def _make_credentials():
            """A Credentials object carrying only a short-lived access token.

            No refresh token and no client secret are handed to the Google
            client library here. If the token expires mid-turn the library
            cannot silently renew it -- which is the intended trade: renewal is
            the server's job, done against the encrypted store, and never
            something a tool can trigger on its own.
            """
            from google.oauth2.credentials import Credentials

            return Credentials(token=access_token)

        credentials_factory = _make_credentials

    allowed = _allowed_tool_names(granted_scopes)

    context = agent_context.AgentContext(
        user_id=user_id,
        google_credentials=credentials_factory,
        allowed_tools=allowed,
    )

    tools = [
        ToolDef(spec.name, spec.description, spec.input_schema)
        for spec in registry.all_tools()
        if spec.name in allowed
    ]

    log.info(
        "toolset built %s",
        safe(user_id=user_id, count=len(tools), provider="google" if live else "none"),
    )
    return context, UserToolset(
        tools=tools,
        allowed=allowed,
        google_connected=live is not None,
        google_account=google_account,
    )


def _allowed_tool_names(granted_scopes: list[str]) -> frozenset[str]:
    """Which tools this user's grants permit.

    Ungated groups are always in. Google tools are in only when the scopes
    behind them were actually granted -- so an agent whose user declined Gmail
    is not told that `gmail_send_email` exists. Filtering the tool list rather
    than refusing the call is the better shape: the model cannot be talked into
    attempting something it has never heard of, and the user is not shown a
    refusal for a thing they never enabled.
    """
    permitted = permissions.tools_for_scopes(granted_scopes)
    names = {
        tool.name
        for tool in registry.all_tools()
        if tool.group in UNGATED_GROUPS or tool.name in permitted
    }
    return frozenset(names)


async def run_turn(
    *,
    user_id: str,
    message: str,
    agent: Agent,
    callbacks: Callbacks,
) -> str:
    """Run one turn as `user_id`.

    The agent's blocking loop runs in a worker thread, and the context is bound
    *inside* that thread. `contextvars` are copied into a thread started by
    `asyncio.to_thread`, but binding within the target function keeps the
    lifetime obvious and guarantees the reset happens on the same stack that
    set it.
    """
    context, toolset = await build_context(user_id=user_id)
    agent.tools = toolset.tools

    def _run() -> str:
        with agent_context.bind(context):
            return agent.send(message)

    return await asyncio.to_thread(_run)
