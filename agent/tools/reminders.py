"""Reminder tools, backed by the app's own database rather than anyone else's.

These are deliberately separate from Google Tasks. A task is a thing on a list;
a reminder is a thing that rings. `agent/reminders.py` owns the storage and
`agent/notify.py` owns the ringing — this module is only the surface the model
sees.

The last tool here, `daily_agenda`, is the one that makes the separation pay:
it pulls calendar events, reminders and tasks together into one answer to "what
do I need to do tomorrow?".
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..logs import logger
from ..registry import obj, tool
from ..reminders import RECURRENCES, SCOPES, ReminderError, store

GROUP = "reminders"
log = logger("reminders.tools")

DUE_HELP = (
    "When it should ring: 'YYYY-MM-DD HH:MM' in the user's local time, e.g. "
    "'2026-08-09 17:00'. A bare 'YYYY-MM-DD' rings at 9am. If the user did not "
    "say when, ask them — never invent a time."
)

RECURRENCE_HELP = (
    "Repeat rule: " + ", ".join(RECURRENCES) + ", or 'weekly:MO,WE' for particular "
    "weekdays. 'every weekday at 6pm' is 'weekdays'; 'every Monday' is 'weekly:MO'. "
    "Omit for a one-off."
)


def _found(reminders: list[dict], **extra) -> dict:
    return {"count": len(reminders), "reminders": reminders, **extra}


def _outcome(reminder: dict, action: str) -> dict:
    """Report what just happened without losing what the reminder now *is*.

    A reminder carries its own `status` — pending or completed — and both the
    model and the UI read the tool result's `status` as "what did this call do".
    Spreading the record over the outcome would quietly answer the second
    question with the first, so the lifecycle moves aside.
    """
    return {**reminder, "status": action, "reminder_status": reminder.get("status")}


@tool(
    group=GROUP,
    name="create_reminder",
    description=(
        "Create a reminder that will notify the user at a set time, even if the "
        "app is closed. Use this whenever they say remind me, don't let me "
        "forget, ping me, or ask to be told about something later. These are the "
        "app's own reminders, not Google Tasks — reach for tasks_create only when "
        "the user asks for a to-do with no time attached, and for calendar events "
        "only when something occupies a slot in the day. If the user has not said "
        "when, ask them before calling this: a reminder with a guessed time is "
        "worse than none. Confirm what you set afterwards, including the time."
    ),
    schema=obj(
        {
            "title": {
                "type": "string",
                "description": "What to remind them about, in their own words, e.g. 'Study trigonometry'.",
            },
            "due": {"type": "string", "description": DUE_HELP},
            "notes": {"type": "string", "description": "Optional extra detail shown with the reminder."},
            "recurrence": {"type": "string", "description": RECURRENCE_HELP},
            "tags": {
                "type": "string",
                "description": "Optional comma-separated categories, e.g. 'school,maths'.",
            },
            "timezone": {
                "type": "string",
                "description": "IANA zone if the time is not in this computer's timezone, e.g. 'Europe/Berlin'.",
            },
        },
        required=["title", "due"],
    ),
)
def create_reminder(
    title: str,
    due: str,
    notes: str = "",
    recurrence: str | None = None,
    tags: str = "",
    timezone: str | None = None,
) -> dict:
    created = store().create(title, due, notes, recurrence, tags, timezone)
    return _outcome(created, "created")


@tool(
    group=GROUP,
    name="get_reminders",
    description=(
        "List the user's reminders. 'today' covers everything still due today "
        "including anything overdue, 'upcoming' is everything pending in date "
        "order, 'overdue' is what has passed without being ticked off, "
        "'completed' is history. Use this for 'what reminders do I have', and "
        "before changing one so you have its id."
    ),
    schema=obj(
        {
            "scope": {
                "type": "string",
                "enum": list(SCOPES),
                "description": "Which set to return. Default 'upcoming'.",
            },
            "tag": {"type": "string", "description": "Only reminders carrying this tag."},
            "limit": {"type": "integer", "description": "How many to return (1-500). Default 50."},
        }
    ),
)
def get_reminders(scope: str = "upcoming", tag: str | None = None, limit: int = 50) -> dict:
    found = store().list(scope=scope, tag=tag, limit=limit)
    return _found(found, scope=scope, **store().counts())


@tool(
    group=GROUP,
    name="search_reminders",
    description=(
        "Find reminders by words in their title, notes or tags. Use this to turn "
        "what the user called something — 'my study reminder', 'the one about "
        "calling John' — into the id the other tools need. If several match, ask "
        "which one they meant rather than guessing."
    ),
    schema=obj(
        {
            "query": {"type": "string", "description": "Words to look for."},
            "include_completed": {
                "type": "boolean",
                "description": "Also search reminders already ticked off. Default false.",
            },
            "limit": {"type": "integer", "description": "How many to return. Default 20."},
        },
        required=["query"],
    ),
)
def search_reminders(query: str, include_completed: bool = False, limit: int = 20) -> dict:
    found = store().list(
        scope="all" if include_completed else "upcoming", search=query, limit=limit
    )
    return _found(found, query=query)


@tool(
    group=GROUP,
    name="update_reminder",
    description=(
        "Change a reminder: its time, title, notes, recurrence or tags. Only the "
        "fields you pass change. This is what 'move my study reminder to 7pm' "
        "means — find it with search_reminders first, then update it. Moving the "
        "time also re-arms the notification."
    ),
    schema=obj(
        {
            "reminder_id": {"type": "string", "description": "Id from get_reminders or search_reminders."},
            "title": {"type": "string", "description": "New title."},
            "due": {"type": "string", "description": "New time. " + DUE_HELP},
            "notes": {"type": "string", "description": "New notes."},
            "recurrence": {"type": "string", "description": RECURRENCE_HELP},
            "tags": {"type": "string", "description": "Replacement comma-separated tags."},
            "timezone": {"type": "string", "description": "IANA zone for the new time."},
        },
        required=["reminder_id"],
    ),
)
def update_reminder(
    reminder_id: str,
    title: str | None = None,
    due: str | None = None,
    notes: str | None = None,
    recurrence: str | None = None,
    tags: str | None = None,
    timezone: str | None = None,
) -> dict:
    updated = store().update(reminder_id, title, due, notes, recurrence, tags, timezone)
    return _outcome(updated, "updated")


@tool(
    group=GROUP,
    name="complete_reminder",
    description=(
        "Tick a reminder off. For a repeating one this completes today's "
        "occurrence and rolls it on to the next, which is what the user means by "
        "'done' — it does not cancel the series. To stop a series entirely, use "
        "delete_reminder."
    ),
    schema=obj(
        {"reminder_id": {"type": "string", "description": "Id of the reminder to complete."}},
        required=["reminder_id"],
    ),
)
def complete_reminder(reminder_id: str) -> dict:
    done = store().complete(reminder_id)
    return _outcome(done, done.pop("outcome"))


@tool(
    group=GROUP,
    name="snooze_reminder",
    description=(
        "Push a reminder back by a number of minutes from now. Use it for 'remind "
        "me again in an hour' or 'not yet'. Snoozing re-arms the notification; the "
        "original time is remembered."
    ),
    schema=obj(
        {
            "reminder_id": {"type": "string", "description": "Id of the reminder to snooze."},
            "minutes": {
                "type": "integer",
                "description": "How long to push it back, in minutes. Default 10.",
            },
        },
        required=["reminder_id"],
    ),
)
def snooze_reminder(reminder_id: str, minutes: int = 10) -> dict:
    return _outcome(store().snooze(reminder_id, minutes), "snoozed")


@tool(
    group=GROUP,
    name="delete_reminder",
    description=(
        "Delete a reminder permanently, including every future occurrence of a "
        "repeating one. There is no undo. If the user has simply finished the "
        "thing, complete_reminder is what they want instead. Find the reminder "
        "first and be certain it is the right one — say which one you are about "
        "to delete."
    ),
    schema=obj(
        {"reminder_id": {"type": "string", "description": "Id of the reminder to delete."}},
        required=["reminder_id"],
    ),
    confirm=True,
)
def delete_reminder(reminder_id: str) -> dict:
    return store().delete(reminder_id)


# ------------------------------------------------------- the combined view


def _day_bounds(date: str | None) -> tuple[datetime, datetime, str]:
    """Local midnight-to-midnight for a 'YYYY-MM-DD', or today."""
    if date:
        try:
            day = datetime.fromisoformat(date.strip()[:10])
        except ValueError as exc:
            raise ReminderError(f"could not read {date!r} as a date (use YYYY-MM-DD)") from exc
    else:
        day = datetime.now()
    start = day.replace(hour=0, minute=0, second=0, microsecond=0).astimezone()
    return start, start + timedelta(days=1), start.date().isoformat()


def _calendar_items(start: datetime, end: datetime) -> tuple[list[dict], str | None]:
    from .calendar import calendar_list_events

    try:
        found = calendar_list_events(
            time_min=start.isoformat(), time_max=end.isoformat(), max_results=50
        )
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"
    items = [
        {
            "kind": "event",
            "at": event["start"],
            "title": event["summary"],
            "end": event["end"],
            "location": event.get("location"),
            "id": event["event_id"],
            "all_day": bool(event["start"]) and len(event["start"]) == 10,
        }
        for event in found.get("events", [])
    ]
    return items, None


def _reminder_items(start: datetime, end: datetime) -> list[dict]:
    items = []
    for reminder in store().list(scope="all", limit=500):
        if reminder["status"] == "completed":
            continue
        due = datetime.fromisoformat(reminder["due_utc"])
        if start <= due < end:
            items.append(
                {
                    "kind": "reminder",
                    "at": reminder["due_local"],
                    "title": reminder["title"],
                    "id": reminder["id"],
                    "recurs": reminder["recurs"],
                    "overdue": reminder["overdue"],
                }
            )
    return items


def _task_items(day: str) -> tuple[list[dict], str | None]:
    from .tasks import tasks_list

    try:
        found = tasks_list(max_results=100)
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"
    items = [
        {
            "kind": "task",
            "at": None,  # Google Tasks carries a date, never a time
            "title": task["title"],
            "id": task["task_id"],
            "due_date": (task.get("due") or "")[:10],
        }
        for task in found.get("tasks", [])
        if (task.get("due") or "")[:10] == day
    ]
    return items, None


@tool(
    group=GROUP,
    name="daily_agenda",
    description=(
        "Everything happening on one day, in one call: calendar events, app "
        "reminders and Google Tasks due that day, merged in time order. This is "
        "the right tool for 'what do I need to do tomorrow?', 'what's my day "
        "look like', or 'anything after school today' — it saves calling three "
        "tools and lets you answer as one schedule rather than three lists. If "
        "one source fails the rest still come back, and the failure is named."
    ),
    schema=obj(
        {
            "date": {
                "type": "string",
                "description": "Day to summarise as 'YYYY-MM-DD'. Defaults to today.",
            }
        }
    ),
)
def daily_agenda(date: str | None = None) -> dict:
    start, end, day = _day_bounds(date)
    events, calendar_error = _calendar_items(start, end)
    reminders = _reminder_items(start, end)
    tasks, task_error = _task_items(day)

    # All-day events first, then everything with a time, then undated tasks.
    def sort_key(item: dict) -> tuple:
        if item.get("all_day"):
            return (0, "")
        stamp = str(item.get("at") or "")
        if "T" in stamp:
            # Everything here is on the same local day, so the wall clock is
            # enough to order by — and it is the one part of the stamp that
            # events (with an offset, to the second) and reminders (naive, to
            # the minute) write the same way.
            return (1, stamp.split("T", 1)[1][:5])
        return (2, "")

    items = sorted(events + reminders + tasks, key=sort_key)
    problems = {
        name: detail
        for name, detail in (("calendar", calendar_error), ("tasks", task_error))
        if detail
    }
    return {
        "date": day,
        "count": len(items),
        "events": len(events),
        "reminders": len(reminders),
        "tasks": len(tasks),
        "items": items,
        **({"unavailable": problems} if problems else {}),
    }
