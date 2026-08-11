"""Google Tasks tools: lists, create, complete, delete."""

from __future__ import annotations

from datetime import datetime

from ..registry import obj, tool
from .google_auth import service

GROUP = "tasks"


def _svc():
    return service("tasks", "v1")


def _due(value: str | None) -> str | None:
    """Google Tasks stores due dates as RFC3339 at UTC midnight; time is ignored."""
    if not value:
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)  # handles a trailing Z since 3.11
    except ValueError:
        return text
    return parsed.strftime("%Y-%m-%dT00:00:00.000Z")


def _clean(task: dict) -> dict:
    return {
        "task_id": task.get("id"),
        "title": task.get("title"),
        "notes": task.get("notes"),
        "due": task.get("due"),
        "status": task.get("status"),
        "completed": task.get("completed"),
    }


def _default_tasklist() -> str:
    lists = _svc().tasklists().list(maxResults=1).execute().get("items", [])
    if not lists:
        raise RuntimeError("no Google Tasks list found on this account")
    return lists[0]["id"]


@tool(
    group=GROUP,
    name="tasks_list_tasklists",
    description="List the user's Google Tasks lists with their ids. The first one is usually the default.",
    schema=obj({}),
)
def tasks_list_tasklists() -> dict:
    items = _svc().tasklists().list(maxResults=100).execute().get("items", [])
    return {
        "tasklists": [{"tasklist_id": t["id"], "title": t.get("title")} for t in items]
    }


@tool(
    group=GROUP,
    name="tasks_list",
    description="List tasks in a Google Tasks list. Defaults to the user's default list and hides completed tasks.",
    schema=obj(
        {
            "tasklist_id": {
                "type": "string",
                "description": "Which list to read. Omit for the default list.",
            },
            "show_completed": {
                "type": "boolean",
                "description": "Include completed tasks. Default false.",
            },
            "max_results": {
                "type": "integer",
                "description": "How many tasks to return (1-100). Default 50.",
            },
        }
    ),
)
def tasks_list(
    tasklist_id: str | None = None,
    show_completed: bool = False,
    max_results: int = 50,
) -> dict:
    tasklist_id = tasklist_id or _default_tasklist()
    items = (
        _svc()
        .tasks()
        .list(
            tasklist=tasklist_id,
            showCompleted=show_completed,
            showHidden=show_completed,
            maxResults=max(1, min(int(max_results), 100)),
        )
        .execute()
        .get("items", [])
    )
    return {
        "tasklist_id": tasklist_id,
        "count": len(items),
        "tasks": [_clean(t) for t in items],
    }


@tool(
    group=GROUP,
    name="tasks_create",
    description=(
        "Add a to-do item to a Google Tasks list. Use this when the user says "
        "to remember, track, or follow up on something that is not a calendar "
        "event — a task has a due date but no time slot."
    ),
    schema=obj(
        {
            "title": {"type": "string", "description": "Task title."},
            "notes": {"type": "string", "description": "Longer detail for the task."},
            "due": {
                "type": "string",
                "description": "Due date as 'YYYY-MM-DD'. Google Tasks stores dates only, not times.",
            },
            "tasklist_id": {
                "type": "string",
                "description": "Which list to add to. Omit for the default list.",
            },
        },
        required=["title"],
    ),
)
def tasks_create(
    title: str,
    notes: str | None = None,
    due: str | None = None,
    tasklist_id: str | None = None,
) -> dict:
    tasklist_id = tasklist_id or _default_tasklist()
    body: dict = {"title": title}
    if notes:
        body["notes"] = notes
    if due:
        body["due"] = _due(due)
    created = _svc().tasks().insert(tasklist=tasklist_id, body=body).execute()
    return {"status": "created", "tasklist_id": tasklist_id, **_clean(created)}


@tool(
    group=GROUP,
    name="tasks_complete",
    description=(
        "Mark a Google Task as done. Get the task_id from tasks_list first. "
        "Completing a task keeps it in history; use tasks_delete to remove it."
    ),
    schema=obj(
        {
            "task_id": {"type": "string", "description": "Id of the task."},
            "tasklist_id": {
                "type": "string",
                "description": "Which list it belongs to. Omit for the default list.",
            },
        },
        required=["task_id"],
    ),
)
def tasks_complete(task_id: str, tasklist_id: str | None = None) -> dict:
    tasklist_id = tasklist_id or _default_tasklist()
    updated = (
        _svc()
        .tasks()
        .patch(tasklist=tasklist_id, task=task_id, body={"status": "completed"})
        .execute()
    )
    return {"status": "completed", "tasklist_id": tasklist_id, **_clean(updated)}


@tool(
    group=GROUP,
    name="tasks_delete",
    description=(
        "Permanently remove a task from a Google Tasks list. If the user has "
        "finished the task rather than abandoned it, use tasks_complete instead."
    ),
    schema=obj(
        {
            "task_id": {"type": "string", "description": "Id of the task to delete."},
            "tasklist_id": {
                "type": "string",
                "description": "Which list it belongs to. Omit for the default list.",
            },
        },
        required=["task_id"],
    ),
    confirm=True,
)
def tasks_delete(task_id: str, tasklist_id: str | None = None) -> dict:
    tasklist_id = tasklist_id or _default_tasklist()
    _svc().tasks().delete(tasklist=tasklist_id, task=task_id).execute()
    return {"status": "deleted", "task_id": task_id, "tasklist_id": tasklist_id}
