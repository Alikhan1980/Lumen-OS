"""Reminders the app owns.

Not a view onto Google Tasks or anything else: one SQLite file next to the
user's other data is the source of truth, and everything — the agent tools, the
Reminders page, and the notifier that fires while the app is shut — reads and
writes through this module.

Two tables. `reminders` holds one row per reminder, which for a recurring one
means the *series*: the row always points at its next occurrence. `reminder_log`
records what happened to each occurrence — fired, completed, snoozed — which is
what gives a daily reminder a completion history instead of a single flag.

Time is stored twice on purpose. `due_local` is the wall clock the user asked
for ("17:00"), `due_utc` is the instant that resolves to. Recurrence advances
the wall clock and re-resolves the instant, so a daily 5pm reminder is still at
5pm the day the clocks change. The zone is the machine's own unless an IANA name
is given, and travels with the row either way.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import DATA_DIR
from .logs import logger

log = logger("reminders")

DB_PATH = DATA_DIR / "reminders.db"

# A reminder the machine slept through still fires, but says it is late rather
# than pretending it arrived on time.
LATE_AFTER = timedelta(minutes=2)

STATUSES = ("pending", "completed", "cancelled")
SCOPES = ("today", "upcoming", "overdue", "completed", "all")

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    notes         TEXT NOT NULL DEFAULT '',
    due_utc       TEXT NOT NULL,
    due_local     TEXT NOT NULL,
    tz            TEXT NOT NULL DEFAULT '',
    recurrence    TEXT NOT NULL DEFAULT 'none',
    tags          TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',
    created_utc   TEXT NOT NULL,
    updated_utc   TEXT NOT NULL,
    completed_utc TEXT,
    notified_utc  TEXT,
    snoozed_from  TEXT
);
CREATE INDEX IF NOT EXISTS reminders_due ON reminders(status, due_utc);

CREATE TABLE IF NOT EXISTS reminder_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id    TEXT NOT NULL,
    kind           TEXT NOT NULL,
    at_utc         TEXT NOT NULL,
    occurrence_utc TEXT,
    detail         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS reminder_log_ref ON reminder_log(reminder_id, at_utc);
"""


class ReminderError(ValueError):
    """Something about the request cannot be satisfied — a bad time, no match."""


# ------------------------------------------------------------------ time


def _zone(name: str | None):
    """A tzinfo for an IANA name, or None to mean 'this machine'."""
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception as exc:  # no tzdata on this box, or a name that isn't one
        raise ReminderError(
            f"unknown timezone {name!r} ({exc}). Leave it out to use this "
            "computer's own timezone."
        ) from exc


def local_zone_name() -> str:
    """The machine's zone, as an IANA name when Windows will give us one."""
    try:
        from zoneinfo import ZoneInfo  # noqa: F401

        import tzlocal  # type: ignore

        return str(tzlocal.get_localzone_name() or "")
    except Exception:
        # No tzlocal: the abbreviation is still worth recording for display.
        return datetime.now().astimezone().tzname() or ""


_ISO_CLEAN = re.compile(r"\s+")


def parse_due(value: str, timezone: str | None = None) -> tuple[str, str, str]:
    """Turn what the caller wrote into (due_utc, due_local, tz).

    Accepts 'YYYY-MM-DD HH:MM', 'YYYY-MM-DDTHH:MM[:SS]', a full offset-carrying
    ISO stamp, or a bare 'YYYY-MM-DD' (which means 9am, since a reminder has to
    ring at some point in the day). Naive times mean local time — the agent is
    told the current local time each turn, so that is what it works in.
    """
    text = _ISO_CLEAN.sub(" ", (value or "").strip()).replace(" ", "T", 1)
    if not text:
        raise ReminderError("a reminder needs a date and time")
    if len(text) == 10:
        text += "T09:00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ReminderError(
            f"could not read {value!r} as a date and time. Use "
            "'YYYY-MM-DD HH:MM', e.g. '2026-08-09 17:00'."
        ) from exc

    zone = _zone(timezone)
    if parsed.tzinfo is None:
        # Attach the intended zone: the named one, or this machine's.
        aware = parsed.replace(tzinfo=zone) if zone else parsed.astimezone()
        wall = parsed
    else:
        aware = parsed.astimezone(zone) if zone else parsed
        wall = aware.replace(tzinfo=None)

    name = timezone or local_zone_name()
    return aware.astimezone(UTC).isoformat(), wall.isoformat(timespec="minutes"), name


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


