"""Tool registry.

Tool modules declare their capabilities with the @tool decorator; the registry
turns those into Anthropic tool definitions and dispatches calls back to the
Python functions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

Handler = Callable[..., Any]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Handler
    group: str
    confirm: bool = False


_REGISTRY: dict[str, Tool] = {}


def tool(
    *,
    name: str,
    description: str,
    schema: dict,
    group: str,
    confirm: bool = False,
) -> Callable[[Handler], Handler]:
    """Register a function as a tool the model can call.

    `confirm=True` marks an action that is outward-facing or hard to reverse.
    The agent loop asks the user before running those.
    """

    def decorator(fn: Handler) -> Handler:
        if name in _REGISTRY:
            raise ValueError(f"duplicate tool name: {name}")
        _REGISTRY[name] = Tool(
            name=name,
            description=description,
            input_schema=schema,
            handler=fn,
            group=group,
            confirm=confirm,
        )
        return fn

    return decorator


def obj(properties: dict, required: list[str] | None = None) -> dict:
    """Shorthand for a JSON Schema object."""
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


# What to say while a tool is running, in the user's terms rather than the
# API's — this is read by someone waiting, not by someone debugging. Phrased to
# follow a spinner: "searching your mail…".
ACTIVITY: dict[str, str] = {
    "browser_click": "clicking that",
    "browser_close": "closing the browser",
    "browser_download": "downloading that file",
    "browser_navigate": "moving through the browser history",
    "browser_open": "opening that page",
    "browser_read": "reading the page",
    "browser_screenshot": "taking a screenshot",
    "browser_scroll": "scrolling the page",
    "browser_select": "choosing an option",
    "browser_type": "filling that in",
    "browser_upload": "attaching your file",
    "calendar_create_event": "adding it to your calendar",
    "calendar_delete_event": "removing that event",
    "calendar_find_free_time": "looking for a free slot",
    "calendar_list_calendars": "checking your calendars",
    "calendar_list_events": "checking your calendar",
    "calendar_search_events": "looking for that event",
    "calendar_update_event": "updating that event",
    "complete_reminder": "ticking that reminder off",
    "contacts_list": "looking through your contacts",
    "create_reminder": "setting your reminder",
    "daily_agenda": "pulling your day together",
    "delete_reminder": "deleting that reminder",
    "contacts_search": "looking up that contact",
    "drive_create_file": "creating that file in Drive",
    "drive_download_file": "downloading from Drive",
    "drive_read_file": "reading that file from Drive",
    "drive_search_files": "searching your Drive",
    "drive_share_file": "sharing that file",
    "drive_trash_file": "moving that file to the bin",
    "file_list": "listing your workspace files",
    "file_read": "reading a workspace file",
    "file_write": "saving a workspace file",
    "gmail_create_draft": "saving a draft",
    "gmail_get_profile": "checking your account",
    "gmail_list_labels": "checking your labels",
    "gmail_modify_labels": "updating labels",
    "gmail_read_message": "opening that email",
    "gmail_read_thread": "reading that conversation",
    "get_reminders": "checking your reminders",
    "gmail_search": "searching your mail",
    "gmail_send_email": "sending your email",
    "gmail_trash_message": "moving that email to the bin",
    "search_reminders": "looking for that reminder",
    "snooze_reminder": "snoozing that reminder",
    "tasks_complete": "ticking that task off",
    "tasks_create": "adding that task",
    "tasks_delete": "deleting that task",
    "tasks_list": "checking your tasks",
    "tasks_list_tasklists": "checking your task lists",
    "update_reminder": "updating that reminder",
    "web_fetch": "reading that page",
    "web_search": "searching the web",
}


def activity(name: str) -> str:
    """Prose for the wait indicator while `name` runs.

    A tool added without an entry above still reads as a sentence rather than
    breaking the line, so the map is a nicety to keep current, not a duty.
    """
    return ACTIVITY.get(name) or f"running {name.replace('_', ' ')}"


def all_tools() -> list[Tool]:
    # Sorted so the rendered tool list is byte-stable across runs, which keeps
    # the prompt cache prefix intact.
    return sorted(_REGISTRY.values(), key=lambda t: t.name)


def get(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def api_definitions() -> list[dict]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        for t in all_tools()
    ]


def load_all() -> None:
    """Import every tool module so the decorators run.

    A new integration is one import here plus a module in tools/ — nothing else
    in the app needs to know it exists.
    """
    from .tools import (  # noqa: F401
        browser,
        calendar,
        contacts,
        drive,
        gmail,
        localfiles,
        reminders,
        tasks,
        websearch,
    )
