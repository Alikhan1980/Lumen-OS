"""Getting a reminder in front of the user, whether or not the app is running.

Three parts:

**The toast.** A real Windows notification, raised through WinRT from a short
PowerShell script. No dependency, and it lands in Action Center like any other
notification, so one that appears while the screen is locked is still there
afterwards.

**The sweep.** `deliver_due()` claims everything that has come due and announces
it. Claiming happens in the database, so the app and the scheduled task can both
run it and a reminder is still announced exactly once.

**The schedule.** A Windows scheduled task runs the sweep every minute under the
user's own account. That is what makes reminders independent of the agent: the
app can be closed, and the model is not involved at the moment a reminder fires.
The task is not created behind the user's back — `--reminders-install`, or the
button on the Reminders page, asks for it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from .config import ROOT, is_frozen
from .logs import logger
from .reminders import store

log = logger("notify")

TASK_NAME = "Lumen OS Reminders"
SWEEP_SECONDS = 30

# Toasts have to be raised by something with a Start-menu identity. PowerShell's
# own is always present, which is why notifications show as coming from it.
POWERSHELL_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

# Keeps a console window from blinking on screen every time the sweep runs.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _system32(name: str) -> str:
    """Full path to a Windows tool, so PATH cannot decide what we run."""
    root = os.environ.get("SYSTEMROOT") or r"C:\Windows"
    candidates = {
        "powershell.exe": Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
        "schtasks.exe": Path(root) / "System32" / "schtasks.exe",
    }
    found = candidates.get(name)
    return str(found) if found and found.exists() else name


def _powershell(script: str, extra_env: dict[str, str] | None = None, timeout: int = 30):
    """Run a PowerShell script from a temp file. Values arrive by environment.

    Passing arguments through the environment rather than the command line is
    what keeps a reminder titled `it's "done" & dusted` from breaking the quoting
    — nothing user-supplied is ever parsed as PowerShell.
    """
    if sys.platform != "win32":
        raise RuntimeError("notifications are implemented for Windows only")

    # Not a context manager: the file has to outlive this block so the child
    # process can read it, and the finally below is what removes it.
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
        "w", suffix=".ps1", delete=False, encoding="utf-8-sig"
    )
    try:
        handle.write(script)
        handle.close()
        environment = {**os.environ, **(extra_env or {})}
        # check=False on purpose: every caller reads returncode and turns a
        # failure into a message rather than an exception.
        return subprocess.run(  # noqa: S603
            [
                _system32("powershell.exe"), "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", handle.name,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
            creationflags=_NO_WINDOW,
            check=False,
        )
    finally:
        Path(handle.name).unlink(missing_ok=True)


# --------------------------------------------------------------------- toast

_TOAST_PS = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($env:LUMEN_TOAST_XML)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:LUMEN_TOAST_APPID).Show($toast)
"""


def toast(title: str, message: str, subtitle: str = "") -> bool:
    """Raise a Windows notification. False if the platform would not show it."""
    lines = "".join(
        f"<text>{xml_escape(part)}</text>"
        for part in (title, message, subtitle)
        if part
    )
    xml = (
        '<toast duration="long" scenario="reminder">'
        f"<visual><binding template=\"ToastGeneric\">{lines}</binding></visual>"
        '<audio src="ms-winsoundevent:Notification.Reminder"/>'
        "<actions>"
        '<action content="Got it" arguments="dismiss" activationType="system"/>'
        "</actions>"
        "</toast>"
    )
    try:
        result = _powershell(
            _TOAST_PS,
            {"LUMEN_TOAST_XML": xml, "LUMEN_TOAST_APPID": POWERSHELL_AUMID},
        )
    except Exception as exc:
        log.warning("toast failed: %s: %s", type(exc).__name__, exc)
        return False
    if result.returncode != 0:
        log.warning("toast failed: %s", (result.stderr or "").strip()[:300])
        return False
    return True


def _when(reminder: dict) -> str:
    try:
        moment = datetime.fromisoformat(reminder["due_local"])
    except (ValueError, KeyError):
        return ""
    return moment.strftime("%a %d %b, %H:%M")


def announce(reminder: dict) -> bool:
    """Say one reminder out loud, in the terms the moment calls for."""
    if reminder.get("late"):
        minutes = reminder.get("late_minutes") or 0
        span = f"{minutes} min" if minutes < 90 else f"{minutes // 60} h"
        headline = f"Missed reminder · was due {_when(reminder)} ({span} ago)"
    else:
        headline = f"Reminder · {_when(reminder)}"

    detail = reminder.get("notes") or ""
    if reminder.get("skipped_occurrences"):
        skipped = reminder["skipped_occurrences"]
        detail = (detail + " " if detail else "") + f"({skipped} earlier one(s) missed)"
    if reminder.get("next_due_local"):
        detail = (detail + " · " if detail else "") + f"next {reminder['next_due_local'][:16].replace('T', ' ')}"

    return toast(reminder.get("title") or "Reminder", headline, detail.strip())


