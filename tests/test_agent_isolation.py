"""The agent must only ever reach the data of the user whose turn it is.

Three distinct claims are tested here, because they fail in different ways:

1. **The model cannot name a user.** No registered tool accepts an identity as
   an argument, so there is no string the model can emit that redirects a tool
   at somebody else. This is checked structurally, over the real registry.
2. **Concurrent turns do not bleed.** Two users' turns run at the same time in
   one process; each must see its own binding throughout. This is the failure a
   module-level global would produce, and it would be intermittent and awful.
3. **Grants decide the toolset.** A user who has not connected Gmail is not
   offered Gmail tools at all -- the model never learns they exist.
"""

from __future__ import annotations

import asyncio

import pytest

from agent import context as agent_context
from agent import registry
from server.services import permissions
from server.services.agent_runtime import (
    FORBIDDEN_PARAMETERS,
    assert_no_identity_parameters,
)


def test_no_tool_accepts_a_caller_supplied_identity():
    """The structural guarantee behind "the AI cannot specify another user's ID".

    A tool with a `user_id` parameter would take an identity from the model.
    This is the check that runs at server startup; here it runs over the real
    registry so the suite fails the moment such a tool is added.
    """
    assert_no_identity_parameters()

    registry.load_all()
    for tool in registry.all_tools():
        properties = (tool.input_schema or {}).get("properties") or {}
        for name in properties:
            assert name.lower() not in FORBIDDEN_PARAMETERS, f"{tool.name} takes {name}"


def test_the_check_actually_catches_a_bad_tool(monkeypatch):
    """A test for the test: prove the startup guard is not vacuous."""
    from agent.registry import Tool

    bad = Tool(
        name="evil_read_mail",
        description="",
        input_schema={"type": "object", "properties": {"user_id": {"type": "string"}}},
        handler=lambda **_: None,
        group="gmail",
    )
    monkeypatch.setattr(registry, "all_tools", lambda: [bad])
    with pytest.raises(RuntimeError, match="user_id"):
        assert_no_identity_parameters()


def test_context_is_absent_by_default():
    """No binding means the desktop path, unchanged."""
    assert agent_context.current() is None
    with pytest.raises(agent_context.NoUserBound):
        agent_context.require()


def test_binding_is_restored_on_exit():
    outer = agent_context.AgentContext(user_id="outer")
    inner = agent_context.AgentContext(user_id="inner")

    with agent_context.bind(outer):
        assert agent_context.require().user_id == "outer"
        with agent_context.bind(inner):
            assert agent_context.require().user_id == "inner"
        assert agent_context.require().user_id == "outer"
    assert agent_context.current() is None


def test_binding_is_restored_after_an_exception():
    """A failed turn must not leave an identity bound to a pooled worker."""
    with pytest.raises(ValueError), agent_context.bind(agent_context.AgentContext(user_id="a")):
        raise ValueError("turn blew up")
    assert agent_context.current() is None


async def test_concurrent_turns_do_not_see_each_others_identity():
    """The failure mode a process-wide global would have, made deterministic.

    Each task binds its own user, yields control at a point where the other task
    is guaranteed to run, and then re-reads the binding. A shared global would
    show the other user's id after the await.
    """
    observed: dict[str, list[str]] = {"a": [], "b": []}

    async def turn(label: str, user_id: str) -> None:
        with agent_context.bind(agent_context.AgentContext(user_id=user_id)):
            observed[label].append(agent_context.require().user_id)
            await asyncio.sleep(0)  # hand control to the other task
            observed[label].append(agent_context.require().user_id)
            await asyncio.sleep(0)
            observed[label].append(agent_context.require().user_id)

    await asyncio.gather(turn("a", "user-a"), turn("b", "user-b"))

    assert observed["a"] == ["user-a"] * 3
    assert observed["b"] == ["user-b"] * 3


async def test_binding_survives_into_a_worker_thread():
    """The agent loop is blocking and runs via `to_thread`; the context must follow."""
    seen: list[str | None] = []

    def blocking_work() -> None:
        seen.append(agent_context.user_id())

    with agent_context.bind(agent_context.AgentContext(user_id="user-a")):
        await asyncio.to_thread(blocking_work)

    assert seen == ["user-a"]


# ------------------------------------------------------- grants gate the tools


def test_no_google_grant_means_no_google_tools():
    allowed = permissions.tools_for_scopes([])
    assert allowed == set()


def test_read_grant_does_not_confer_send():
    """Least privilege, at the level the model can see.

    A user who granted "read my email" must not have `gmail_send_email` in the
    tool list -- not refused when called, absent entirely.
    """
    read_only = permissions.scopes_for(["gmail.read"])
    allowed = permissions.tools_for_scopes(read_only)

    assert "gmail_search" in allowed
    assert "gmail_read_thread" in allowed
    assert "gmail_send_email" not in allowed
    assert "drive_search_files" not in allowed
    assert "calendar_create_event" not in allowed


def test_capabilities_are_derived_from_what_was_granted_not_requested():
    """A user can untick items on Google's consent screen; the UI must follow."""
    requested = permissions.scopes_for(["gmail.read", "gmail.send"])
    granted = [s for s in requested if "gmail.send" not in s]

    capabilities = permissions.capabilities_from_scopes(granted)
    assert "gmail.read" in capabilities
    assert "gmail.send" not in capabilities


def test_default_capabilities_request_nothing_outbound():
    """First connect asks for reading only -- nothing that leaves the account."""
    outbound = {
        capability.key
        for capability in permissions.GOOGLE_CAPABILITIES
        if capability.outbound
    }
    assert not (set(permissions.DEFAULT_CAPABILITIES) & outbound)


def test_ungated_tools_are_available_without_any_connection():
    """Search, browser, workspace files and the app's own reminders need no grant."""
    from server.services.agent_runtime import _allowed_tool_names

    registry.load_all()
    allowed = _allowed_tool_names([])

    assert "web_search" in allowed
    assert "create_reminder" in allowed
    assert "file_read" in allowed
    # And nothing Google.
    assert not any(name.startswith(("gmail_", "drive_", "calendar_", "contacts_")) for name in allowed)


def test_every_gated_tool_belongs_to_some_capability():
    """A Google tool nobody can reach is a bug; so is one nobody has to grant.

    Catches the case where a tool is added to a gated group and quietly becomes
    unreachable, and the reverse -- a capability naming a tool that no longer
    exists.
    """
    registry.load_all()
    from server.services.agent_runtime import UNGATED_GROUPS

    registered = {tool.name for tool in registry.all_tools()}
    gated = {tool.name for tool in registry.all_tools() if tool.group not in UNGATED_GROUPS}
    covered = {name for capability in permissions.GOOGLE_CAPABILITIES for name in capability.tools}

    assert not (gated - covered), f"gated but ungrantable: {sorted(gated - covered)}"
    assert not (covered - registered), f"named but not registered: {sorted(covered - registered)}"
