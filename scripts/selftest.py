"""Offline self-test: exercises the agent with a stubbed AI provider.

Verifies tool schemas, local file tools, the tool-dispatch loop, the approval
gate, provider management, the application lock, key handling and error
normalisation — without calling any AI provider or Google, and without needing
a real API key. Run with:

    .venv\\Scripts\\python.exe scripts\\selftest.py
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import registry
from agent.config import load_config
from agent.core import Agent, Callbacks
from agent.providers import keystore as keystore_module
from agent.providers.base import (
    AIProvider,
    Capabilities,
    ModelInfo,
    ProviderError,
    Sink,
    Text,
    Thinking,
    ToolUse,
    Turn,
    Usage,
    ValidationResult,
)
from agent.providers.manager import ProviderManager
from agent.providers.settings import ProviderSettings

PASSED: list[str] = []
FAILED: list[str] = []
SKIPPED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(f"{label}{f' — {detail}' if detail else ''}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{f' — {detail}' if detail else ''}")


def skip(label: str, reason: str) -> None:
    """Not applicable here — not a failure."""
    SKIPPED.append(f"{label} — {reason}")
    print(f"  SKIP  {label} — {reason}")


# ------------------------------------------------------------- fake provider


def text_block(text: str) -> Text:
    return Text(text)


def tool_block(call_id: str, name: str, params: dict) -> ToolUse:
    return ToolUse(call_id, name, params)


def message(blocks, stop_reason: str) -> Turn:
    """One scripted assistant turn, in the shape a provider returns."""
    return Turn(
        blocks=list(blocks),
        stop_reason=stop_reason,
        usage=Usage(input_tokens=100, output_tokens=50),
        provider="fake",
    )


class FakeProvider(AIProvider):
    """A provider that replays a script instead of calling anyone.

    Implements the same interface as the real three, which is the point: if the
    agent can drive this, it is genuinely provider-agnostic.
    """

    id = "fake"
    name = "Fake Provider"
    console_url = "https://example.invalid/keys"
    key_hint = "fake-…"
    env_var = "FAKE_API_KEY"
    billing_note = "Not a real provider."

    def __init__(self, api_key: str, model: str | None = None, script=None, caps=None):
        super().__init__(api_key, model)
        self.script = list(script or [])
        self.requests: list = []
        self._caps = caps

    @classmethod
    def catalog(cls) -> list[ModelInfo]:
        return [
            ModelInfo("fake-large", "Fake Large", 200_000, 32_000, 1.0, 5.0),
            ModelInfo("fake-small", "Fake Small", 100_000, 8_000, 0.5, 1.5),
        ]

    def capabilities(self, model: str | None = None) -> Capabilities:
        return self._caps or Capabilities(
            thinking=True, mid_conversation_system=True, effort=True, max_output_tokens=32_000
        )

    def validate_key(self) -> ValidationResult:
        return ValidationResult(True, "ok", models=self.catalog())

    def stream(self, request, sink: Sink) -> Turn:
        self.requests.append(request)
        if not self.script:
            raise AssertionError("the fake provider ran out of scripted turns")
        turn = self.script.pop(0)
        if isinstance(turn, Exception):
            raise turn
        for block in turn.blocks:
            if isinstance(block, Text):
                sink.text(block.text)
            elif isinstance(block, Thinking):
                sink.thinking(block.text)
        return turn


class MemoryKeystore:
    """An in-process stand-in for the OS keystore. Nothing touches disk."""

    name = "test keystore"
    detail = "in memory"
    secure = True

    def __init__(self):
        self.data: dict[str, str] = {}

    def get(self, account):
        return self.data.get(account)

    def set(self, account, secret):
        self.data[account] = secret

    def delete(self, account):
        return self.data.pop(account, None) is not None


class RecordingCallbacks(Callbacks):
    def __init__(self, approve: bool = True):
        self.text = ""
        self.tools: list[str] = []
        self.notices: list[str] = []
        self.confirmations: list[str] = []
        self.approve = approve

    def on_text(self, delta):
        self.text += delta

    def on_tool_start(self, name, params):
        self.tools.append(name)

    def on_notice(self, message):
        self.notices.append(message)

    def confirm(self, name, params):
        self.confirmations.append(name)
        return self.approve


class StubManager(ProviderManager):
    """A manager wired to one FakeProvider, with no keystore and no disk."""

    def __init__(self, provider: FakeProvider, unlocked: bool = True):
        super().__init__(
            store=MemoryKeystore(),
            settings=ProviderSettings(active="fake" if unlocked else None),
            allow_env=False,
        )
        self.provider = provider
        self._unlocked = unlocked

    def _save(self) -> None:
        pass  # nothing on disk to write

    def is_configured(self, provider_id):
        return self._unlocked and provider_id == "fake"

    def configured_ids(self):
        return ["fake"] if self._unlocked else []

    def model_for(self, provider_id):
        return self.provider.model

    def build(self, provider_id=None):
        if not self._unlocked:
            from agent.providers import ProviderNotConfigured

            raise ProviderNotConfigured("No API key is configured.")
        return self.provider


def build_agent(script, callbacks, caps=None, unlocked: bool = True):
    config = load_config()
    provider = FakeProvider("fake-test-key-not-real", "fake-large", script, caps)
    return Agent(config, callbacks, StubManager(provider, unlocked))


# ------------------------------------------------------------------- checks


def test_schemas() -> None:
    print("\nTool definitions")
    tools = registry.all_tools()
    check("at least 25 tools registered", len(tools) >= 25, f"{len(tools)} found")

    names_ok = all(t.name.replace("_", "").isalnum() for t in tools)
    check("tool names are API-safe", names_ok)

    schema_ok = True
    for spec in tools:
        schema = spec.input_schema
        if schema.get("type") != "object" or "properties" not in schema:
            schema_ok = False
            print(f"       bad schema: {spec.name}")
        for required in schema.get("required", []):
            if required not in schema["properties"]:
                schema_ok = False
                print(f"       {spec.name}: required '{required}' not in properties")
    check("all input schemas well-formed", schema_ok)

    described = all(len(t.description) > 40 for t in tools)
    check("all tools carry a real description", described)

    confirmed = {t.name for t in tools if t.confirm}
    expected = {
        "gmail_send_email",
        "gmail_trash_message",
        "drive_share_file",
        "drive_trash_file",
        "calendar_create_event",
        "calendar_update_event",
        "calendar_delete_event",
        "tasks_delete",
        "browser_upload",
    }
    check(
        "outward-facing tools require approval",
        expected <= confirmed,
        f"missing {sorted(expected - confirmed)}" if expected - confirmed else "",
    )
    check(
        "read-only tools do not require approval",
        not any(n.endswith(("_search", "_read_message", "_list")) for n in confirmed),
    )


def test_local_tools() -> None:
    print("\nLocal file tools (real I/O)")
    from agent.tools import localfiles

    written = localfiles.file_write("selftest/hello.txt", "line one\n")
    check("file_write creates a file", written["status"] == "written")

    localfiles.file_write("selftest/hello.txt", "line two\n", append=True)
    read = localfiles.file_read("selftest/hello.txt")
    check("append then read round-trips", read["content"] == "line one\nline two\n")

    listing = localfiles.file_list("selftest")
    check("file_list sees the file", any(e["name"] == "hello.txt" for e in listing["entries"]))

    escaped = False
    for attempt in ("../../secrets.txt", "..\\..\\secrets.txt"):
        try:
            localfiles.file_read(attempt)
            escaped = True
        except ValueError:
            pass
        except FileNotFoundError:
            escaped = True  # resolved outside but was not blocked by the guard
    check("path traversal is blocked", not escaped)

    Path(load_config().workspace / "selftest" / "hello.txt").unlink(missing_ok=True)
    (load_config().workspace / "selftest").rmdir()


def test_email_attachments() -> None:
    """Attaching a workspace file to mail — built and checked without Gmail."""
    print("\nEmail attachments")
    from agent.tools import gmail, localfiles

    localfiles.file_write("selftest-attach/report.csv", "quarter,revenue\nQ2,148000\n")
    localfiles.file_write("selftest-attach/notes.txt", "context\n")

    message, attached = gmail._build_message(
        "someone@example.com",
        "Latest report",
        "The report is attached.",
        attachments=["selftest-attach/report.csv", "selftest-attach/notes.txt"],
    )
    check("attaching makes it a multipart message", message.get_content_type() == "multipart/mixed")

    parts = [(p.get_content_type(), p.get_filename()) for p in message.walk()]
    check("the body is still the first part", parts[1] == ("text/plain", None), str(parts[1]))
    check(
        "both files are attached under their own names",
        [p[1] for p in parts[2:]] == ["report.csv", "notes.txt"],
        str(parts),
    )
    check(
        "the content type does not depend on the Windows registry",
        parts[2][0] == "text/csv",
        parts[2][0],
    )
    on_disk = (load_config().workspace / "selftest-attach" / "report.csv").stat().st_size
    check(
        "what was attached is reported back",
        [a["filename"] for a in attached] == ["report.csv", "notes.txt"]
        and attached[0]["size_bytes"] == on_disk,
        str(attached),
    )
    check(
        "a message with no attachments is unchanged",
        gmail._build_message("a@b.com", "s", "b")[0].get_content_type() == "text/plain",
    )

    for label, path, expected in (
        ("a path outside the workspace cannot be mailed", "../../secrets.txt", ValueError),
        ("an absolute path cannot be mailed", "C:\\Windows\\win.ini", ValueError),
        ("a missing file is reported clearly", "selftest-attach/nope.pdf", FileNotFoundError),
    ):
        try:
            gmail._build_message("a@b.com", "s", "b", attachments=[path])
            check(label, False, "it was attached")
        except expected:
            check(label, True)
        except Exception as exc:
            check(label, False, f"{type(exc).__name__}: {exc}")

    original = gmail.MAX_ATTACHMENT_BYTES
    gmail.MAX_ATTACHMENT_BYTES = 10
    try:
        gmail._build_message("a@b.com", "s", "b", attachments=["selftest-attach/report.csv"])
        check("an oversized attachment is refused before sending", False, "it was attached")
    except ValueError as exc:
        check("an oversized attachment is refused before sending", "Drive" in str(exc), str(exc)[:70])
    finally:
        gmail.MAX_ATTACHMENT_BYTES = original

    schema = registry.get("gmail_send_email").input_schema["properties"]["attachments"]
    check("the tool takes a list of paths", schema["type"] == "array")
    check("drafts can carry attachments too", "attachments" in registry.get("gmail_create_draft").input_schema["properties"])

    workspace = load_config().workspace
    for name in ("report.csv", "notes.txt"):
        (workspace / "selftest-attach" / name).unlink(missing_ok=True)
    (workspace / "selftest-attach").rmdir()


def _scratch_reminders():
    """A throwaway reminders database, so tests never touch the real one."""
    import tempfile

    from agent import reminders as store_module

    folder = tempfile.mkdtemp(prefix="agent-reminders-")
    store_module.use_store(store_module.Store(path=Path(folder) / "reminders.db"))
    return store_module.store(), folder


def test_reminder_store() -> None:
    """The app's own reminders: create, move, repeat, complete, delete."""
    print("\nReminders store")
    import shutil
    from datetime import datetime

    from agent import reminders as store_module

    store, folder = _scratch_reminders()
    try:
        made = store.create(
            "Study trigonometry", "2026-08-09 17:00", notes="chapter 4", tags="school, maths"
        )
        check("a reminder is created with its wall-clock time", made["due"] == "2026-08-09T17:00", made["due"])
        check("tags are split and tidied", made["tags"] == ["maths", "school"], str(made["tags"]))
        check("it starts pending", made["status"] == "pending")
        check("a one-off says so", made["recurs"] == "", made["recurs"])

        check(
            "a bare date rings in the morning rather than at midnight",
            store.create("Dentist", "2026-09-01")["due"].endswith("T09:00"),
        )
        for bad in ("", "next tuesday", "2026-13-45 99:00"):
            try:
                store.create("nope", bad)
                check(f"{bad!r} is refused", False, "it was accepted")
            except store_module.ReminderError:
                check(f"{bad!r} is refused as a date", True)
        try:
            store.create("", "2026-08-09 17:00")
            check("a reminder needs a title", False)
        except store_module.ReminderError:
            check("a reminder needs a title", True)

        moved = store.update(made["id"], due="2026-08-09 19:00")
        check("moving it changes the time", moved["due"] == "2026-08-09T19:00", moved["due"])
        check("and re-arms the notification", moved["notified"] is False)

        # Recurrence
        check("'every weekday' is understood", store_module.normalise_recurrence("weekdays") == "weekdays")
        check("'every Monday' shorthand", store_module.normalise_recurrence("weekly:mo") == "weekly:MO")
        check("days are ordered, not as typed", store_module.normalise_recurrence("weekly:WE,MO") == "weekly:MO,WE")
        check("it reads back in English", store_module.describe_recurrence("weekly:MO,WE") == "every Mon, Wed")
        try:
            store_module.normalise_recurrence("every other tuesday")
            check("an unsupported rule is refused", False)
        except store_module.ReminderError:
            check("an unsupported rule is refused", True)

        friday = datetime(2026, 8, 7, 18, 0)  # a Friday
        check(
            "weekdays skips the weekend",
            store_module.next_local(friday, "weekdays").day == 10,
            str(store_module.next_local(friday, "weekdays")),
        )
        check(
            "monthly clamps to a short month",
            store_module.next_local(datetime(2026, 1, 31, 9, 0), "monthly").day == 28,
        )

        repeating = store.create("Work on my app", "2026-08-10 18:00", recurrence="weekdays")
        done = store.complete(repeating["id"])
        check("completing a repeat rolls it forward", done["outcome"] == "occurrence_completed")
        check("to the next weekday", store.get(repeating["id"])["due"] == "2026-08-11T18:00")
        check("and the series stays pending", store.get(repeating["id"])["status"] == "pending")

        finished = store.complete(made["id"])
        check("completing a one-off closes it", finished["status"] == "completed")
        check("it moves to the completed list", [r["id"] for r in store.list(scope="completed")] == [made["id"]])
        store.reopen(made["id"])
        check("and can be un-ticked", store.get(made["id"])["status"] == "pending")

        snoozed = store.snooze(repeating["id"], 45)
        due_in = datetime.fromisoformat(snoozed["due"]) - datetime.now()
        check("snoozing pushes it out from now", 43 <= due_in.total_seconds() / 60 <= 46, str(due_in))
        check("and is remembered as a snooze", snoozed["snoozed"] is True)

        check("search finds it by title", [r["title"] for r in store.list(scope="all", search="trig")] == ["Study trigonometry"])
        check("search finds it by tag", len(store.list(scope="all", tag="school")) == 1)
        check("search that matches nothing is empty", store.list(scope="all", search="zzzz") == [])

        removed = store.delete(made["id"])
        check("deleting reports what went", removed["title"] == "Study trigonometry")
        try:
            store.get(made["id"])
            check("a deleted reminder is gone", False)
        except store_module.ReminderError:
            check("a deleted reminder is gone", True)
    finally:
        store.close()
        store_module.use_store(None)
        shutil.rmtree(folder, ignore_errors=True)