def deliver_due(now: datetime | None = None) -> list[dict]:
    """Claim everything due and announce it. Safe to run from two processes."""
    due = store().claim_due(now)
    for reminder in due:
        shown = announce(reminder)
        log.info(
            "fired %s %r (late=%s, notified=%s)",
            reminder["id"], reminder["title"], reminder.get("late"), shown,
        )
    return due


# ------------------------------------------------------------------ watcher


def start_watcher(interval: int = SWEEP_SECONDS) -> threading.Thread:
    """Sweep for due reminders while the app is open.

    The scheduled task covers the app being shut; this covers the app being
    open without one installed, and makes a reminder created moments ago fire
    on time rather than at the top of the next minute.
    """

    def loop() -> None:
        while True:
            try:
                deliver_due()
            except Exception as exc:  # a bad row must not end the watcher
                log.warning("sweep failed: %s: %s", type(exc).__name__, exc)
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="reminder-watcher", daemon=True)
    thread.start()
    log.info("reminder watcher started (every %ss)", interval)
    return thread


# ------------------------------------------------------------- the schedule


def sweep_command() -> tuple[str, str]:
    """(executable, arguments) that runs one sweep, for the scheduled task."""
    if is_frozen():
        return sys.executable, "--notify"
    # pythonw, so the minute-by-minute run never flashes a console. It sits
    # beside python.exe in every venv and in a normal install.
    interpreter = Path(sys.executable)
    windowless = interpreter.with_name("pythonw.exe")
    runner = windowless if windowless.exists() else interpreter
    return str(runner), f'"{ROOT / "launcher.py"}" --notify'


# Defined as XML and registered with schtasks.exe rather than through the
# ScheduledTasks PowerShell module: Register-ScheduledTask goes via the CIM
# provider, which refuses with "Access is denied" for a standard user on a
# default Windows Home box. schtasks talks to the older COM API as the calling
# user and needs no elevation — and the XML form is the only way to get the
# settings that matter here.
_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{description}</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
    <TimeTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT1M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""

DESCRIPTION = (
    "Fires Lumen OS reminders every minute, so they arrive whether or not the "
    "app is open. Safe to disable: reminders then only appear while it is running."
)


def _current_user() -> str:
    """DOMAIN\\user for the account the task should run as."""
    domain = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME") or ""
    name = os.environ.get("USERNAME") or ""
    return f"{domain}\\{name}" if domain and name else name


def _schtasks(*arguments: str, timeout: int = 60):
    return subprocess.run(  # noqa: S603
        [_system32("schtasks.exe"), *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=_NO_WINDOW,
        check=False,
    )


def install_task() -> dict:
    """Register the scheduled task. Per-user, so no admin rights are needed."""
    if sys.platform != "win32":
        return {"installed": False, "error": "scheduled notifications are Windows-only"}

    executable, arguments = sweep_command()
    xml = _TASK_XML.format(
        description=xml_escape(DESCRIPTION),
        # Both the principal and the logon trigger are pinned to this account.
        # A logon trigger with no user means *any* user, which is a machine-wide
        # change and is refused without elevation.
        user=xml_escape(_current_user()),
        # StartBoundary in the past plus StartWhenAvailable: Windows runs the
        # first sweep immediately rather than at some future anniversary.
        # Local wall clock: Task Scheduler reads StartBoundary in local time.
        start=datetime.now().replace(microsecond=0).isoformat(),  # noqa: DTZ005
        command=xml_escape(executable),
        arguments=xml_escape(arguments),
        workdir=xml_escape(str(ROOT)),
    )

    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - see _powershell
        "w", suffix=".xml", delete=False, encoding="utf-16"
    )
    try:
        handle.write(xml)
        handle.close()
        result = _schtasks("/Create", "/TN", TASK_NAME, "/XML", handle.name, "/F")
    except Exception as exc:
        return {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        Path(handle.name).unlink(missing_ok=True)

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:400]
        log.warning("installing the scheduled task failed: %s", detail)
        return {"installed": False, "error": detail or "schtasks refused to register the task"}

    log.info("scheduled task installed: %s %s", executable, arguments)
    return {"installed": True, "command": f"{executable} {arguments}", **task_status()}


def remove_task() -> dict:
    if sys.platform != "win32":
        return {"installed": False, "supported": False}
    try:
        result = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    except Exception as exc:
        return {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
    log.info("scheduled task removed (exit %s)", result.returncode)
    return {"installed": False, "removed": result.returncode == 0}


def _field(text: str, label: str) -> str:
    for line in text.splitlines():
        name, _, value = line.partition(":")
        if name.strip().lower() == label:
            return value.strip()
    return ""


def task_status() -> dict:
    """Is the sweep scheduled, and when did it last run?"""
    if sys.platform != "win32":
        return {"installed": False, "supported": False, "detail": "Windows only"}
    try:
        result = _schtasks("/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V", timeout=45)
    except Exception as exc:
        return {"installed": False, "supported": True, "error": f"{type(exc).__name__}: {exc}"}

    if result.returncode != 0:
        return {"installed": False, "supported": True}
    output = result.stdout or ""
    return {
        "installed": True,
        "supported": True,
        "state": _field(output, "status"),
        "last_run": _field(output, "last run time"),
        "next_run": _field(output, "next run time"),
        "last_result": _field(output, "last result"),
    }
