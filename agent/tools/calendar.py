"""Google Calendar tools: list, search, create, update, delete events; find free
time; check for clashes.

Recurrence uses the same small vocabulary as the app's own reminders — 'daily',
'weekdays', 'weekly:MO,WE' — translated to an RRULE on the way out, so the model
does not have to know one spelling for a repeating meeting and another for a
repeating reminder.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from ..logs import logger
from ..registry import obj, tool
from ..reminders import normalise_recurrence
from .google_auth import service

GROUP = "calendar"
log = logger("calendar")

# A search with no window still has to have one. Far enough back to find "that
# meeting last month" and far enough on to find next term's timetable.
SEARCH_BACK_DAYS = 30
SEARCH_FORWARD_DAYS = 180

# What "free" means when nobody says otherwise: office hours, weekdays.
DEFAULT_EARLIEST_HOUR = 9
DEFAULT_LATEST_HOUR = 18


def _svc():
    return service("calendar", "v3")


def _local_tz_name() -> str:
    return datetime.now().astimezone().tzname() or "UTC"


def _rfc3339(value: str | None, default: datetime | None = None) -> str | None:
    """Accept an RFC3339 string, a bare date, or fall back to a default."""
    if not value:
        return default.isoformat() if default else None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)  # handles a trailing Z since 3.11
    except ValueError:
        return text  # let the API validate it and report a clear error
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.isoformat()


def _time_field(value: str, tz: str | None) -> dict:
    """All-day events use 'date'; timed events use 'dateTime' + timeZone."""
    if len(value.strip()) == 10:
        return {"date": value.strip()}
    field = {"dateTime": _rfc3339(value)}
    if tz:
        field["timeZone"] = tz
    return field


def _rrule(recurrence: str | None, count: int | None = None, until: str | None = None) -> list[str] | None:
    """Turn the shared recurrence vocabulary into what Google expects.

    Google wants an RFC 5545 RRULE; the model gets to say 'weekdays'.
    """
    rule = normalise_recurrence(recurrence)
    if rule == "none":
        return None

    if rule == "weekdays":
        parts = ["FREQ=WEEKLY", "BYDAY=MO,TU,WE,TH,FR"]
    elif rule.startswith("weekly:"):
        parts = ["FREQ=WEEKLY", "BYDAY=" + rule.split(":", 1)[1]]
    else:
        parts = ["FREQ=" + {"daily": "DAILY", "weekly": "WEEKLY",
                            "monthly": "MONTHLY", "yearly": "YEARLY"}[rule]]

    if count:
        parts.append(f"COUNT={max(1, min(int(count), 730))}")
    elif until:
        try:
            edge = datetime.fromisoformat(until.strip()[:19] if len(until.strip()) > 10 else until.strip())
        except ValueError as exc:
            raise ValueError(f"could not read repeat_until={until!r} as a date") from exc
        if edge.tzinfo is None:
            edge = edge.astimezone()
        parts.append("UNTIL=" + edge.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"))
    return ["RRULE:" + ";".join(parts)]


def _is_timed(value: str) -> bool:
    """A timed event, as opposed to an all-day one written 'YYYY-MM-DD'."""
    return len(value.strip()) > 10


def _busy_conflicts(
    start: str, end: str, calendar_id: str, ignore_event_id: str | None = None
) -> list[dict]:
    """Existing events that overlap this slot.

    Events the user has declined, and ones marked "free" rather than "busy", are
    not clashes — Google shows them on the grid but they do not occupy the time.
    All-day events are reported so the user hears about the holiday they forgot,
    but they are flagged rather than treated as a hard clash.
    """
    found = (
        _svc()
        .events()
        .list(
            calendarId=calendar_id,
            timeMin=_rfc3339(start),
            timeMax=_rfc3339(end),
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        )
        .execute()
        .get("items", [])
    )

    clashes = []
    for event in found:
        if event.get("id") == ignore_event_id or event.get("status") == "cancelled":
            continue
        if event.get("transparency") == "transparent":
            continue
        mine = [a for a in event.get("attendees", []) if a.get("self")]
        if mine and mine[0].get("responseStatus") == "declined":
            continue
        starts = (event.get("start") or {})
        cleaned = _clean(event)
        cleaned["all_day"] = "date" in starts
        clashes.append(cleaned)
    return clashes


def _conflict_report(clashes: list[dict], summary: str, start: str, end: str) -> dict:
    """The answer given instead of double-booking someone."""
    hard = [c for c in clashes if not c.get("all_day")]
    return {
        "status": "conflict",
        "created": False,
        "summary": summary,
        "requested_start": start,
        "requested_end": end,
        "conflicts": clashes,
        "detail": (
            f"That slot clashes with {len(hard) or len(clashes)} existing "
            "event(s), so nothing was changed. Tell the user what it clashes "
            "with and either pick another time — calendar_find_free_time will "
            "suggest one — or call this again with allow_conflicts=true if they "
            "want both."
        ),
    }


def _clean(event: dict) -> dict:
    start = event.get("start") or {}
    end = event.get("end") or {}
    return {
        "event_id": event.get("id"),
        "summary": event.get("summary"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "location": event.get("location"),
        "description": event.get("description"),
        "organizer": (event.get("organizer") or {}).get("email"),
        "attendees": [
            {"email": a.get("email"), "response": a.get("responseStatus")}
            for a in event.get("attendees", [])
        ],
        "status": event.get("status"),
        "link": event.get("htmlLink"),
        "meet_link": event.get("hangoutLink"),
    }


# How many calendars the week view will pull events from. A busy account can
# have dozens subscribed; each one costs a round trip, and the grid stops being
# readable long before that.
UI_MAX_CALENDARS = 8
UI_MAX_EVENTS_PER_CALENDAR = 250


def _ui_calendars(items: list[dict]) -> list[dict]:
    """The calendars worth drawing: the ones shown in Google's own UI.

    `selected` is how Google records which calendars the user actually keeps
    ticked. It is absent rather than false when off, and the primary calendar
    is worth showing whether or not it carries the flag.
    """
    chosen = [c for c in items if c.get("primary") or c.get("selected")]
    if not chosen:
        chosen = [c for c in items if c.get("primary")] or items[:1]
    return chosen[:UI_MAX_CALENDARS]


def ui_agenda(start: str | None = None, days: int = 7) -> dict:
    """Everything the browser's calendar view needs, in one round trip.

    Kept here rather than in the web layer so Google's field names stay inside
    this module, and returned already merged across calendars — the page only
    has to lay events out, not reconcile them.
    """
    tz = datetime.now().astimezone().tzinfo
    if start:
        day0 = datetime.fromisoformat(start).replace(tzinfo=tz)
    else:
        day0 = datetime.now(tz)
    day0 = day0.replace(hour=0, minute=0, second=0, microsecond=0)
    days = max(1, min(int(days), 31))
    day1 = day0 + timedelta(days=days)

    svc = _svc()
    entries = svc.calendarList().list().execute().get("items", [])
    calendars = _ui_calendars(entries)

    events: list[dict] = []
    listed: list[dict] = []
    for calendar in calendars:
        cal_id = calendar.get("id")
        colour = calendar.get("backgroundColor") or "#4b4bd8"
        listed.append(
            {
                "calendar_id": cal_id,
                "summary": calendar.get("summaryOverride") or calendar.get("summary"),
                "color": colour,
                "primary": bool(calendar.get("primary")),
            }
        )
        try:
            found = (
                svc.events()
                .list(
                    calendarId=cal_id,
                    timeMin=day0.isoformat(),
                    timeMax=day1.isoformat(),
                    maxResults=UI_MAX_EVENTS_PER_CALENDAR,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
                .get("items", [])
            )
        except Exception:
            # One unreadable calendar must not blank out the whole week.
            continue

        for event in found:
            if event.get("status") == "cancelled":
                continue
            start_field = event.get("start") or {}
            end_field = event.get("end") or {}
            events.append(
                {
                    "event_id": event.get("id"),
                    "calendar_id": cal_id,
                    "color": colour,
                    "summary": event.get("summary") or "(no title)",
                    "start": start_field.get("dateTime") or start_field.get("date"),
                    "end": end_field.get("dateTime") or end_field.get("date"),
                    "all_day": "date" in start_field,
                    "location": event.get("location"),
                    "link": event.get("htmlLink"),
                    "meet_link": event.get("hangoutLink"),
                }
            )

    events.sort(key=lambda e: (e["start"] or "", e["summary"]))
    return {
        "start": day0.date().isoformat(),
        "days": days,
        "time_zone": str(tz),
        "calendars": listed,
        "events": events,
        "count": len(events),
    }


@tool(
    group=GROUP,
    name="calendar_list_calendars",
    description="List the calendars on the account with their ids. 'primary' is the user's main calendar.",
    schema=obj({}),
)
def calendar_list_calendars() -> dict:
    items = _svc().calendarList().list().execute().get("items", [])
    return {
        "calendars": [
            {
                "calendar_id": c.get("id"),
                "summary": c.get("summary"),
                "primary": c.get("primary", False),
                "access_role": c.get("accessRole"),
                "time_zone": c.get("timeZone"),
            }
            for c in items
        ]
    }


@tool(
    group=GROUP,
    name="calendar_list_events",
    description=(
        "List calendar events in a time range, earliest first. Defaults to the "
        "next 7 days on the primary calendar. Use this to answer questions "
        "about the user's schedule."
    ),
    schema=obj(
        {
            "time_min": {
                "type": "string",
                "description": "Start of the range, RFC3339 (e.g. '2026-08-01T00:00:00+02:00') or 'YYYY-MM-DD'. Defaults to now.",
            },
            "time_max": {
                "type": "string",
                "description": "End of the range, same formats. Defaults to 7 days after time_min.",
            },
            "query": {
                "type": "string",
                "description": "Free-text filter on event title, description, attendees.",
            },
            "calendar_id": {
                "type": "string",
                "description": "Calendar id. Default 'primary'.",
            },
            "max_results": {
                "type": "integer",
                "description": "How many events to return (1-100). Default 25.",
            },
        }
    ),
)
def calendar_list_events(
    time_min: str | None = None,
    time_max: str | None = None,
    query: str | None = None,
    calendar_id: str = "primary",
    max_results: int = 25,
) -> dict:
    now = datetime.now(UTC).astimezone()
    start = _rfc3339(time_min, now)
    end_default = datetime.fromisoformat(start) + timedelta(days=7)
    end = _rfc3339(time_max, end_default)

    events = (
        _svc()
        .events()
        .list(
            calendarId=calendar_id,
            timeMin=start,
            timeMax=end,
            q=query or None,
            maxResults=max(1, min(int(max_results), 100)),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
        .get("items", [])
    )
    return {
        "calendar_id": calendar_id,
        "time_min": start,
        "time_max": end,
        "count": len(events),
        "events": [_clean(e) for e in events],
    }


@tool(
    group=GROUP,
    name="calendar_search_events",
    description=(
        "Find events by name across a wide date range, without knowing when they "
        "are. Use this when the user refers to something by description — 'my "
        "meeting with Sarah', 'the dentist appointment' — and you need its id "
        "before moving or cancelling it. Searches the last month and the next six "
        "months by default, across every calendar unless you name one. If several "
        "match, ask which one they mean rather than guessing."
    ),
    schema=obj(
        {
            "query": {
                "type": "string",
                "description": "Words to match in the title, description, location or attendees.",
            },
            "time_min": {"type": "string", "description": "Earliest date to search. Defaults to a month ago."},
            "time_max": {"type": "string", "description": "Latest date to search. Defaults to six months out."},
            "calendar_id": {
                "type": "string",
                "description": "One calendar to search. Omit to search all of them.",
            },
            "max_results": {"type": "integer", "description": "How many to return (1-50). Default 15."},
        },
        required=["query"],
    ),
)
def calendar_search_events(
    query: str,
    time_min: str | None = None,
    time_max: str | None = None,
    calendar_id: str | None = None,
    max_results: int = 15,
) -> dict:
    now = datetime.now(UTC).astimezone()
    start = _rfc3339(time_min, now - timedelta(days=SEARCH_BACK_DAYS))
    end = _rfc3339(time_max, now + timedelta(days=SEARCH_FORWARD_DAYS))
    limit = max(1, min(int(max_results), 50))

    svc = _svc()
    if calendar_id:
        targets = [{"id": calendar_id, "name": calendar_id}]
    else:
        entries = svc.calendarList().list().execute().get("items", [])
        targets = [
            {"id": c.get("id"), "name": c.get("summaryOverride") or c.get("summary")}
            for c in _ui_calendars(entries)
        ]

    found: list[dict] = []
    for target in targets:
        try:
            events = (
                svc.events()
                .list(
                    calendarId=target["id"],
                    timeMin=start,
                    timeMax=end,
                    q=query,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=limit,
                )
                .execute()
                .get("items", [])
            )
        except Exception as exc:
            # One calendar the account cannot read must not fail the search;
            # the others still have the event we are looking for.
            log.debug("searching %s failed: %s", target["id"], exc)
            continue
        for event in events:
            if event.get("status") == "cancelled":
                continue
            found.append({**_clean(event), "calendar_id": target["id"], "calendar": target["name"]})

    found.sort(key=lambda e: e["start"] or "")
    return {
        "query": query,
        "time_min": start,
        "time_max": end,
        "calendars_searched": [t["id"] for t in targets],
        "count": len(found),
        "events": found[:limit],
    }


@tool(
    group=GROUP,
    name="calendar_create_event",
    description=(
        "Create a calendar event. If attendees are given they receive an "
        "invitation email, so this is outward-facing. Use 'YYYY-MM-DD' for "
        "start/end to create an all-day event, or full RFC3339 timestamps for "
        "a timed one. The slot is checked for clashes first: if the user is "
        "already busy the event is NOT created and the clashing events come "
        "back instead, so tell them and either move it or call again with "
        "allow_conflicts=true. Set recurrence for a repeating event."
    ),
    schema=obj(
        {
            "summary": {"type": "string", "description": "Event title."},
            "start": {
                "type": "string",
                "description": "Start time, RFC3339 (e.g. '2026-08-05T14:00:00') or 'YYYY-MM-DD' for all-day.",
            },
            "end": {
                "type": "string",
                "description": "End time, same format as start.",
            },
            "description": {"type": "string", "description": "Event details."},
            "location": {"type": "string", "description": "Where the event takes place."},
            "attendees": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Email addresses to invite. They will be emailed an invitation.",
            },
            "time_zone": {
                "type": "string",
                "description": "IANA timezone, e.g. 'Europe/Berlin'. Defaults to the calendar's own timezone.",
            },
            "calendar_id": {"type": "string", "description": "Calendar id. Default 'primary'."},
            "add_meet_link": {
                "type": "boolean",
                "description": "Attach a Google Meet video link. Default false.",
            },
            "recurrence": {
                "type": "string",
                "description": (
                    "Repeat rule: daily, weekdays, weekly, monthly, yearly, or "
                    "'weekly:MO,WE' for particular days. Omit for a one-off."
                ),
            },
            "repeat_count": {
                "type": "integer",
                "description": "Stop after this many occurrences, e.g. 8 for a course.",
            },
            "repeat_until": {
                "type": "string",
                "description": "Stop repeating after this date, 'YYYY-MM-DD'.",
            },
            "allow_conflicts": {
                "type": "boolean",
                "description": (
                    "Create it even though the user is already busy then. Only "
                    "set this after telling them about the clash. Default false."
                ),
            },
        },
        required=["summary", "start", "end"],
    ),
    confirm=True,
)
def calendar_create_event(
    summary: str,
    start: str,
    end: str,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    time_zone: str | None = None,
    calendar_id: str = "primary",
    add_meet_link: bool = False,
    recurrence: str | None = None,
    repeat_count: int | None = None,
    repeat_until: str | None = None,
    allow_conflicts: bool = False,
) -> dict:
    # Look before booking. An all-day event is not a clash with anything, and a
    # user who has already been told about the clash passes allow_conflicts.
    if not allow_conflicts and _is_timed(start):
        clashes = _busy_conflicts(start, end, calendar_id)
        if any(not c.get("all_day") for c in clashes):
            return _conflict_report(clashes, summary, start, end)

    body: dict = {
        "summary": summary,
        "start": _time_field(start, time_zone),
        "end": _time_field(end, time_zone),
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees]
    rule = _rrule(recurrence, repeat_count, repeat_until)
    if rule:
        body["recurrence"] = rule

    kwargs: dict = {"calendarId": calendar_id, "body": body}
    if attendees:
        kwargs["sendUpdates"] = "all"
    if add_meet_link:
        body["conferenceData"] = {
            "createRequest": {
                # Google keys Meet-link creation off this, so it has to be
                # unique per request; a clock reading is not guaranteed to be.
                "requestId": f"agent-{uuid.uuid4()}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
        kwargs["conferenceDataVersion"] = 1

    created = _svc().events().insert(**kwargs).execute()
    return {
        "status": "created",
        **_clean(created),
        **({"recurrence": rule[0]} if rule else {}),
    }


@tool(
    group=GROUP,
    name="calendar_update_event",
    description=(
        "Change an existing calendar event: reschedule it, rename it, move it, or "
        "change who is coming. Only the fields you pass are modified, and "
        "attendees are notified of the change. This is what 'move my meeting with "
        "Sarah to Friday' means — find the event first, then pass its id with a "
        "new start and end. Use add_attendees to invite someone in addition to the "
        "people already on it; `attendees` replaces the list outright. Moving an "
        "event checks the new slot for clashes the same way creating one does."
    ),
    schema=obj(
        {
            "event_id": {"type": "string", "description": "Id of the event to update."},
            "summary": {"type": "string", "description": "New title."},
            "start": {"type": "string", "description": "New start time."},
            "end": {
                "type": "string",
                "description": "New end time. Omit when moving an event to keep its length.",
            },
            "description": {"type": "string", "description": "New description."},
            "location": {"type": "string", "description": "New location."},
            "attendees": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Replacement attendee list (replaces, does not append).",
            },
            "add_attendees": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Email addresses to invite in addition to whoever is already on the event.",
            },
            "recurrence": {
                "type": "string",
                "description": "New repeat rule, or 'none' to stop it repeating.",
            },
            "time_zone": {"type": "string", "description": "IANA timezone for new times."},
            "calendar_id": {"type": "string", "description": "Calendar id. Default 'primary'."},
            "allow_conflicts": {
                "type": "boolean",
                "description": "Move it even if the new slot is already busy. Default false.",
            },
        },
        required=["event_id"],
    ),
    confirm=True,
)
def calendar_update_event(
    event_id: str,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    add_attendees: list[str] | None = None,
    recurrence: str | None = None,
    time_zone: str | None = None,
    calendar_id: str = "primary",
    allow_conflicts: bool = False,
) -> dict:
    # Anything that needs the event as it stands: appending attendees, or
    # working out the new end when only a new start was given.
    existing: dict | None = None
    if add_attendees or (start and not end):
        existing = (
            _svc().events().get(calendarId=calendar_id, eventId=event_id).execute()
        )

    patch: dict = {}
    if summary is not None:
        patch["summary"] = summary
    if description is not None:
        patch["description"] = description
    if location is not None:
        patch["location"] = location
    if recurrence is not None:
        rule = _rrule(recurrence)
        # An empty list is how Google is told to stop an event repeating.
        patch["recurrence"] = rule or []

    if start is not None and end is None and existing:
        # Moving an event should not silently change how long it lasts.
        old_start = (existing.get("start") or {}).get("dateTime") or (existing.get("start") or {}).get("date")
        old_end = (existing.get("end") or {}).get("dateTime") or (existing.get("end") or {}).get("date")
        if old_start and old_end and _is_timed(old_start) and _is_timed(start):
            length = datetime.fromisoformat(old_end) - datetime.fromisoformat(old_start)
            end = (datetime.fromisoformat(_rfc3339(start)) + length).isoformat()

    if start is not None:
        patch["start"] = _time_field(start, time_zone)
    if end is not None:
        patch["end"] = _time_field(end, time_zone)

    if attendees is not None:
        patch["attendees"] = [{"email": a} for a in attendees]
    elif add_attendees:
        current = [
            {"email": a.get("email")}
            for a in (existing or {}).get("attendees", [])
            if a.get("email")
        ]
        known = {a["email"].lower() for a in current}
        current += [{"email": a} for a in add_attendees if a.lower() not in known]
        patch["attendees"] = current

    if not patch:
        return {"status": "no_change", "event_id": event_id}

    if start is not None and end is not None and not allow_conflicts and _is_timed(start):
        clashes = _busy_conflicts(start, end, calendar_id, ignore_event_id=event_id)
        if any(not c.get("all_day") for c in clashes):
            report = _conflict_report(clashes, summary or "(unchanged)", start, end)
            report["moved"] = False
            report["event_id"] = event_id
            return report

    updated = (
        _svc()
        .events()
        .patch(
            calendarId=calendar_id,
            eventId=event_id,
            body=patch,
            sendUpdates="all",
        )
        .execute()
    )
    return {"status": "updated", **_clean(updated)}


@tool(
    group=GROUP,
    name="calendar_delete_event",
    description="Delete a calendar event. Attendees are notified of the cancellation.",
    schema=obj(
        {
            "event_id": {"type": "string", "description": "Id of the event to delete."},
            "calendar_id": {"type": "string", "description": "Calendar id. Default 'primary'."},
        },
        required=["event_id"],
    ),
    confirm=True,
)
def calendar_delete_event(event_id: str, calendar_id: str = "primary") -> dict:
    _svc().events().delete(
        calendarId=calendar_id, eventId=event_id, sendUpdates="all"
    ).execute()
    return {"status": "deleted", "event_id": event_id}


@tool(
    group=GROUP,
    name="calendar_find_free_time",
    description=(
        "Find open slots in a time range by checking busy periods across one or "
        "more calendars. Use this before proposing a meeting time, and to answer "
        "'when am I free this week' or 'find me a 30-minute slot'. Only daytime "
        "on weekdays counts as free by default — nobody means 3am — so widen "
        "earliest_hour/latest_hour or set include_weekends when they do."
    ),
    schema=obj(
        {
            "time_min": {"type": "string", "description": "Start of the search window, RFC3339."},
            "time_max": {"type": "string", "description": "End of the search window, RFC3339."},
            "duration_minutes": {
                "type": "integer",
                "description": "Minimum length of a usable slot. Default 30.",
            },
            "calendar_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Calendars to check. Default ['primary'].",
            },
            "earliest_hour": {
                "type": "integer",
                "description": f"Earliest hour of the day to offer, 0-23. Default {DEFAULT_EARLIEST_HOUR}.",
            },
            "latest_hour": {
                "type": "integer",
                "description": f"Latest hour a slot may end, 1-24. Default {DEFAULT_LATEST_HOUR}.",
            },
            "include_weekends": {
                "type": "boolean",
                "description": "Offer Saturday and Sunday too. Default false.",
            },
        },
        required=["time_min", "time_max"],
    ),
)
def calendar_find_free_time(
    time_min: str,
    time_max: str,
    duration_minutes: int = 30,
    calendar_ids: list[str] | None = None,
    earliest_hour: int = DEFAULT_EARLIEST_HOUR,
    latest_hour: int = DEFAULT_LATEST_HOUR,
    include_weekends: bool = False,
) -> dict:
    calendar_ids = calendar_ids or ["primary"]
    start = _rfc3339(time_min)
    end = _rfc3339(time_max)

    busy_response = (
        _svc()
        .freebusy()
        .query(
            body={
                "timeMin": start,
                "timeMax": end,
                "items": [{"id": cid} for cid in calendar_ids],
            }
        )
        .execute()
    )

    busy: list[tuple[datetime, datetime]] = []
    for calendar in busy_response.get("calendars", {}).values():
        for period in calendar.get("busy", []):
            busy.append(
                (
                    datetime.fromisoformat(period["start"]),
                    datetime.fromisoformat(period["end"]),
                )
            )
    busy.sort()

    # Merge overlapping busy blocks, then take the gaps between them.
    merged: list[list[datetime]] = []
    for block_start, block_end in busy:
        if merged and block_start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], block_end)
        else:
            merged.append([block_start, block_end])

    window_start = datetime.fromisoformat(start)
    window_end = datetime.fromisoformat(end)
    minimum = timedelta(minutes=duration_minutes)

    gaps: list[tuple[datetime, datetime]] = []
    cursor = window_start
    for block_start, block_end in merged:
        if block_start > cursor:
            gaps.append((cursor, block_start))
        cursor = max(cursor, block_end)
    if window_end > cursor:
        gaps.append((cursor, window_end))

    free = [
        {"start": slot[0].isoformat(), "end": slot[1].isoformat(), "minutes": int((slot[1] - slot[0]).total_seconds() // 60)}
        for slot in _within_hours(gaps, earliest_hour, latest_hour, include_weekends)
        if slot[1] - slot[0] >= minimum
    ]

    return {
        "calendars_checked": calendar_ids,
        "duration_minutes": duration_minutes,
        "hours_considered": f"{earliest_hour:02d}:00-{latest_hour:02d}:00"
        + ("" if include_weekends else ", weekdays only"),
        "busy_blocks": [
            {"start": b[0].isoformat(), "end": b[1].isoformat()} for b in merged
        ],
        "count": len(free),
        "free_slots": free,
    }


def _within_hours(
    gaps: list[tuple[datetime, datetime]],
    earliest_hour: int,
    latest_hour: int,
    include_weekends: bool,
) -> list[tuple[datetime, datetime]]:
    """Clip free gaps to the part of each day a human would accept a meeting in.

    A gap can span days — Friday evening to Monday morning is one hole in the
    calendar — so each day it touches is cut out separately.
    """
    earliest = max(0, min(int(earliest_hour), 23))
    latest = max(earliest + 1, min(int(latest_hour), 24))

    usable: list[tuple[datetime, datetime]] = []
    for gap_start, gap_end in gaps:
        day = gap_start.replace(hour=0, minute=0, second=0, microsecond=0)
        while day < gap_end:
            following = day + timedelta(days=1)
            if include_weekends or day.weekday() < 5:
                window_open = day + timedelta(hours=earliest)
                window_shut = day + timedelta(hours=latest) if latest < 24 else following
                piece_start = max(gap_start, window_open)
                piece_end = min(gap_end, window_shut)
                if piece_end > piece_start:
                    usable.append((piece_start, piece_end))
            day = following
    return usable