# ------------------------------------------------------------ recurrence

WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
RECURRENCES = ("none", "daily", "weekdays", "weekly", "monthly", "yearly")


def normalise_recurrence(rule: str | None) -> str:
    """Accept the shorthands people say, return the stored form.

    'weekly:MO,WE' is the general weekly case; plain 'weekly' repeats on
    whatever weekday the reminder already falls on.
    """
    text = (rule or "none").strip().lower().replace(" ", "")
    if text in ("", "none", "once", "never"):
        return "none"
    if text in ("everyday", "every_day", "day"):
        return "daily"
    if text in ("weekday", "weekdays", "everyweekday"):
        return "weekdays"
    if text == "week":
        return "weekly"
    if text == "month":
        return "monthly"
    if text in ("year", "annually"):
        return "yearly"
    if text.startswith("weekly:"):
        days = [d.strip().upper()[:2] for d in text.split(":", 1)[1].split(",") if d.strip()]
        unknown = [d for d in days if d not in WEEKDAYS]
        if unknown or not days:
            raise ReminderError(
                f"weekly recurrence needs day codes from {', '.join(WEEKDAYS)} "
                f"(got {text.split(':', 1)[1]!r})"
            )
        return "weekly:" + ",".join(sorted(set(days), key=lambda d: WEEKDAYS[d]))
    if text in RECURRENCES:
        return text
    raise ReminderError(
        f"unknown recurrence {rule!r}. Use one of: {', '.join(RECURRENCES)}, "
        "or 'weekly:MO,WE' for particular weekdays."
    )


def describe_recurrence(rule: str) -> str:
    """How to say the rule out loud."""
    if rule == "none":
        return ""
    if rule.startswith("weekly:"):
        names = {"MO": "Mon", "TU": "Tue", "WE": "Wed", "TH": "Thu", "FR": "Fri", "SA": "Sat", "SU": "Sun"}
        days = [names[d] for d in rule.split(":", 1)[1].split(",")]
        return "every " + ", ".join(days)
    return {
        "daily": "every day",
        "weekdays": "every weekday",
        "weekly": "every week",
        "monthly": "every month",
        "yearly": "every year",
    }.get(rule, rule)


def _add_months(moment: datetime, months: int) -> datetime:
    """Same day next month, clamped — the 31st of a 30-day month is the 30th."""
    month = moment.month - 1 + months
    year = moment.year + month // 12
    month = month % 12 + 1
    for day in range(moment.day, 27, -1):
        try:
            return moment.replace(year=year, month=month, day=day)
        except ValueError:
            continue
    return moment.replace(year=year, month=month, day=min(moment.day, 28))


def next_local(after: datetime, rule: str) -> datetime | None:
    """The next wall-clock occurrence strictly after `after`, or None if once."""
    if rule == "none":
        return None
    if rule == "daily":
        return after + timedelta(days=1)
    if rule == "weekdays":
        moment = after + timedelta(days=1)
        while moment.weekday() >= 5:
            moment += timedelta(days=1)
        return moment
    if rule == "weekly":
        return after + timedelta(days=7)
    if rule == "monthly":
        return _add_months(after, 1)
    if rule == "yearly":
        try:
            return after.replace(year=after.year + 1)
        except ValueError:  # 29 February
            return after.replace(year=after.year + 1, day=28)
    if rule.startswith("weekly:"):
        wanted = sorted(WEEKDAYS[d] for d in rule.split(":", 1)[1].split(","))
        for step in range(1, 8):
            moment = after + timedelta(days=step)
            if moment.weekday() in wanted:
                return moment
        return None
    return None