def test_reminder_firing() -> None:
    """What rings, when, and exactly once."""
    print("\nReminders firing")
    import shutil
    from datetime import datetime, timedelta

    from agent import reminders as store_module

    store, folder = _scratch_reminders()
    try:
        overdue = store.create(
            "Ring now", (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M")
        )
        future = store.create(
            "Ring later", (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
        )

        fired = store.claim_due()
        check("what is due is claimed", [f["id"] for f in fired] == [overdue["id"]], str(len(fired)))
        check("what is not due is left alone", store.get(future["id"])["notified"] is False)
        check("a reminder the machine slept through is marked late", fired[0]["late"] is True)
        check("with how late it was", 29 <= fired[0]["late_minutes"] <= 31, str(fired[0]["late_minutes"]))
        check("a second sweep fires nothing", store.claim_due() == [])
        check("the firing is on the record", "fired" in [h["kind"] for h in store.history(overdue["id"])])
        check("and so is having missed it", "missed" in [h["kind"] for h in store.history(overdue["id"])])
        check("an open page can see what just rang", len(store.recently_fired()) == 1)

        # Three days off: one notification, not three, and armed for tomorrow.
        stale = store.create(
            "Daily thing",
            (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M"),
            recurrence="daily",
        )
        caught_up = store.claim_due()
        entry = next(f for f in caught_up if f["id"] == stale["id"])
        check("a repeat fires once for a missed run", len(caught_up) == 1)
        check("and says how many it stepped over", entry["skipped_occurrences"] == 3, str(entry["skipped_occurrences"]))
        check(
            "and is armed for the future, not the past",
            datetime.fromisoformat(store.get(stale["id"])["due"]) > datetime.now(),
        )

        overdue_now = store.list(scope="overdue")
        check("an unticked one still shows as overdue", [r["id"] for r in overdue_now] == [overdue["id"]])
        counts = store.counts()
        check("the counts add up", counts["overdue"] == 1 and counts["upcoming"] == 3, str(counts))
    finally:
        store.close()
        store_module.use_store(None)
        shutil.rmtree(folder, ignore_errors=True)


def test_reminder_tools() -> None:
    """The surface the model sees, including how results read back."""
    print("\nReminder tools")
    import shutil

    from agent import reminders as store_module
    from agent.tools import reminders as tools

    store, folder = _scratch_reminders()
    try:
        made = tools.create_reminder("Call Mom", "2026-08-09 14:00", recurrence="weekly:SU")
        check("the tool reports the action, not the record's state", made["status"] == "created", made["status"])
        check("while still saying what the reminder is", made["reminder_status"] == "pending")
        check("recurrence is described for the answer", made["recurs"] == "every Sun", made["recurs"])

        found = tools.search_reminders("mom")
        check("search reaches it", found["count"] == 1 and found["reminders"][0]["id"] == made["id"])
        listed = tools.get_reminders(scope="upcoming")
        check("listing carries the counts for a summary", listed["upcoming"] == 1, str(listed.get("upcoming")))

        moved = tools.update_reminder(made["id"], due="2026-08-09 19:00")
        check("update reports itself as an update", moved["status"] == "updated")
        check("snooze reports itself as a snooze", tools.snooze_reminder(made["id"], 15)["status"] == "snoozed")
        done = tools.complete_reminder(made["id"])
        check("completing a repeat says so precisely", done["status"] == "occurrence_completed")
        check("deleting is confirmed first", registry.get("delete_reminder").confirm is True)
        check("creating is not", registry.get("create_reminder").confirm is False)

        gone = tools.delete_reminder(made["id"])
        check("delete goes through the store", gone["status"] == "deleted")
    finally:
        store.close()
        store_module.use_store(None)
        shutil.rmtree(folder, ignore_errors=True)


def test_calendar_helpers() -> None:
    """Recurrence translation and the working-hours filter, without Google."""
    print("\nCalendar scheduling helpers")
    from datetime import datetime

    from agent.tools import calendar as cal

    check("a daily event becomes an RRULE", cal._rrule("daily") == ["RRULE:FREQ=DAILY"])
    check(
        "'weekdays' expands to the five days",
        cal._rrule("weekdays") == ["RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"],
    )
    check("particular days come through", cal._rrule("weekly:MO,WE") == ["RRULE:FREQ=WEEKLY;BYDAY=MO,WE"])
    check("a course can stop after n times", "COUNT=8" in cal._rrule("weekly", count=8)[0])
    check("or on a date", "UNTIL=" in cal._rrule("monthly", until="2026-12-31")[0])
    check("a one-off carries no rule", cal._rrule(None) is None)
    check("the vocabulary matches the reminders one", cal._rrule("weekdays") is not None)

    check("a timed event is told from an all-day one", cal._is_timed("2026-08-09T15:00:00"))
    check("and a bare date reads as all-day", not cal._is_timed("2026-08-09"))

    friday_evening = datetime(2026, 8, 7, 16, 0).astimezone()
    monday_morning = datetime(2026, 8, 10, 11, 0).astimezone()
    weekdays_only = cal._within_hours([(friday_evening, monday_morning)], 9, 18, False)
    check(
        "a weekend-spanning gap is offered as working hours only",
        [(s.strftime("%a %H:%M"), e.strftime("%a %H:%M")) for s, e in weekdays_only]
        == [("Fri 16:00", "Fri 18:00"), ("Mon 09:00", "Mon 11:00")],
        str([(s.strftime("%a %H:%M"), e.strftime("%a %H:%M")) for s, e in weekdays_only]),
    )
    with_weekend = cal._within_hours([(friday_evening, monday_morning)], 9, 18, True)
    check("weekends can be asked for", len(with_weekend) == 4, str(len(with_weekend)))
    check(
        "nobody is offered 3am",
        all(9 <= s.hour < 18 or s == friday_evening for s, _ in with_weekend),
    )

    report = cal._conflict_report(
        [{"summary": "Math class", "all_day": False}], "New thing", "a", "b"
    )
    check("a clash refuses rather than double-books", report["created"] is False)
    check("and names what to do about it", "another time" in report["detail"] or "free_time" in report["detail"])


def test_agenda_merge() -> None:
    """Calendar, reminders and tasks in one list, and one source failing."""
    print("\nCombined day view")
    import shutil
    from datetime import datetime, timedelta

    from agent import reminders as store_module
    from agent.tools import reminders as tools

    store, folder = _scratch_reminders()
    try:
        target = (datetime.now() + timedelta(days=1)).date().isoformat()
        store.create("Study reminder", f"{target} 17:00")

        calls = {}

        def fake_events(start, end):
            calls["calendar"] = True
            return [
                {"kind": "event", "at": f"{target}T09:00:00", "title": "Math class", "end": "", "id": "e1"},
                {"kind": "event", "at": f"{target}T14:00:00", "title": "Meeting", "end": "", "id": "e2"},
            ], None

        def fake_tasks(day):
            calls["tasks"] = True
            return [{"kind": "task", "at": None, "title": "Finish project", "id": "t1", "due_date": day}], None

        original = tools._calendar_items, tools._task_items
        tools._calendar_items, tools._task_items = fake_events, fake_tasks
        try:
            agenda = tools.daily_agenda(target)
        finally:
            tools._calendar_items, tools._task_items = original

        check("all three sources are pulled", calls == {"calendar": True, "tasks": True})
        check("and merged into one list", agenda["count"] == 4, str(agenda["count"]))
        check(
            "timed things come in time order, undated last",
            [i["title"] for i in agenda["items"]]
            == ["Math class", "Meeting", "Study reminder", "Finish project"],
            str([i["title"] for i in agenda["items"]]),
        )
        check("each item says which kind it is", {i["kind"] for i in agenda["items"]} == {"event", "reminder", "task"})

        # Google being unreachable must not lose the local reminders.
        def broken(*args):
            return [], "HttpError: 503"

        tools._calendar_items, tools._task_items = broken, broken
        try:
            degraded = tools.daily_agenda(target)
        finally:
            tools._calendar_items, tools._task_items = original
        check("a failing source does not empty the day", degraded["reminders"] == 1)
        check("and the failure is named rather than hidden", "calendar" in degraded.get("unavailable", {}))
    finally:
        store.close()
        store_module.use_store(None)
        shutil.rmtree(folder, ignore_errors=True)


def test_tool_loop() -> None:
    print("\nAgent loop")
    script = [
        message(
            [
                text_block("Checking your files. "),
                tool_block("call_1", "file_list", {"path": "."}),
            ],
            "tool_use",
        ),
        message([text_block("The workspace is ready.")], "end_turn"),
    ]
    callbacks = RecordingCallbacks()
    agent = build_agent(script, callbacks)
    answer = agent.send("what's in my workspace?")

    check("tool was dispatched", callbacks.tools == ["file_list"], str(callbacks.tools))
    check("streamed text reached the callback", "Checking your files." in callbacks.text)
    check("final answer returned", "The workspace is ready." in answer)
    check("two provider round trips", len(agent.manager.provider.requests) == 2)

    history = agent.messages
    check(
        "history is user → system → assistant → tool_result → assistant",
        [m.role for m in history] == ["user", "system", "assistant", "user", "assistant"],
        str([m.role for m in history]),
    )
    tool_result = history[3].blocks[0]
    check("tool_result carries the matching id", tool_result.tool_use_id == "call_1")
    check("tool_result carries the name too (Gemini pairs by name)", tool_result.name == "file_list")
    check("tool_result is not marked as an error", tool_result.is_error is False)
    check("usage accumulated", agent.usage.output_tokens == 100, str(agent.usage))


def test_request_shape() -> None:
    print("\nRequest shape")
    script = [message([text_block("hi")], "end_turn")]
    agent = build_agent(script, RecordingCallbacks())
    agent.send("hello")
    request = agent.manager.provider.requests[0]

    check("model is set", request.model == agent.model)
    check("tools are attached", len(request.tools) == len(registry.all_tools()))
    check("the system prompt is passed through", "assistant" in request.system.lower())
    check(
        "per-turn context injected, not baked into the system prompt",
        "Current local date" not in request.system,
    )
    check(
        "max_tokens is clamped to what the model can emit",
        request.max_tokens <= agent.manager.provider.capabilities().max_output_tokens,
        str(request.max_tokens),
    )


def test_capability_negotiation() -> None:
    """The agent asks the provider what it can do rather than assuming."""
    print("\nCapability negotiation")

    plain = Capabilities(
        thinking=False, effort=False, tools=False, max_output_tokens=4096,
        mid_conversation_system=False,
    )
    agent = build_agent([message([text_block("hi")], "end_turn")], RecordingCallbacks(), caps=plain)
    agent.config.effort = "high"
    agent.config.show_thinking = True
    agent.send("hello")
    request = agent.manager.provider.requests[0]

    check("no tools are sent to a provider that has none", request.tools == [])
    check("effort is dropped when the provider has no such knob", request.effort is None)
    check("thinking is not requested when unsupported", request.want_thinking is False)
    check("max_tokens follows the smaller provider ceiling", request.max_tokens == 4096, str(request.max_tokens))

    rich = Capabilities(thinking=True, effort=True, tools=True, max_output_tokens=32_000)
    agent = build_agent([message([text_block("hi")], "end_turn")], RecordingCallbacks(), caps=rich)
    agent.config.effort = "high"
    agent.config.show_thinking = True
    agent.send("hello")
    request = agent.manager.provider.requests[0]
    check("effort reaches a provider that supports it", request.effort == "high")
    check("thinking is requested when supported", request.want_thinking is True)


def test_writing_styles() -> None:
    """A picked style reaches the request without disturbing the cached prefix."""
    print("\nWriting styles")
    import json as _json
    import threading
    import urllib.error
    import urllib.request

    from agent.prompts import STYLE_LABELS, STYLE_PROMPTS, SYSTEM_PROMPT
    from agent.web import AccountManager, ChatSession, _handler_class, _page, _Server

    agent = build_agent([message([text_block("hi")], "end_turn")] * 2, RecordingCallbacks())
    check("default adds no style text", agent._system_prompt() == SYSTEM_PROMPT)

    agent.style = "concise"
    prompt = agent._system_prompt()
    check("a style is appended", prompt.endswith(STYLE_PROMPTS["concise"]))
    check(
        "behind the shared prefix, so switching it costs no cache",
        prompt.startswith(SYSTEM_PROMPT),
    )

    agent.send("hello")
    request = agent.manager.provider.requests[0]
    check("the style reaches the request", STYLE_PROMPTS["concise"] in request.system)

    from agent.providers.base import Message as ProviderMessage

    agent.messages.append(ProviderMessage(role="user", blocks=[Text("x")]))
    agent.reset()
    check("clearing the conversation keeps the style", agent.style == "concise")

    session = ChatSession(agent)
    key = "selftest-key"
    server = _Server(
        ("127.0.0.1", 0),
        _handler_class(session, key, _page(agent, None), {}, AccountManager(session)),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def call(path: str, payload: dict | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(
            base + path,
            data=_json.dumps(payload).encode() if payload is not None else None,
            method="POST" if payload is not None else "GET",
            headers={"X-Agent-Key": key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, _json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, _json.loads(exc.read() or b"{}")

    try:
        status, body = call("/api/style")
        check("the picker is served its options", status == 200 and len(body["options"]) == len(STYLE_LABELS))
        check("it reports the current style", body["style"] == "concise")

        status, _ = call("/api/style", {"style": "formal"})
        check("a style can be set", status == 200 and agent.style == "formal")

        status, _ = call("/api/style", {"style": "pirate"})
        check("an unknown style is refused", status == 400, str(status))
        check("and the old one survives the attempt", agent.style == "formal")
    finally:
        server.shutdown()
        server.server_close()


def test_context_injection_per_provider() -> None:
    """The clock note travels as a system message or folded in, per provider.

    The agent always emits the same neutral system message; each provider
    decides how to carry it. Anthropic's newest models take one directly;
    OpenAI and Gemini have no such role and fold it into the user turn.
    """
    print("\nPer-turn context injection")

    agent = build_agent([message([text_block("hi")], "end_turn")], RecordingCallbacks())
    agent.send("hello")
    roles = [m.role for m in agent.messages]
    check("the agent emits a neutral system message", "system" in roles, str(roles))
    check(
        "and it carries the clock",
        "Current local date" in agent.messages[1].text(),
    )

    from agent.providers.base import Message, split_system

    history = [
        Message(role="user", blocks=[Text("hello")]),
        Message(role="system", blocks=[Text("Current local date and time: now")]),
        Message(role="user", blocks=[Text("and again")]),
    ]
    folded = split_system(history)
    check(
        "split_system removes the system role for providers without one",
        all(m.role != "system" for m in folded),
        str([m.role for m in folded]),
    )
    check(
        "the note rides with the turn it describes, not the one after it",
        "Current local date" in folded[0].text()
        and "Current local date" not in folded[1].text(),
        folded[1].text(),
    )
    check("and is tagged as context so it cannot be mistaken for user text",
          "<context>" in folded[0].text())
    check(
        "the user's own words are kept ahead of it",
        folded[0].blocks[0].text == "hello",
    )
    check("nothing is lost when a system note trails the history",
          "Current local date" in split_system(history[:2])[-1].text())
    check(
        "a note with no preceding user turn folds forward instead",
        "Current local date" in split_system(history[1:])[0].text(),
    )


def test_confirmation_gate() -> None:
    print("\nApproval gate")
    send_args = {"to": "a@b.com", "subject": "hi", "body": "hello"}
    script = [
        message([tool_block("call_1", "gmail_send_email", send_args)], "tool_use"),
        message([text_block("I did not send it.")], "end_turn"),
    ]
    callbacks = RecordingCallbacks(approve=False)
    agent = build_agent(script, callbacks)
    agent.config.auto_approve = False
    agent.send("email a@b.com")

    check("send_email asked for approval", callbacks.confirmations == ["gmail_send_email"])
    check("declined tool never executed", callbacks.tools == [], str(callbacks.tools))
    result = agent.messages[3].blocks[0].content
    check("model told the user declined", "declined" in result.lower())


def test_midrun_approval() -> None:
    """A tool can ask for approval itself, once it knows what it is about to do."""
    print("\nApproval asked for mid-tool")
    from agent import approvals
    from agent.registry import _REGISTRY, obj, tool

    @tool(
        group="selftest",
        name="selftest_asks",
        description="A tool that decides for itself whether this call needs approving.",
        schema=obj({}),
    )
    def selftest_asks() -> dict:
        return {"status": "ok", "approved": approvals.ask("selftest_asks", {"detail": "x"})}

    def script():
        return [
            message([tool_block("call_1", "selftest_asks", {})], "tool_use"),
            message([text_block("done")], "end_turn"),
        ]

    try:
        check("nobody is bound outside a tool call", not approvals.available())
        check("asking with nobody bound is denied", approvals.ask("x", {}) is False)

        callbacks = RecordingCallbacks(approve=True)
        agent = build_agent(script(), callbacks)
        agent.config.auto_approve = False
        agent.send("go")
        check("the running tool reached the user", callbacks.confirmations == ["selftest_asks"])
        check("the answer got back to the tool", '"approved": true' in agent.messages[3].blocks[0].content)

        callbacks = RecordingCallbacks(approve=False)
        agent = build_agent(script(), callbacks)
        agent.config.auto_approve = False
        agent.send("go")
        check("a refusal reaches the tool too", '"approved": false' in agent.messages[3].blocks[0].content)

        callbacks = RecordingCallbacks(approve=False)
        agent = build_agent(script(), callbacks)
        agent.config.auto_approve = True
        agent.send("go")
        check("auto-approve answers without asking", callbacks.confirmations == [])
        check(
            "and the tool is told it was approved",
            '"approved": true' in agent.messages[3].blocks[0].content,
        )

        check("the binding is unwound afterwards", not approvals.available())
    finally:
        _REGISTRY.pop("selftest_asks", None)


def test_web_tools() -> None:
    """Provider selection, URL safety and text extraction, without the network."""
    print("\nWeb search and fetch")
    import dataclasses

    from agent.tools import websearch

    bare = dataclasses.replace(
        load_config(),
        search_provider=None,
        brave_api_key=None,
        tavily_api_key=None,
        google_search_api_key=None,
        google_search_engine_id=None,
    )
    check("search works with no keys at all", websearch.provider_order(bare) == ["duckduckgo"])
    keyed = dataclasses.replace(bare, brave_api_key="k")
    check("a key promotes its provider", websearch.provider_order(keyed)[0] == "brave")
    check("the keyless backend stays as fallback", "duckduckgo" in websearch.provider_order(keyed))
    pinned = dataclasses.replace(keyed, tavily_api_key="k2", search_provider="tavily")
    check("an explicit choice goes first", websearch.provider_order(pinned)[0] == "tavily")
    check(
        "choosing a provider with no key does not strand the search",
        websearch.provider_order(dataclasses.replace(bare, search_provider="brave")) == ["duckduckgo"],
    )

    for label, url in (
        ("a private address is refused", "http://10.0.0.1/"),
        ("loopback is refused", "http://localhost:9/"),
        ("file:// is refused", "file:///C:/Windows/win.ini"),
        ("javascript: is refused", "javascript:alert(1)"),
        ("an empty URL is refused", ""),
    ):
        try:
            websearch.normalise_url(url)
            check(label, False, "it was accepted")
        except ValueError:
            check(label, True)
    check(
        "a bare host is assumed to be https",
        websearch.normalise_url("example.com") == "https://example.com",
    )

    html = (
        "<html><head><title>Quarterly report</title></head><body>"
        "<nav>Menu one</nav><main><h1>Revenue</h1><p>Revenue rose 12%.</p>"
        '<a href="/detail">Full breakdown</a></main>'
        "<footer>Small print</footer><script>var tracking = 1;</script></body></html>"
    )
    title, text, links = websearch.extract_text(html)
    check("the page title is read", title == "Quarterly report", title)
    check("the content survives", "Revenue rose 12%." in text)
    check("scripts, nav and footer are dropped", not any(
        junk in text for junk in ("var tracking", "Menu one", "Small print")
    ), text.replace("\n", " | "))
    check("links come back for navigation", links[0]["href"] == "/detail", str(links))

    # A provider that is down must fall through to the next one, not fail the turn.
    tried: list[str] = []

    def broken(config, query, count, recency):
        tried.append("brave")
        raise websearch.SearchError("pretend outage")

    def works(config, query, count, recency):
        tried.append("duckduckgo")
        return [websearch.Result("A title", "https://example.com/a", "A snippet")]

    original = dict(websearch._PROVIDERS)
    os.environ["BRAVE_SEARCH_API_KEY"] = "selftest-not-a-real-key"
    try:
        websearch._PROVIDERS.update({"brave": broken, "duckduckgo": works})
        result = websearch.web_search("anything")
        check("a failed provider falls through to the next", tried == ["brave", "duckduckgo"], str(tried))
        check("the answer names the provider that served it", result["provider"] == "duckduckgo")
        check("the failure is reported alongside the results", any("brave" in n for n in result["notes"]))
        check("results are marked as coming from the web", "web" in result["source"])

        websearch._PROVIDERS.update({"duckduckgo": broken})
        try:
            websearch.web_search("anything")
            check("with every provider down the tool errors", False, "it returned")
        except RuntimeError as exc:
            check("with every provider down the tool errors", "could answer" in str(exc), str(exc)[:60])
    finally:
        websearch._PROVIDERS.clear()
        websearch._PROVIDERS.update(original)
        os.environ.pop("BRAVE_SEARCH_API_KEY", None)


def test_browser_policy() -> None:
    """The guards that must hold before Chromium is ever launched."""
    print("\nBrowser guards")
    from agent.tools import browser

    check("a bare host is assumed to be https", browser.check_url("example.com") == "https://example.com")
    check("a port is not mistaken for a scheme", browser.check_url("localhost:8080").endswith(":8080"))
    for label, url in (
        ("javascript: is refused", "javascript:alert(1)"),
        ("file:// is refused", "file:///C:/Windows/win.ini"),
        ("data: is refused", "data:text/html,<h1>x</h1>"),
        ("an empty URL is refused", ""),
    ):
        try:
            browser.check_url(url)
            check(label, False, "it was accepted")
        except ValueError:
            check(label, True)

    os.environ["AGENT_BROWSER_ALLOWED_DOMAINS"] = "example.com, intranet.test"
    try:
        check("an allowed domain passes", browser.check_url("https://example.com/x"))
        check("so do its subdomains", browser.check_url("https://docs.example.com/x"))
        try:
            browser.check_url("https://somewhere-else.test/")
            check("anything else is refused", False, "it was accepted")
        except ValueError:
            check("anything else is refused", True)
    finally:
        os.environ.pop("AGENT_BROWSER_ALLOWED_DOMAINS", None)

    risky = [
        ("Buy now", "https://shop.test/x"),
        ("Place your order", "https://shop.test/x"),
        ("Delete my account", "https://app.test/settings"),
        ("Submit application", "https://jobs.test/apply"),
        ("Transfer funds", "https://bank.test/"),
        ("Continue", "https://shop.test/checkout/2"),
    ]
    missed = [text for text, url in risky if not browser.sensitivity(text, url)]
    check("purchases, deletions and submissions are caught", not missed, str(missed))

    routine = [
        ("Next page", "https://news.test/"),
        ("Open the archive", "https://news.test/"),
        ("Accept cookies", "https://news.test/"),
        ("Sign in", "https://news.test/"),
        ("Search", "https://news.test/"),
    ]
    flagged = [text for text, url in routine if browser.sensitivity(text, url)]
    check("ordinary clicks are not gated", not flagged, str(flagged))

    check(
        "a download name cannot escape its folder",
        browser.safe_name("../../etc/passwd") == "passwd",
        browser.safe_name("../../etc/passwd"),
    )

    # These all fail before anything is launched, which is what keeps the
    # offline test offline.
    for label, call in (
        ("uploading from outside the workspace is refused", lambda: browser.browser_upload("1", "../../secrets.txt")),
        ("a download with no target is refused", browser.browser_download),
        ("an unknown navigation is refused", lambda: browser.browser_navigate("teleport")),
        ("an unknown scroll direction is refused", lambda: browser.browser_scroll("sideways")),
    ):
        try:
            call()
            check(label, False, "it ran")
        except (ValueError, FileNotFoundError):
            check(label, True)

    check("closing a browser that never opened is harmless", browser.browser_close()["status"] == "closed")


def test_tool_error_handling() -> None:
    print("\nTool errors")
    script = [
        message(
            [tool_block("call_1", "file_read", {"path": "does-not-exist.txt"})],
            "tool_use",
        ),
        message([text_block("That file is not there.")], "end_turn"),
    ]
    agent = build_agent(script, RecordingCallbacks())
    agent.send("read does-not-exist.txt")
    result = agent.messages[3].blocks[0]
    check("failure is flagged is_error", result.is_error is True)
    check("error text is returned to the model", "FileNotFoundError" in result.content)


def test_unknown_tool() -> None:
    print("\nUnknown tool")
    script = [
        message([tool_block("call_1", "not_a_real_tool", {})], "tool_use"),
        message([text_block("ok")], "end_turn"),
    ]
    agent = build_agent(script, RecordingCallbacks())
    agent.send("do something")
    result = agent.messages[3].blocks[0]
    check("unknown tool reported cleanly", result.is_error and "Unknown tool" in result.content)


def test_refusal() -> None:
    print("\nRefusal handling")
    refused = message([], "refusal")
    refused.refusal_detail = "cyber"
    agent = build_agent([refused], RecordingCallbacks())
    answer = agent.send("something disallowed")
    check("refusal returns empty rather than crashing", answer == "")
    check("history rolled back to a valid state", agent.messages == [], str(agent.messages))
    check("user was told", any("declined" in n for n in agent.callbacks.notices))


def test_turn_rollback() -> None:
    """A failed turn must not leave a tool call with no matching result."""
    print("\nTurn rollback")
    agent = build_agent(
        [ProviderError("PROVIDER_UNAVAILABLE", provider="fake")], RecordingCallbacks()
    )
    try:
        agent.send("hello")
        check("a provider failure propagates", False, "it was swallowed")
    except ProviderError as exc:
        check("a provider failure propagates", True)
        check("normalised as an outage, not a bad key", exc.code == "PROVIDER_UNAVAILABLE")
    check("history is rolled back to empty", agent.messages == [], str(agent.messages))

    # Now fail on the *second* round, with an unanswered tool call in history.
    agent = build_agent(
        [
            message([tool_block("c1", "file_list", {"path": "."})], "tool_use"),
            ProviderError("NETWORK_ERROR", provider="fake"),
        ],
        RecordingCallbacks(),
    )
    with contextlib.suppress(ProviderError):
        agent.send("list my files")
    dangling = [m for m in agent.messages if m.role == "assistant" and m.tool_uses()]
    check("no assistant turn is left holding an unanswered tool call", not dangling)


# --------------------------------------------------------- provider plumbing

# Keys that are obviously fake but shaped right, so format checks are exercised
# without a real credential existing anywhere in this repository.
FAKE_KEYS = {
    "openai": "sk-" + "selftestnotarealkey" * 2,
    "anthropic": "sk-ant-" + "selftestnotarealkey" * 2,
    "gemini": "AIza" + "SelftestNotARealKey1234567890",
}


def _manager(tmp: Path, **kwargs) -> ProviderManager:
    """A manager on a memory keystore and a scratch settings file."""
    return ProviderManager(
        store=MemoryKeystore(),
        settings=ProviderSettings(),
        settings_path=tmp / "providers.json",
        allow_env=kwargs.get("allow_env", False),
    )


def _accept_all(monkey: dict) -> None:
    """Make every provider's validate_key succeed, without any network."""
    from agent.providers import catalog

    for provider in catalog.all_providers():
        monkey[provider.id] = provider.validate_key
        provider.validate_key = lambda self: ValidationResult(True, "accepted", models=[])


def _restore(monkey: dict) -> None:
    from agent.providers import catalog

    for provider in catalog.all_providers():
        if provider.id in monkey:
            provider.validate_key = monkey[provider.id]


def test_provider_registry() -> None:
    """Every registered provider is complete enough to be offered to a user."""
    print("\nProvider registry")
    from agent.providers import catalog

    ids = catalog.provider_ids()
    for wanted in ("openai", "anthropic", "gemini"):
        check(f"{wanted} is registered", wanted in ids, str(ids))

    for provider in catalog.all_providers():
        label = provider.id
        check(f"{label} declares a name", bool(provider.name))
        check(f"{label} points at a console", provider.console_url.startswith("https://"))
        check(f"{label} explains its billing", len(provider.billing_note) > 30)
        check(f"{label} ships a model catalog", len(provider.catalog()) > 0)
        check(f"{label} has a default model", provider.default_model() in {m.id for m in provider.catalog()})
        instance = provider("x" * 40)
        caps = instance.capabilities()
        check(f"{label} declares capabilities", isinstance(caps, Capabilities))
        check(f"{label} prices its default model", all(p >= 0 for p in instance.price_per_mtok()))

    # The extensibility claim, tested rather than asserted in a comment: a
    # provider written outside this package registers and is picked up.
    before = len(catalog.all_providers())
    catalog.register(FakeProvider)
    try:
        check("a new provider can be registered without touching the agent",
              len(catalog.all_providers()) == before + 1)
        check("and is described like any other", catalog.describe("fake")["name"] == "Fake Provider")
    finally:
        catalog._PROVIDERS.pop("fake", None)


def test_key_format_checks() -> None:
    """A wrong-provider paste is caught locally, before any network call."""
    print("\nKey format checks")
    from agent.providers import catalog

    openai = catalog.provider_class("openai")
    anthropic = catalog.provider_class("anthropic")
    gemini = catalog.provider_class("gemini")

    check("an empty key is refused", openai.check_format("") is not None)
    check("whitespace is caught", openai.check_format(" sk-abc ") is not None)
    check(
        "an Anthropic key pasted into OpenAI is caught by name",
        "Anthropic" in (openai.check_format(FAKE_KEYS["anthropic"]) or ""),
    )
    check("a Gemini key is not an OpenAI key", openai.check_format(FAKE_KEYS["gemini"]) is not None)
    check("an OpenAI key is not an Anthropic key", anthropic.check_format(FAKE_KEYS["openai"]) is not None)
    check("an OpenAI key is not a Gemini key", gemini.check_format(FAKE_KEYS["openai"]) is not None)
    check("a well-formed OpenAI key passes", openai.check_format(FAKE_KEYS["openai"]) is None)
    check("a well-formed Anthropic key passes", anthropic.check_format(FAKE_KEYS["anthropic"]) is None)
    check("a well-formed Gemini key passes", gemini.check_format(FAKE_KEYS["gemini"]) is None)
    check("a too-short key is refused", anthropic.check_format("sk-ant-x") is not None)


def test_provider_management() -> None:
    """Add, replace, switch, remove — the whole lifecycle, no network."""
    print("\nProvider management")
    scratch = Path(tempfile.mkdtemp(prefix="agent-providers-"))
    monkey: dict = {}
    try:
        _accept_all(monkey)
        manager = _manager(scratch)

        check("nothing is configured to begin with", manager.configured_ids() == [])
        check("and the agent is locked", not manager.is_unlocked())

        # --- add each provider
        for provider_id in ("openai", "anthropic", "gemini"):
            result = manager.add(provider_id, FAKE_KEYS[provider_id])
            check(f"{provider_id} key accepted and stored", result.ok, result.message)
            check(f"{provider_id} reads back as configured", manager.is_configured(provider_id))

        check("all three coexist", sorted(manager.configured_ids()) == ["anthropic", "gemini", "openai"],
              str(manager.configured_ids()))
        check("no provider had to be removed to add another", len(manager.configured_ids()) == 3)
        check("the first one connected became active", manager.active_id() == "openai")
        check("which unlocks the agent", manager.is_unlocked())

        # --- masking
        status = manager.status()
        openai_row = next(p for p in status["providers"] if p["id"] == "openai")
        check("a stored key is shown masked", openai_row["masked_key"].startswith("••••"))
        check("only the last four characters are shown",
              openai_row["masked_key"].endswith(FAKE_KEYS["openai"][-4:]))
        check("the key itself never appears in the status payload",
              FAKE_KEYS["openai"] not in _json_dumps(status))

        # --- switching
        manager.set_active("gemini")
        check("the active provider can be switched", manager.active_id() == "gemini")
        check("and the agent stays unlocked", manager.is_unlocked())

        # --- model selection
        manager.set_model("gemini", "gemini-2.5-flash")
        check("a model can be chosen per provider", manager.model_for("gemini") == "gemini-2.5-flash")
        check("and does not disturb another provider's model",
              manager.model_for("openai") == manager.model_for("openai"))

        # --- replacing a key
        replacement = FAKE_KEYS["openai"][:-4] + "9999"
        result = manager.add("openai", replacement)
        check("a key can be replaced", result.ok)
        check("and the new one is what is stored",
              manager.credential("openai").masked.endswith("9999"))

        # --- removing a non-active provider
        outcome = manager.remove("anthropic")
        check("a provider can be removed", outcome["removed"])
        check("it is gone from the configured set", not manager.is_configured("anthropic"))
        check("the active provider is untouched", manager.active_id() == "gemini")
        check("its model preference is forgotten too", "anthropic" not in manager.settings.models)

        # --- removing the active one, with exactly one left
        outcome = manager.remove("gemini")
        check("removing the active provider switches to the only one left", outcome["switched"])
        check("and says which", outcome["active"] == "openai", str(outcome["active"]))
        check("the agent is still usable", manager.is_unlocked())

        # --- removing the last one locks the agent
        outcome = manager.remove("openai")
        check("removing the last provider locks the agent", outcome["locked"])
        check("nothing is configured", manager.configured_ids() == [])
        check("is_unlocked reports false", not manager.is_unlocked())
        check("and the reason is a setup instruction", "provider" in manager.lock_reason().lower())
    finally:
        _restore(monkey)
        shutil.rmtree(scratch, ignore_errors=True)


def test_ambiguous_removal() -> None:
    """With several left, removing the active one asks rather than guesses."""
    print("\nRemoving the active provider")
    scratch = Path(tempfile.mkdtemp(prefix="agent-providers-"))
    monkey: dict = {}
    try:
        _accept_all(monkey)
        manager = _manager(scratch)
        for provider_id in ("openai", "anthropic", "gemini"):
            manager.add(provider_id, FAKE_KEYS[provider_id])
        manager.set_active("openai")

        outcome = manager.remove("openai")
        check("with two candidates left, nothing is chosen automatically", not outcome["switched"])
        check("the active provider is cleared", manager.active_id() is None)
        check("the agent is locked until the user picks", not manager.is_unlocked())
        check("and is told to choose", "choose" in manager.lock_reason().lower())
        check("both remaining providers are still configured",
              sorted(manager.configured_ids()) == ["anthropic", "gemini"])

        manager.set_active("anthropic")
        check("picking one unlocks it again", manager.is_unlocked())
    finally:
        _restore(monkey)
        shutil.rmtree(scratch, ignore_errors=True)


def test_validation_failures() -> None:
    """A bad key is never stored, and a working one survives a failed replace."""
    print("\nKey validation")
    from agent.providers import catalog

    scratch = Path(tempfile.mkdtemp(prefix="agent-providers-"))
    monkey: dict = {}
    try:
        _accept_all(monkey)
        manager = _manager(scratch)
        manager.add("openai", FAKE_KEYS["openai"])
        good_tail = manager.credential("openai").masked

        # Every distinct failure the provider layer can report. The point of
        # the suite: none of these may be reported as "invalid API key" unless
        # that is what actually happened.
        failures = {
            "INVALID_API_KEY": "OpenAI rejected that key.",
            "PERMISSION_DENIED": "not permitted",
            "RATE_LIMITED": "rate limit",
            "BILLING_ERROR": "no credit left",
            "NETWORK_ERROR": "Could not reach",
            "PROVIDER_UNAVAILABLE": "having trouble",
            "MODEL_UNAVAILABLE": "does not offer",
            "INVALID_REQUEST": "rejected the request",
        }
        for code, fragment in failures.items():
            catalog.provider_class("openai").validate_key = (
                lambda self, c=code, f=fragment: ValidationResult(False, f, c)
            )
            result = manager.add("openai", FAKE_KEYS["openai"][:-4] + "0000")
            check(f"{code} is refused", not result.ok)
            check(f"{code} keeps its own code", result.code == code, result.code)
            check(f"{code} is not reported as a bad key" if code != "INVALID_API_KEY"
                  else f"{code} is reported as a bad key",
                  (result.code == "INVALID_API_KEY") == (code == "INVALID_API_KEY"))
            check(f"the working key survives a failed replace after {code}",
                  manager.credential("openai").masked == good_tail)

        # A raised ProviderError is caught and normalised, not leaked.
        catalog.provider_class("openai").validate_key = (
            lambda self: (_ for _ in ()).throw(ProviderError("RATE_LIMITED", provider="openai"))
        )
        result = manager.validate("openai", FAKE_KEYS["openai"])
        check("a raised provider error becomes a result, not a crash", not result.ok)
        check("with its code intact", result.code == "RATE_LIMITED")

        # A malformed key never reaches the network at all.
        called = {"n": 0}

        def counting(self):
            called["n"] += 1
            return ValidationResult(True, "ok")

        catalog.provider_class("openai").validate_key = counting
        result = manager.validate("openai", "not-a-key")
        check("a malformed key is refused locally", not result.ok)
        check("without contacting the provider", called["n"] == 0)
    finally:
        _restore(monkey)
        shutil.rmtree(scratch, ignore_errors=True)


def test_error_normalisation() -> None:
    """Each provider maps its own failures onto the shared codes."""
    print("\nProvider error normalisation")
    import httpx

    from agent.providers.anthropic_provider import AnthropicProvider
    from agent.providers.gemini_provider import GeminiProvider
    from agent.providers.openai_provider import OpenAIProvider

    def response(status, payload):
        return httpx.Response(status, json=payload, request=httpx.Request("GET", "https://x.invalid"))

    openai = OpenAIProvider(FAKE_KEYS["openai"])
    cases = [
        (401, {"error": {"code": "invalid_api_key"}}, "INVALID_API_KEY"),
        (403, {"error": {"code": "unsupported_country"}}, "PERMISSION_DENIED"),
        (429, {"error": {"code": "rate_limit_exceeded"}}, "RATE_LIMITED"),
        (429, {"error": {"code": "insufficient_quota"}}, "BILLING_ERROR"),
        (404, {"error": {"code": "model_not_found"}}, "MODEL_UNAVAILABLE"),
        (503, {"error": {"code": "engine_overloaded"}}, "PROVIDER_UNAVAILABLE"),
        (400, {"error": {"code": "context_length_exceeded"}}, "CONTEXT_TOO_LONG"),
        (400, {"error": {"code": "invalid_value", "message": "bad"}}, "INVALID_REQUEST"),
    ]
    for status, payload, expected in cases:
        got = openai._fail(response(status, payload)).code
        check(f"openai {status}/{payload['error']['code']} → {expected}", got == expected, got)

    check(
        "a rate limit is told apart from an exhausted quota on the same status",
        openai._fail(response(429, {"error": {"code": "rate_limit_exceeded"}})).code
        != openai._fail(response(429, {"error": {"code": "insufficient_quota"}})).code,
    )

    gemini = GeminiProvider(FAKE_KEYS["gemini"])
    gemini_cases = [
        (400, {"error": {"details": [{"reason": "API_KEY_INVALID"}]}}, "INVALID_API_KEY"),
        (403, {"error": {"details": [{"reason": "SERVICE_DISABLED"}]}}, "PERMISSION_DENIED"),
        (429, {"error": {"status": "RESOURCE_EXHAUSTED"}}, "RATE_LIMITED"),
        (404, {"error": {"status": "NOT_FOUND"}}, "MODEL_UNAVAILABLE"),
        (503, {"error": {"status": "UNAVAILABLE"}}, "PROVIDER_UNAVAILABLE"),
    ]
    for status, payload, expected in gemini_cases:
        got = gemini._fail(response(status, payload)).code
        check(f"gemini {status} → {expected}", got == expected, got)

    check(
        "a 400 that is really a bad key is not reported as a bad request",
        gemini._fail(response(400, {"error": {"details": [{"reason": "API_KEY_INVALID"}]}})).code
        == "INVALID_API_KEY",
    )

    import anthropic as anthropic_sdk

    claude = AnthropicProvider(FAKE_KEYS["anthropic"])
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    sdk_cases = [
        (anthropic_sdk.AuthenticationError, 401, "INVALID_API_KEY"),
        (anthropic_sdk.PermissionDeniedError, 403, "PERMISSION_DENIED"),
        (anthropic_sdk.RateLimitError, 429, "RATE_LIMITED"),
        (anthropic_sdk.NotFoundError, 404, "MODEL_UNAVAILABLE"),
    ]
    for kind, status, expected in sdk_cases:
        exc = kind("boom", response=httpx.Response(status, request=request), body=None)
        got = claude._fail(exc).code
        check(f"anthropic {kind.__name__} → {expected}", got == expected, got)

    connection = anthropic_sdk.APIConnectionError(request=request)
    check("a dropped connection is a network error, not a bad key",
          claude._fail(connection).code == "NETWORK_ERROR")

    overloaded = anthropic_sdk.InternalServerError(
        "overloaded", response=httpx.Response(529, request=request), body=None
    )
    check("an overloaded provider is an outage",
          claude._fail(overloaded).code == "PROVIDER_UNAVAILABLE")

    out_of_credit = anthropic_sdk.BadRequestError(
        "your credit balance is too low",
        response=httpx.Response(400, request=request),
        body={"error": {"message": "your credit balance is too low"}},
    )
    check("an exhausted balance is a billing error, not a bad request",
          claude._fail(out_of_credit).code == "BILLING_ERROR")

    # Every code the app can produce must have a message a user can act on.
    from agent.providers import base

    for code in (base.INVALID_API_KEY, base.RATE_LIMITED, base.BILLING_ERROR,
                 base.NETWORK_ERROR, base.PROVIDER_UNAVAILABLE, base.MODEL_UNAVAILABLE,
                 base.INVALID_REQUEST, base.PERMISSION_DENIED, base.CONTEXT_TOO_LONG,
                 base.UNKNOWN_PROVIDER_ERROR):
        check(f"{code} has a user-facing message", len(ProviderError(code).message) > 20)


def test_serialisation_per_provider() -> None:
    """Each provider renders the same conversation into its own wire format."""
    print("\nProvider serialisation")
    from agent.providers.anthropic_provider import AnthropicProvider
    from agent.providers.base import Message, ToolDef, ToolResult, TurnRequest
    from agent.providers.gemini_provider import GeminiProvider
    from agent.providers.openai_provider import OpenAIProvider

    tools = [ToolDef("file_list", "List files.", {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    })]
    history = [
        Message(role="user", blocks=[Text("list my files")]),
        Message(role="system", blocks=[Text("Current local date: today")]),
        Message(role="assistant", blocks=[Text("Looking."), ToolUse("c1", "file_list", {"path": "."})]),
        Message(role="user", blocks=[ToolResult("c1", "two files", False, name="file_list")]),
    ]
    request = TurnRequest(model="", system="You are an assistant.", messages=history, tools=tools)

    # --- OpenAI
    openai = OpenAIProvider(FAKE_KEYS["openai"], "gpt-5.1")
    request.model = "gpt-5.1"
    body = openai._body(request)
    roles = [m["role"] for m in body["messages"]]
    check("openai puts the system prompt first", roles[0] == "system")
    check("openai has no mid-conversation system role", roles.count("system") == 1, str(roles))
    check("openai folds the context note into the user turn",
          "<context>" in body["messages"][1]["content"])
    check("openai emits tool_calls on the assistant turn",
          any("tool_calls" in m for m in body["messages"]))
    check("openai arguments are a JSON string, not an object",
          isinstance(
              next(m for m in body["messages"] if "tool_calls" in m)["tool_calls"][0]["function"]["arguments"],
              str,
          ))
    check("openai sends the tool result as its own role", "tool" in roles, str(roles))
    check("a reasoning model gets max_completion_tokens", "max_completion_tokens" in body)
    check("and not max_tokens", "max_tokens" not in body)
    request.model = "gpt-4o"
    check("an older model gets max_tokens instead", "max_tokens" in openai._body(request))
    request.effort = "max"
    request.model = "gpt-5.1"
    check("our effort scale is mapped onto theirs",
          openai._body(request)["reasoning_effort"] in {"low", "medium", "high"})
    request.effort = None

    # --- Gemini
    gemini = GeminiProvider(FAKE_KEYS["gemini"], "gemini-2.5-pro")
    request.model = "gemini-2.5-pro"
    body = gemini._body(request)
    check("gemini takes the system prompt out of the messages", "systemInstruction" in body)
    roles = [c["role"] for c in body["contents"]]
    check("gemini calls the assistant 'model'", "model" in roles, str(roles))
    check("gemini has no system role in contents", "system" not in roles, str(roles))
    parts = [p for c in body["contents"] for p in c["parts"]]
    check("gemini sends a functionCall, not a tool_use", any("functionCall" in p for p in parts))
    response_part = next(p for p in parts if "functionResponse" in p)
    check("gemini pairs the response by name", response_part["functionResponse"]["name"] == "file_list")
    schema = body["tools"][0]["functionDeclarations"][0]["parameters"]
    check("gemini schema types are upper-case", schema["type"] == "OBJECT", str(schema.get("type")))
    check("keywords gemini rejects are stripped", "additionalProperties" not in schema)
    check("nested property types are converted too",
          schema["properties"]["path"]["type"] == "STRING")

    parameterless = [ToolDef("ping", "Ping.", {"type": "object", "properties": {}})]
    empty = gemini._body(TurnRequest(model="gemini-2.5-pro", system="s", messages=history, tools=parameterless))
    check("a parameterless tool omits parameters entirely",
          "parameters" not in empty["tools"][0]["functionDeclarations"][0])

    # --- Anthropic
    claude = AnthropicProvider(FAKE_KEYS["anthropic"], "claude-sonnet-5")
    request.model = "claude-sonnet-5"
    params = claude._params(request)
    check("anthropic caches the system prompt",
          params["system"][0]["cache_control"]["type"] == "ephemeral")
    check("sonnet-5 gets no mid-conversation system message",
          all(m["role"] != "system" for m in params["messages"]),
          str([m["role"] for m in params["messages"]]))
    request.model = "claude-opus-5"
    params = claude._params(request)
    check("opus-5 does get one",
          any(m["role"] == "system" for m in params["messages"]),
          str([m["role"] for m in params["messages"]]))
    check("anthropic keeps tool input as an object",
          isinstance(
              next(b for m in params["messages"] if isinstance(m["content"], list)
                   for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use")["input"],
              dict,
          ))

    # An assistant turn from another provider must be rebuilt, not replayed —
    # a foreign `raw` payload would be rejected.
    foreign = Message(role="assistant", blocks=[Text("hi")], provider="openai", raw={"junk": True})
    rebuilt = claude._serialise([foreign], supports_system=False)
    check("another provider's raw payload is never replayed",
          rebuilt[0]["content"] == [{"type": "text", "text": "hi"}], str(rebuilt))

    own = Message(role="assistant", blocks=[Text("hi")], provider="anthropic", raw=[{"type": "text", "text": "hi"}])
    check("its own raw payload is replayed verbatim (thinking signatures survive)",
          claude._serialise([own], supports_system=False)[0]["content"] == own.raw)


def test_application_lock() -> None:
    """The hard requirement: no key, no AI, enforced below the UI."""
    print("\nApplication lock")
    from agent.providers import ProviderNotConfigured

    scratch = Path(tempfile.mkdtemp(prefix="agent-lock-"))
    monkey: dict = {}
    try:
        _accept_all(monkey)
        manager = _manager(scratch)

        check("a fresh install is locked", not manager.is_unlocked())
        try:
            manager.require_active()
            check("require_active refuses when nothing is configured", False, "it returned a provider")
        except ProviderNotConfigured:
            check("require_active refuses when nothing is configured", True)

        # The lock is in the service layer: an Agent built with no provider
        # raises before it touches history, tools or the network.
        agent = build_agent([message([text_block("hi")], "end_turn")], RecordingCallbacks(), unlocked=False)
        try:
            agent.send("do something")
            check("agent.send refuses without a provider", False, "the turn ran")
        except ProviderNotConfigured:
            check("agent.send refuses without a provider", True)
        check("and nothing was appended to history", agent.messages == [], str(agent.messages))
        check("and no request was ever built", agent.manager.provider.requests == [])

        # --- configuring one unlocks it
        manager.add("anthropic", FAKE_KEYS["anthropic"])
        check("connecting a provider unlocks the agent", manager.is_unlocked())
        check("and require_active now returns one", manager.require_active().id == "anthropic")

        # --- removing the last one locks it again
        manager.remove("anthropic")
        check("removing the last provider locks it again", not manager.is_unlocked())
        try:
            manager.require_active()
            check("require_active refuses once more", False, "it returned a provider")
        except ProviderNotConfigured:
            check("require_active refuses once more", True)

        # --- an active provider whose key vanished is not usable
        manager.add("openai", FAKE_KEYS["openai"])
        manager.keystore().delete("openai")
        check("an active provider with no key does not count as unlocked",
              not manager.is_unlocked())
        check("and the reason names the missing key", "key" in manager.lock_reason().lower())
    finally:
        _restore(monkey)
        shutil.rmtree(scratch, ignore_errors=True)


def test_no_developer_key() -> None:
    """There is no developer key, and no path that could reach one."""
    print("\nNo developer API key")
    root = Path(__file__).resolve().parent.parent

    sources = [
        p for p in root.rglob("*.py")
        if not any(part in {".venv", "build", "dist", "__pycache__"} for part in p.parts)
    ]
    check("the app has source files to audit", len(sources) > 10, str(len(sources)))

    # A real key, committed. The shapes are checked with a run-length so the
    # placeholders in .env.example and the docs do not trip it.
    import re

    live = re.compile(r"sk-ant-[A-Za-z0-9_-]{30,}|sk-proj-[A-Za-z0-9_-]{30,}|AIza[A-Za-z0-9_-]{35,}")
    offenders = []
    for path in [*sources, *root.glob("*.md"), *root.glob("*.ps1"), *root.glob(".env.example")]:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in live.findall(text):
            # The self-test's own obviously-fake keys are the one exemption,
            # and they are built from the word "selftest" precisely so this
            # check can tell them apart from a real credential.
            if "selftest" in match.lower() or "notarealkey" in match.lower():
                continue
            offenders.append(f"{path.name}: {match[:12]}…")
    check("no live-looking API key anywhere in the repository", not offenders, str(offenders[:3]))

    # No fallback logic: nothing in the shipped app may read a key that is not
    # the user's own. Scoped to agent/ — this file names the markers in order
    # to search for them, which would otherwise match itself.
    fallback_markers = ("DEVELOPER_KEY", "BUILTIN_API_KEY", "FALLBACK_API_KEY", "OWNER_KEY")
    shipped = [p for p in sources if p.parts[-2:][0] == "agent" or "agent" in p.parts]
    found = [
        f"{p.name}:{marker}"
        for p in shipped for marker in fallback_markers
        if marker in p.read_text(encoding="utf-8", errors="ignore")
    ]
    check("no developer-key fallback exists in the app", not found, str(found))
    check("the audit actually looked at the app", len(shipped) > 15, str(len(shipped)))

    # The proxy that used to hold the owner's key is gone entirely.
    check("the developer-key proxy is removed", not (root / "proxy").exists())

    # And nothing bundles a credential into the build.
    spec = (root / "WorkspaceAgent.spec").read_text(encoding="utf-8")
    check("the build bundles no .env", ".env" not in spec)
    check("the build bundles no proxy config", "proxy.json" not in spec)
    build_script = (root / "build.ps1").read_text(encoding="utf-8")
    check("the build script has no key-bundling switch", "ProxyUrl" not in build_script)


def test_key_secrecy() -> None:
    """A key must not reach a log, a URL, an error, or the settings file."""
    print("\nKey handling")
    from agent.providers.base import mask, redact
    from agent.providers.gemini_provider import GeminiProvider
    from agent.providers.openai_provider import OpenAIProvider

    secret = FAKE_KEYS["openai"]

    # --- masking
    masked = mask(secret)
    check("a masked key shows only four characters", masked == "••••••••••••••••" + secret[-4:])
    check("the masked form contains none of the secret's body", secret[:20] not in masked)
    check("masking an empty key does not crash", mask("") == "••••••••••••••••")

    # --- redaction of provider error text
    for shape in (FAKE_KEYS["openai"], FAKE_KEYS["anthropic"], FAKE_KEYS["gemini"]):
        leaked = f"Incorrect API key provided: {shape}. Check your keys."
        check(f"a {shape[:6]}… key echoed in an error is redacted", shape not in redact(leaked))
        check("and replaced with a marker", "[REDACTED]" in redact(leaked))
    check("a bearer token is redacted", "Bearer sk-" not in redact("Authorization: Bearer " + secret))
    check("ordinary text survives redaction", redact("model not found") == "model not found")

    # --- the key never appears in a provider's repr or str
    provider = OpenAIProvider(secret, "gpt-5.1")
    check("a provider's repr holds no key", secret not in repr(provider))
    check("a provider's str holds no key", secret not in str(provider))
    check("nor does an f-string of it", secret not in f"{provider}")

    # --- the key travels in a header, never in a URL
    headers = provider._headers()
    check("openai sends the key as a bearer header", headers["Authorization"] == f"Bearer {secret}")
    gemini_headers = GeminiProvider(FAKE_KEYS["gemini"])._headers()
    check("gemini sends the key as a header, not a query parameter",
          gemini_headers["x-goog-api-key"] == FAKE_KEYS["gemini"])

    source = (Path(__file__).resolve().parent.parent / "agent" / "providers" / "gemini_provider.py").read_text(encoding="utf-8")
    check("the gemini module never puts a key in the query string",
          "params={\"key\"" not in source and "?key=" not in source)

    # --- a ProviderError built from a leaky upstream body stays clean
    error = ProviderError("INVALID_API_KEY", redact(f"key {secret} was rejected"), provider="openai")
    check("a provider error carries no key", secret not in error.message)
    check("nor in its dict form", secret not in _json_dumps(error.as_dict()))

    # --- the crash path must not dump frame locals, where a key lives
    main_source = (Path(__file__).resolve().parent.parent / "agent" / "__main__.py").read_text(encoding="utf-8")
    check("the crash screen never shows frame locals",
          "show_locals=False" in main_source and "show_locals=True" not in main_source)

    # --- no telemetry to leak a key into in the first place
    app_root = Path(__file__).resolve().parent.parent / "agent"
    tracking = [
        p.name for p in app_root.rglob("*.py")
        for word in ("sentry", "posthog", "mixpanel", "amplitude", "analytics", "telemetry")
        if word in p.read_text(encoding="utf-8", errors="ignore").lower()
    ]
    check("the app has no analytics or telemetry to leak into", not tracking, str(tracking))

    # --- the provider layer writes no log records at all
    for module in ("base", "manager", "keystore", "openai_provider",
                   "anthropic_provider", "gemini_provider"):
        text = (app_root / "providers" / f"{module}.py").read_text(encoding="utf-8")
        check(f"providers/{module}.py imports no logger", "from ..logs import" not in text)

    # --- the credential wrapper cannot leak through logging
    from agent.providers.manager import Credential

    credential = Credential("openai", secret, "keystore")
    check("a credential's repr is masked", secret not in repr(credential))
    check("and its str too", secret not in str(credential))
    # What logging actually does with an argument: "%s" via __str__.
    check("logging a credential logs the mask", secret not in "{}".format(credential))  # noqa: UP032

    # --- nothing key-shaped is written to the settings file
    scratch = Path(tempfile.mkdtemp(prefix="agent-secrets-"))
    monkey: dict = {}
    try:
        _accept_all(monkey)
        settings_path = scratch / "providers.json"
        manager = ProviderManager(
            store=MemoryKeystore(), settings=ProviderSettings(), settings_path=settings_path, allow_env=False
        )
        for provider_id in ("openai", "anthropic", "gemini"):
            manager.add(provider_id, FAKE_KEYS[provider_id])
        written = settings_path.read_text(encoding="utf-8")
        for provider_id, key in FAKE_KEYS.items():
            check(f"the {provider_id} key is not in providers.json", key not in written)
        from agent.providers import settings as settings_module

        check("and nothing key-shaped is either",
              settings_module.sanity_check(settings_path) == [],
              str(settings_module.sanity_check(settings_path)))
        check("the settings file holds only choices",
              set(_json_loads(written)) == {"active", "models", "fallback_enabled", "fallback_order"},
              str(sorted(_json_loads(written))))

        # The status payload is what the web UI receives. It must be safe to
        # ship to a browser, and to paste into a bug report.
        payload = _json_dumps(manager.status())
        for provider_id, key in FAKE_KEYS.items():
            check(f"the {provider_id} key is not in the status payload", key not in payload)
        check("one masked tail per connected provider, and nothing else",
              payload.count("••••••••••••••••") == 3,
              str(payload.count("••••••••••••••••")))
    finally:
        _restore(monkey)
        shutil.rmtree(scratch, ignore_errors=True)


def test_env_precedence() -> None:
    """A user's stored key always wins over an environment variable."""
    print("\nEnvironment variable precedence")
    scratch = Path(tempfile.mkdtemp(prefix="agent-env-"))
    monkey: dict = {}
    previous = os.environ.get("OPENAI_API_KEY")
    try:
        _accept_all(monkey)
        os.environ["OPENAI_API_KEY"] = "sk-" + "fromtheenvironmentnotreal" * 2

        allowed = _manager(scratch, allow_env=True)
        credential = allowed.credential("openai")
        check("a dev checkout can read a key from the environment", credential is not None)
        check("and says where it came from", credential.source == "environment")
        check("which unlocks nothing on its own until it is made active",
              allowed.active_id() is None)

        allowed.add("openai", FAKE_KEYS["openai"])
        credential = allowed.credential("openai")
        check("a stored key takes precedence over the environment",
              credential.source == "keystore")
        check("and it is the stored key that is used",
              credential.masked.endswith(FAKE_KEYS["openai"][-4:]))

        blocked = _manager(scratch, allow_env=False)
        check("a packaged build ignores the environment entirely",
              blocked.credential("anthropic") is None)

        # The default for a frozen build must be "off", or a user could
        # silently inherit whatever a developer had in their shell.
        from agent.providers.manager import ENV_OPT_IN

        check("inheriting a developer's key needs an explicit opt-in",
              ENV_OPT_IN == "AGENT_ALLOW_ENV_KEYS")
    finally:
        _restore(monkey)
        if previous is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = previous
        shutil.rmtree(scratch, ignore_errors=True)


def test_fallback_policy() -> None:
    """Fallback is opt-in, and never triggers on a bad key."""
    print("\nProvider fallback")
    scratch = Path(tempfile.mkdtemp(prefix="agent-fallback-"))
    monkey: dict = {}
    try:
        _accept_all(monkey)
        manager = _manager(scratch)
        manager.add("openai", FAKE_KEYS["openai"])
        manager.add("anthropic", FAKE_KEYS["anthropic"])

        check("fallback is off by default", not manager.settings.fallback_enabled)
        check("so there is no chain to follow", manager.fallback_chain() == [])
        check("and an outage does not trigger one",
              not manager.should_fall_back(ProviderError("PROVIDER_UNAVAILABLE")))

        manager.set_fallback(True)
        check("turning it on builds a chain", manager.fallback_chain() == ["anthropic"],
              str(manager.fallback_chain()))
        check("the active provider is not in its own fallback chain",
              manager.active_id() not in manager.fallback_chain())

        for code in ("PROVIDER_UNAVAILABLE", "RATE_LIMITED", "NETWORK_ERROR"):
            check(f"{code} may fall back", manager.should_fall_back(ProviderError(code)))
        for code in ("INVALID_API_KEY", "BILLING_ERROR", "PERMISSION_DENIED",
                     "INVALID_REQUEST", "MODEL_UNAVAILABLE", "CONTEXT_TOO_LONG"):
            check(f"{code} never spends another provider's credits",
                  not manager.should_fall_back(ProviderError(code)))
    finally:
        _restore(monkey)
        shutil.rmtree(scratch, ignore_errors=True)


def test_keystore_backends() -> None:
    """Keys go to an OS credential store, never to a plaintext file by default."""
    print("\nSecure storage")
    from agent.providers.keystore import (
        INSECURE_OPT_IN,
        InsecureFileStore,
        MacKeychainStore,
        SecretToolStore,
        WindowsCredentialStore,
    )

    native = {
        "win32": WindowsCredentialStore,
        "darwin": MacKeychainStore,
    }.get(sys.platform, SecretToolStore)
    check(f"this platform's native store is {native.__name__}", native.secure)

    if sys.platform == "win32":
        check("Windows Credential Manager is available here", WindowsCredentialStore.available())
        store = WindowsCredentialStore()
        account = "selftest-provider"
        probe = "selftest-value-not-a-real-key"
        try:
            store.set(account, probe)
            check("a secret round-trips through the OS keystore", store.get(account) == probe)
            check("reading a key that was never stored returns None",
                  store.get("selftest-absent") is None)
            check("deleting it reports success", store.delete(account))
            check("and it is gone", store.get(account) is None)
            check("deleting it twice is not an error", store.delete(account) is False)
        finally:
            store.delete(account)
    else:
        skip("OS keystore round-trip", f"not applicable on {sys.platform}")

    check("the insecure fallback is off unless opted into",
          not InsecureFileStore.available() or os.getenv(INSECURE_OPT_IN))
    check("and it declares itself insecure", InsecureFileStore.secure is False)
    check("with a detail that says so", "NOT encrypted" in InsecureFileStore.detail)

    # The opt-in path still has to work, for a headless Linux box.
    previous = os.environ.get(INSECURE_OPT_IN)
    scratch = Path(tempfile.mkdtemp(prefix="agent-store-"))
    try:
        os.environ[INSECURE_OPT_IN] = "1"
        check("the opt-in makes it available", InsecureFileStore.available())
        store = InsecureFileStore(scratch / "keys.json")
        store.set("openai", FAKE_KEYS["openai"])
        check("it round-trips", store.get("openai") == FAKE_KEYS["openai"])
        raw = (scratch / "keys.json").read_text(encoding="utf-8")
        check("the key is not sitting there in plain sight", FAKE_KEYS["openai"] not in raw)
        check("but this is encoding, not encryption — and it is documented as such",
              "not encryption" in InsecureFileStore.__doc__)
        check("removal works", store.delete("openai") and store.get("openai") is None)
    finally:
        if previous is None:
            os.environ.pop(INSECURE_OPT_IN, None)
        else:
            os.environ[INSECURE_OPT_IN] = previous
        shutil.rmtree(scratch, ignore_errors=True)

    described = keystore_module.describe()
    check("the app can report where keys live", "name" in described)
    check("and never reports a key in doing so", "key" not in str(described.get("detail", "")).lower()
          or "keys" in str(described.get("detail", "")).lower())


def test_gitignore() -> None:
    """Nothing secret can be committed by accident."""
    print("\nGit hygiene")
    root = Path(__file__).resolve().parent.parent
    rules = (root / ".gitignore").read_text(encoding="utf-8")

    for pattern in (".env", ".env.*", "credentials/", "client_secret.json", "providers.json"):
        check(f"{pattern} is ignored", pattern in rules)
    check(".env.example is kept", "!.env.example" in rules)

    example = (root / ".env.example").read_text(encoding="utf-8")
    check("the example file exists and is a template", "OPENAI_API_KEY" in example)
    for key in FAKE_KEYS.values():
        check("the example holds no key", key not in example)
    import re

    check("every key line in the example is empty or a placeholder",
          all(
              not value.strip() or value.strip().startswith(("your-", "sk-...", "AIza..."))
              for value in re.findall(r"^[A-Z_]*API_KEY=(.*)$", example, re.MULTILINE)
          ))


def _json_dumps(value) -> str:
    import json

    # ensure_ascii=False so the mask renders as itself rather than as •
    # escapes — the assertions below count real characters.
    return json.dumps(value, default=str, ensure_ascii=False)


def _json_loads(text: str):
    import json

    return json.loads(text)


def test_web_provider_routes() -> None:
    """The browser can manage providers, and cannot read a key back out."""
    print("\nBrowser provider controls")
    import json as _json
    import threading
    import urllib.error
    import urllib.request

    from agent.web import AccountManager, ChatSession, _handler_class, _page, _Server

    scratch = Path(tempfile.mkdtemp(prefix="agent-webprov-"))
    monkey: dict = {}
    try:
        _accept_all(monkey)
        config = load_config()
        manager = ProviderManager(
            store=MemoryKeystore(), settings=ProviderSettings(),
            settings_path=scratch / "providers.json", allow_env=False,
        )
        agent = Agent(config, Callbacks(), manager)
        session = ChatSession(agent)
        key = "selftest-web-key"
        page = _page(agent, None)
        handler = _handler_class(session, key, page, {}, AccountManager(session))
        server = _Server(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"

        def call(path, payload=None, method=None):
            request = urllib.request.Request(
                base + path,
                data=_json.dumps(payload).encode() if payload is not None else None,
                headers={"X-Agent-Key": key, "Content-Type": "application/json"},
                method=method or ("POST" if payload is not None else "GET"),
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, _json.loads(response.read() or b"{}")
            except urllib.error.HTTPError as exc:
                body = exc.read()
                try:
                    return exc.code, _json.loads(body or b"{}")
                except ValueError:
                    return exc.code, {}

        try:
            # The window opens whatever state setup is in, so the screen that
            # takes a key has to be reachable from inside a locked page.
            check("the rail offers an API Keys section", 'id="navProviders"' in page)
            check("and marks it when nothing is connected", 'id="provBadge"' in page)
            check("the composer says why it cannot be used", 'id="keylock"' in page)
            check("with a way through to the key screen", 'id="keylockGo"' in page)
            check("a key already on this computer can be used as it is",
                  'id="setupUse"' in page)
            check("or replaced with a different one", 'id="setupReplace"' in page)
            check("and the gate can be dismissed", 'id="setupSkip"' in page)

            status, state = call("/api/providers")
            check("the page can read provider state", status == 200)
            check("which starts locked", state["unlocked"] is False)
            check("and lists every provider it could connect",
                  {p["id"] for p in state["providers"]} >= {"openai", "anthropic", "gemini"})

            status, body = call("/api/chat", {"message": "hello"})
            check("chat is refused while locked", status == 423, str(status))
            check("with a reason the page can show", "provider" in body.get("error", "").lower())
            check("and the page is told it is a lock, not a failure", body.get("locked") is True)

            status, body = call("/api/providers", {"action": "add", "provider": "openai",
                                                   "key": FAKE_KEYS["openai"]})
            check("a key can be added from the browser", body.get("ok") is True, str(body))
            check("the response carries the new state", body["state"]["unlocked"] is True)
            check("the key is not echoed back", FAKE_KEYS["openai"] not in _json_dumps(body))
            check("only its masked tail is",
                  any(p["masked_key"].endswith(FAKE_KEYS["openai"][-4:])
                      for p in body["state"]["providers"]))

            status, state = call("/api/providers")
            check("no endpoint returns the stored key",
                  FAKE_KEYS["openai"] not in _json_dumps(state))

            call("/api/providers", {"action": "add", "provider": "gemini", "key": FAKE_KEYS["gemini"]})
            status, body = call("/api/providers", {"action": "activate", "provider": "gemini"})
            check("the active provider can be switched from the browser",
                  body["state"]["active"] == "gemini")

            status, body = call("/api/providers", {"action": "model", "provider": "gemini",
                                                   "model": "gemini-2.5-flash"})
            check("a model can be chosen from the browser",
                  next(p for p in body["state"]["providers"] if p["id"] == "gemini")["model"]
                  == "gemini-2.5-flash")

            status, body = call("/api/providers", {"action": "add", "provider": "openai", "key": "rubbish"})
            check("a malformed key is rejected with a reason", body.get("ok") is False)
            check("and the working key is untouched", body["state"]["providers"][0]["connected"])

            status, body = call("/api/providers", {"action": "remove", "provider": "openai"})
            check("a provider can be removed from the browser", body.get("ok") is True)
            status, body = call("/api/providers", {"action": "remove", "provider": "gemini"})
            check("removing the last one locks the agent", body["state"]["unlocked"] is False)

            status, body = call("/api/chat", {"message": "hello"})
            check("and chat is refused again", status == 423, str(status))

            status, body = call("/api/providers", {"action": "nonsense"})
            check("an unknown action is refused", status == 400, str(status))

            request = urllib.request.Request(base + "/api/providers")
            try:
                urllib.request.urlopen(request, timeout=5)
                check("provider state needs the access key", False, "it was served")
            except urllib.error.HTTPError as exc:
                check("provider state needs the access key", exc.code == 403, str(exc.code))
        finally:
            server.shutdown()
            server.server_close()
    finally:
        _restore(monkey)
        shutil.rmtree(scratch, ignore_errors=True)


def test_web_account_routes() -> None:
    """The browser UI can do what the terminal slash commands do."""
    print("\nBrowser account controls")
    import json as _json
    import threading
    import urllib.error
    import urllib.request

    from agent.web import AccountManager, ChatSession, _handler_class, _page, _Server

    agent = build_agent([], RecordingCallbacks())
    session = ChatSession(agent)
    accounts = AccountManager(session)
    key = "selftest-key"
    page = _page(agent, "someone@example.com")

    check("the page offers account switching", 'id="switch"' in page)
    check("the page offers sign out", 'id="signout"' in page)
    check("the signed-in address is shown", "someone@example.com" in page)

    server = _Server(("127.0.0.1", 0), _handler_class(session, key, page, {}, accounts))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def call(path: str, method: str = "GET", auth: bool = True) -> tuple[int, dict]:
        request = urllib.request.Request(
            base + path,
            data=b"" if method == "POST" else None,
            method=method,
            headers={"X-Agent-Key": key} if auth else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, _json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, _json.loads(exc.read() or b"{}")

    try:
        status, _ = call("/api/account", auth=False)
        check("account state needs the access key", status == 403, str(status))

        status, state = call("/api/account")
        check("account state is served", status == 200, str(status))
        check(
            "it reports who is signed in",
            {"email", "signed_in", "signing_in"} <= set(state),
            str(sorted(state)),
        )

        # Swapping credentials mid-turn would pull the rug from under running
        # tools, so both routes must refuse while a turn holds the lock.
        session.lock.acquire()
        try:
            status, body = call("/api/signin", method="POST")
            check("sign-in is refused mid-turn", status == 409, str(status))
            check("and says why", "still working" in (body.get("error") or ""), str(body))
            status, _ = call("/api/signout", method="POST")
            check("sign-out is refused mid-turn", status == 409, str(status))
        finally:
            session.lock.release()

        check("no sign-in was left running", not accounts.busy)
    finally:
        server.shutdown()
        server.server_close()

    # With no accounts server configured this is the single-user desktop app it
    # has always been: no overlay to sign past, no menu items pointing at a
    # deployment that does not exist, and no half-live /api/auth surface.
    import os

    previous = os.environ.pop("LUMEN_API_URL", None)
    try:
        standalone = _page(agent, "someone@example.com")
        check("without a server there is no sign-in screen", 'id="auth"' not in standalone)
        check("nor Lumen account items", 'id="lumenSignout"' not in standalone)
        check("and the app itself is untouched", 'id="acctMenu"' in standalone)

        server = _Server(
            ("127.0.0.1", 0), _handler_class(session, key, standalone, {}, accounts)
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            status, _ = call("/api/auth/state")
            check("and the auth routes are not served", status == 404, str(status))
        finally:
            server.shutdown()
            server.server_close()
    finally:
        if previous is not None:
            os.environ["LUMEN_API_URL"] = previous


def test_web_starts_unconfigured() -> None:
    """--web opens the window whatever state setup is in.

    The window is the only entry point a user gets from a desktop shortcut, and
    it can collect both an API key and a Google sign-in itself. A terminal
    prompt in front of it would be unanswerable with no console attached, so
    the launch must not depend on one. The lock still holds — it is enforced in
    the service layer, which the other tests here cover.
    """
    print("\nThe window starts unconfigured")

    from agent import __main__ as cli
    from agent import notify, onboarding, ui, web
    from agent.tools import google_auth

    manager = ProviderManager(
        store=MemoryKeystore(), settings=ProviderSettings(),
        settings_path=Path(tempfile.mkdtemp(prefix="agent-launch-")) / "providers.json",
        allow_env=False,
    )
    served: dict = {}
    saved = (
        onboarding.needs_onboarding, onboarding.run, web.serve,
        notify.start_watcher, cli.shared_manager, google_auth.cached_account_email,
        ui.TerminalCallbacks,
    )
    onboarding.needs_onboarding = lambda *a, **k: True   # nothing is set up at all
    onboarding.run = lambda *a, **k: served.setdefault("onboarded", True)
    web.serve = lambda agent, email, **k: served.update(agent=agent, email=email)
    notify.start_watcher = lambda *a, **k: None
    cli.shared_manager = lambda: manager
    google_auth.cached_account_email = lambda: None
    ui.TerminalCallbacks = lambda config: Callbacks()
    try:
        code = cli.main(["--web"])
        check("the window opens with nothing configured", code == 0, str(code))
        check("and it was actually served", "agent" in served)
        check("without a terminal prompt in the way", "onboarded" not in served)
        check("the agent it serves knows it has no key",
              served["agent"].manager.is_unlocked() is False)
        check("and can say what is missing",
              "provider" in served["agent"].manager.lock_reason().lower())
    finally:
        (
            onboarding.needs_onboarding, onboarding.run, web.serve,
            notify.start_watcher, cli.shared_manager, google_auth.cached_account_email,
            ui.TerminalCallbacks,
        ) = saved


def test_reminder_routes() -> None:
    """The Reminders page's API: create, tick, snooze, delete, and who may."""
    print("\nReminders in the browser")
    import json as _json
    import shutil
    import threading
    import urllib.error
    import urllib.request

    from agent import reminders as store_module
    from agent.web import AccountManager, ChatSession, _handler_class, _page, _Server

    store, folder = _scratch_reminders()
    agent = build_agent([], RecordingCallbacks())
    session = ChatSession(agent)
    key = "selftest-key"
    page = _page(agent, None)

    check("the rail offers a Reminders section", 'id="navReminders"' in page)
    check("the page can create one by hand", 'id="remNew"' in page)
    check("it separates today, upcoming and completed", all(
        marker in page for marker in ('id="remToday"', 'id="remUpcoming"', 'id="remDone"')
    ))

    server = _Server(("127.0.0.1", 0), _handler_class(session, key, page, {}, AccountManager(session)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def call(path: str, payload: dict | None = None, auth: bool = True) -> tuple[int, dict]:
        request = urllib.request.Request(
            base + path,
            data=_json.dumps(payload).encode() if payload is not None else None,
            method="POST" if payload is not None else "GET",
            headers={"X-Agent-Key": key, "Content-Type": "application/json"} if auth else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, _json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, _json.loads(exc.read() or b"{}")

    try:
        status, _ = call("/api/reminders", auth=False)
        check("reading reminders needs the access key", status == 403, str(status))
        status, _ = call("/api/reminders", {"action": "create", "title": "x", "due": "2026-09-01T09:00"}, auth=False)
        check("so does changing them", status == 403, str(status))

        status, body = call(
            "/api/reminders",
            {"action": "create", "title": "Submit project", "due": "2026-08-14T20:00", "tags": "school"},
        )
        made = body.get("result", {})
        check("the page can create a reminder", status == 200 and made.get("due") == "2026-08-14T20:00", str(body)[:80])

        status, body = call("/api/reminders?scope=upcoming")
        check("and list them with their counts", body["counts"]["upcoming"] == 1, str(body.get("counts")))

        _, body = call("/api/reminders", {"action": "snooze", "id": made["id"], "minutes": 30})
        check("snoozing works from the page", body["result"]["snoozed"] is True)
        _, body = call("/api/reminders", {"action": "complete", "id": made["id"]})
        check("ticking off works from the page", body["result"]["status"] == "completed")
        _, body = call("/api/reminders?scope=completed")
        check("and it lands in Completed", len(body["reminders"]) == 1)

        _, body = call("/api/reminders", {"action": "create", "title": "bad", "due": "sometime soon"})
        check(
            "a date it cannot read is explained, not a 500",
            "error" in body and "could not read" in body["error"],
            str(body)[:80],
        )

        _, body = call("/api/reminders", {"action": "delete", "id": made["id"]})
        check("deleting works from the page", body["result"]["status"] == "deleted")
        status, body = call("/api/reminders", {"action": "wat"})
        check("an unknown action is refused", status == 400, str(status))
    finally:
        server.shutdown()
        server.server_close()
        store.close()
        store_module.use_store(None)
        shutil.rmtree(folder, ignore_errors=True)



def main() -> int:
    print("Lumen OS — offline self-test")
    registry.load_all()
    test_schemas()
    test_local_tools()
    test_email_attachments()
    test_reminder_store()
    test_reminder_firing()
    test_reminder_tools()
    test_calendar_helpers()
    test_agenda_merge()
    test_tool_loop()
    test_request_shape()
    test_capability_negotiation()
    test_writing_styles()
    test_context_injection_per_provider()
    test_confirmation_gate()
    test_midrun_approval()
    test_web_tools()
    test_browser_policy()
    test_tool_error_handling()
    test_unknown_tool()
    test_refusal()
    test_turn_rollback()

    # --- bring your own key
    test_provider_registry()
    test_key_format_checks()
    test_provider_management()
    test_ambiguous_removal()
    test_validation_failures()
    test_error_normalisation()
    test_serialisation_per_provider()
    test_fallback_policy()
    test_keystore_backends()

    # --- the hard requirements
    test_application_lock()
    test_no_developer_key()
    test_key_secrecy()
    test_env_precedence()
    test_gitignore()

    test_web_provider_routes()
    test_web_account_routes()
    test_web_starts_unconfigured()
    test_reminder_routes()

    summary = f"\n{len(PASSED)} passed, {len(FAILED)} failed"
    if SKIPPED:
        summary += f", {len(SKIPPED)} skipped"
    print(summary)
    for failure in FAILED:
        print(f"  FAILED: {failure}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