def advance_past(due_local: datetime, rule: str, now_local: datetime) -> tuple[datetime, int]:
    """Roll a recurring reminder forward to its next future occurrence.

    Returns the new wall clock and how many occurrences were stepped over — the
    ones that fell while the computer was off, which the notification mentions
    rather than firing a dozen toasts for.
    """
    moment, skipped = due_local, 0
    for _ in range(500):  # a hard stop; 500 daily steps is over a year asleep
        following = next_local(moment, rule)
        if following is None:
            return moment, skipped
        moment = following
        if moment > now_local:
            return moment, skipped
        skipped += 1
    return moment, skipped


# ----------------------------------------------------------------- store


@dataclass
class Store:
    """The reminders database. One per process; `store()` hands out the shared one."""

    path: Path = DB_PATH
    _db: sqlite3.Connection = field(init=False)
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the web server answers on a thread pool and
        # the watcher sweeps on its own thread. Every access takes _lock.
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        # WAL so the notifier process can read while the app holds the file, and
        # a busy timeout so the two of them queue instead of raising.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(SCHEMA)
        self._db.commit()

    # -- helpers ---------------------------------------------------------

    def _row(self, reminder_id: str) -> sqlite3.Row:
        row = self._db.execute(
            "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
        if row is None:
            raise ReminderError(f"no reminder with id {reminder_id!r}")
        return row

    def _log(self, reminder_id: str, kind: str, occurrence: str | None, detail: str = "") -> None:
        self._db.execute(
            "INSERT INTO reminder_log (reminder_id, kind, at_utc, occurrence_utc, detail)"
            " VALUES (?, ?, ?, ?, ?)",
            (reminder_id, kind, _iso(_now_utc()), occurrence, detail),
        )

    # -- writes ----------------------------------------------------------

    def create(
        self,
        title: str,
        due: str,
        notes: str = "",
        recurrence: str | None = None,
        tags: str = "",
        timezone: str | None = None,
    ) -> dict:
        clean_title = (title or "").strip()
        if not clean_title:
            raise ReminderError("a reminder needs a title")
        due_utc, due_local, tz = parse_due(due, timezone)
        rule = normalise_recurrence(recurrence)
        now = _iso(_now_utc())
        reminder_id = uuid.uuid4().hex[:12]

        with self._lock:
            self._db.execute(
                "INSERT INTO reminders (id, title, notes, due_utc, due_local, tz,"
                " recurrence, tags, status, created_utc, updated_utc)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    reminder_id, clean_title, (notes or "").strip(), due_utc, due_local,
                    tz, rule, _clean_tags(tags), now, now,
                ),
            )
            self._log(reminder_id, "created", due_utc)
            self._db.commit()
            row = self._row(reminder_id)
        log.info("created %s %r due %s (%s)", reminder_id, clean_title, due_local, rule or "once")
        return as_dict(row)

    def update(
        self,
        reminder_id: str,
        title: str | None = None,
        due: str | None = None,
        notes: str | None = None,
        recurrence: str | None = None,
        tags: str | None = None,
        timezone: str | None = None,
    ) -> dict:
        patch: dict = {}
        if title is not None:
            if not title.strip():
                raise ReminderError("a reminder needs a title")
            patch["title"] = title.strip()
        if notes is not None:
            patch["notes"] = notes.strip()
        if tags is not None:
            patch["tags"] = _clean_tags(tags)
        if recurrence is not None:
            patch["recurrence"] = normalise_recurrence(recurrence)
        if due is not None:
            patch["due_utc"], patch["due_local"], patch["tz"] = parse_due(due, timezone)
            # A moved reminder has not been announced at its new time.
            patch["notified_utc"] = None
            patch["snoozed_from"] = None
        if not patch:
            with self._lock:
                return as_dict(self._row(reminder_id))

        patch["updated_utc"] = _iso(_now_utc())
        # Column names come from this function's own vocabulary, never from the
        # caller; every value is bound. S608 cannot see that.
        assignments = ", ".join(f"{column} = ?" for column in patch)
        with self._lock:
            self._row(reminder_id)  # raises if it is gone
            self._db.execute(
                f"UPDATE reminders SET {assignments} WHERE id = ?",  # noqa: S608
                (*patch.values(), reminder_id),
            )
            self._log(reminder_id, "updated", patch.get("due_utc"), ", ".join(patch))
            self._db.commit()
            row = self._row(reminder_id)
        log.info("updated %s (%s)", reminder_id, ", ".join(patch))
        return as_dict(row)

    def complete(self, reminder_id: str) -> dict:
        """Tick it off. A recurring one rolls on to its next occurrence."""
        now = _now_utc()
        with self._lock:
            row = self._row(reminder_id)
            rule = row["recurrence"]
            if rule == "none":
                self._db.execute(
                    "UPDATE reminders SET status = 'completed', completed_utc = ?,"
                    " updated_utc = ? WHERE id = ?",
                    (_iso(now), _iso(now), reminder_id),
                )
                self._log(reminder_id, "completed", row["due_utc"])
                outcome = "completed"
            else:
                current = datetime.fromisoformat(row["due_local"])
                following = next_local(current, rule) or current
                due_utc, due_local, tz = parse_due(following.isoformat(), _stored_tz(row))
                self._db.execute(
                    "UPDATE reminders SET due_utc = ?, due_local = ?, tz = ?,"
                    " notified_utc = NULL, snoozed_from = NULL, updated_utc = ?"
                    " WHERE id = ?",
                    (due_utc, due_local, tz, _iso(now), reminder_id),
                )
                self._log(reminder_id, "completed", row["due_utc"], "occurrence")
                outcome = "occurrence_completed"
            self._db.commit()
            fresh = as_dict(self._row(reminder_id))
        log.info("completed %s (%s)", reminder_id, outcome)
        return {**fresh, "outcome": outcome}

    def reopen(self, reminder_id: str) -> dict:
        """Undo a completion — the checkbox has to go both ways."""
        with self._lock:
            self._row(reminder_id)
            self._db.execute(
                "UPDATE reminders SET status = 'pending', completed_utc = NULL,"
                " updated_utc = ? WHERE id = ?",
                (_iso(_now_utc()), reminder_id),
            )
            self._log(reminder_id, "reopened", None)
            self._db.commit()
            return as_dict(self._row(reminder_id))

    def snooze(self, reminder_id: str, minutes: int = 10) -> dict:
        """Push it out from now, not from when it was due."""
        minutes = max(1, min(int(minutes), 60 * 24 * 14))
        with self._lock:
            row = self._row(reminder_id)
            later = datetime.now() + timedelta(minutes=minutes)
            due_utc, due_local, tz = parse_due(later.isoformat(), _stored_tz(row))
            self._db.execute(
                "UPDATE reminders SET due_utc = ?, due_local = ?, tz = ?, status = 'pending',"
                " notified_utc = NULL, snoozed_from = COALESCE(snoozed_from, ?),"
                " completed_utc = NULL, updated_utc = ? WHERE id = ?",
                (due_utc, due_local, tz, row["due_utc"], _iso(_now_utc()), reminder_id),
            )
            self._log(reminder_id, "snoozed", row["due_utc"], f"{minutes}m")
            self._db.commit()
            fresh = as_dict(self._row(reminder_id))
        log.info("snoozed %s by %sm -> %s", reminder_id, minutes, fresh["due_local"])
        return fresh

    def delete(self, reminder_id: str) -> dict:
        with self._lock:
            row = self._row(reminder_id)
            self._db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
            self._log(reminder_id, "deleted", row["due_utc"], row["title"])
            self._db.commit()
        log.info("deleted %s %r", reminder_id, row["title"])
        return {"status": "deleted", "id": reminder_id, "title": row["title"]}

    # -- reads -----------------------------------------------------------

    def get(self, reminder_id: str) -> dict:
        with self._lock:
            return as_dict(self._row(reminder_id))

    def list(
        self,
        scope: str = "upcoming",
        search: str | None = None,
        tag: str | None = None,
        limit: int = 50,
        newest_first: bool = False,
    ) -> list[dict]:
        if scope not in SCOPES:
            raise ReminderError(f"scope must be one of {', '.join(SCOPES)}")
        now = _now_utc()
        clauses: list[str] = []
        params: list = []

        if scope == "completed":
            clauses.append("status = 'completed'")
        elif scope == "overdue":
            clauses += ["status = 'pending'", "due_utc <= ?"]
            params.append(_iso(now))
        elif scope == "today":
            midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            clauses += ["status = 'pending'", "due_utc < ?"]
            params.append(_iso((midnight + timedelta(days=1)).astimezone()))
        elif scope == "upcoming":
            clauses.append("status = 'pending'")
        # 'all' adds nothing

        if search:
            clauses.append("(LOWER(title) LIKE ? OR LOWER(notes) LIKE ? OR LOWER(tags) LIKE ?)")
            needle = f"%{search.strip().lower()}%"
            params += [needle, needle, needle]
        if tag:
            clauses.append("LOWER(tags) LIKE ?")
            params.append(f"%{tag.strip().lower()}%")

        # Both fragments are assembled from literals chosen above — the search
        # text and the tag are bound parameters, not interpolated.
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "due_utc DESC" if (newest_first or scope == "completed") else "due_utc ASC"
        sql = f"SELECT * FROM reminders {where} ORDER BY {order} LIMIT ?"  # noqa: S608
        params.append(max(1, min(int(limit), 500)))

        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [as_dict(row) for row in rows]

    def history(self, reminder_id: str, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT kind, at_utc, occurrence_utc, detail FROM reminder_log"
                " WHERE reminder_id = ? ORDER BY id DESC LIMIT ?",
                (reminder_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [dict(row) for row in rows]

    def recently_fired(self, within_minutes: int = 5) -> list[dict]:
        """What has rung lately — how an open page knows to show a banner."""
        since = _iso(_now_utc() - timedelta(minutes=max(1, within_minutes)))
        with self._lock:
            rows = self._db.execute(
                "SELECT l.reminder_id, l.at_utc, l.detail, r.title, r.due_local, r.status"
                " FROM reminder_log l LEFT JOIN reminders r ON r.id = l.reminder_id"
                " WHERE l.kind = 'fired' AND l.at_utc >= ? ORDER BY l.at_utc DESC LIMIT 20",
                (since,),
            ).fetchall()
        return [dict(row) for row in rows]

    def counts(self) -> dict:
        now = _iso(_now_utc())
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow = _iso((midnight + timedelta(days=1)).astimezone())
        with self._lock:
            def one(sql: str, args: tuple) -> int:
                return int(self._db.execute(sql, args).fetchone()[0])

            return {
                "today": one(
                    "SELECT COUNT(*) FROM reminders WHERE status='pending' AND due_utc < ?",
                    (tomorrow,),
                ),
                "overdue": one(
                    "SELECT COUNT(*) FROM reminders WHERE status='pending' AND due_utc <= ?",
                    (now,),
                ),
                "upcoming": one(
                    "SELECT COUNT(*) FROM reminders WHERE status='pending'", ()
                ),
                "completed": one(
                    "SELECT COUNT(*) FROM reminders WHERE status='completed'", ()
                ),
            }

    # -- firing ----------------------------------------------------------

    def claim_due(self, now: datetime | None = None) -> list[dict]:
        """Take ownership of everything due, so exactly one process announces it.

        The claim is the `notified_utc IS NULL` guard inside the UPDATE: the app
        and the scheduled task both run this, and only one of them can win a
        given occurrence. A recurring reminder rolls forward as it is claimed,
        so it is ready to ring again tomorrow whether or not anyone ticks it.
        """
        moment = now or _now_utc()
        claimed: list[dict] = []
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM reminders WHERE status = 'pending'"
                " AND notified_utc IS NULL AND due_utc <= ? ORDER BY due_utc",
                (_iso(moment),),
            ).fetchall()

            for row in rows:
                taken = self._db.execute(
                    "UPDATE reminders SET notified_utc = ? WHERE id = ? AND notified_utc IS NULL",
                    (_iso(moment), row["id"]),
                )
                if taken.rowcount != 1:
                    continue  # the other process got there first

                due_utc = datetime.fromisoformat(row["due_utc"])
                lateness = moment - due_utc
                late = lateness > LATE_AFTER
                entry = {
                    **as_dict(row),
                    "late": late,
                    "late_minutes": int(lateness.total_seconds() // 60) if late else 0,
                    "skipped_occurrences": 0,
                }

                if row["recurrence"] != "none":
                    current = datetime.fromisoformat(row["due_local"])
                    following, skipped = advance_past(current, row["recurrence"], datetime.now())
                    due, due_local, tz = parse_due(following.isoformat(), _stored_tz(row))
                    self._db.execute(
                        "UPDATE reminders SET due_utc = ?, due_local = ?, tz = ?,"
                        " notified_utc = NULL, snoozed_from = NULL WHERE id = ?",
                        (due, due_local, tz, row["id"]),
                    )
                    entry["skipped_occurrences"] = skipped
                    entry["next_due_local"] = due_local

                self._log(
                    row["id"], "fired", row["due_utc"], "late" if late else ""
                )
                if late:
                    self._log(row["id"], "missed", row["due_utc"], f"{entry['late_minutes']}m late")
                claimed.append(entry)

            self._db.commit()
        return claimed

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _clean_tags(tags: str | None) -> str:
    parts = [t.strip().lstrip("#") for t in (tags or "").replace(";", ",").split(",")]
    return ",".join(sorted({p for p in parts if p}))


def _stored_tz(row: sqlite3.Row) -> str | None:
    """The row's zone, if it is one zoneinfo will accept."""
    name = row["tz"] or ""
    return name if "/" in name else None


def as_dict(row: sqlite3.Row) -> dict:
    """The shape everything outside this module sees."""
    due_local = row["due_local"]
    due_utc = datetime.fromisoformat(row["due_utc"])
    overdue = row["status"] == "pending" and due_utc <= _now_utc()
    return {
        "id": row["id"],
        "title": row["title"],
        "notes": row["notes"],
        "due": due_local,
        "due_local": due_local,
        "due_utc": row["due_utc"],
        "timezone": row["tz"],
        "recurrence": row["recurrence"],
        "recurs": describe_recurrence(row["recurrence"]),
        "tags": [t for t in row["tags"].split(",") if t],
        "status": row["status"],
        "overdue": overdue,
        "notified": bool(row["notified_utc"]),
        "snoozed": bool(row["snoozed_from"]),
        "created": row["created_utc"],
        "completed": row["completed_utc"],
    }


# One store per process, opened on first use so importing this module never
# touches the disk (the self-test imports the whole package to read schemas).
# Held in a dict rather than a bare name so the accessors do not need `global`.
_current: dict[str, Store | None] = {"store": None}
_store_lock = threading.Lock()


def store() -> Store:
    with _store_lock:
        if _current["store"] is None:
            _current["store"] = Store()
        return _current["store"]


def use_store(replacement: Store | None) -> None:
    """Point the module at another database. For tests."""
    with _store_lock:
        _current["store"] = replacement
