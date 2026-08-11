"""Check that the Google sign-in actually granted everything the tools need.

Read-only: one cheap call per API, nothing is created, modified, or sent.

    .venv\\Scripts\\python.exe scripts\\verify_google.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import TOKEN_PATH
from agent.tools.google_auth import SCOPES, refresh_account_email, service

FRIENDLY = {
    "gmail.modify": "read, label, archive, draft mail",
    "gmail.send": "send mail",
    "calendar": "read and write calendar events",
    "drive": "read, create, share Drive files",
    "contacts.readonly": "look up saved contacts",
    "contacts.other.readonly": "look up people you have emailed",
    "tasks": "read and write Google Tasks",
}

failures: list[str] = []


def check_scopes() -> None:
    print("Granted permissions")
    if not TOKEN_PATH.exists():
        print("  no token found — run:  .\\run.ps1 --auth")
        failures.append("not signed in")
        return

    granted = set(json.loads(TOKEN_PATH.read_text(encoding="utf-8")).get("scopes", []))
    for scope in SCOPES:
        short = scope.rsplit("/auth/", 1)[-1]
        label = FRIENDLY.get(short, short)
        if scope in granted:
            print(f"  OK    {short:<26} {label}")
        else:
            print(f"  MISS  {short:<26} {label}")
            failures.append(f"scope not granted: {short}")

    extra = granted - set(SCOPES)
    for scope in sorted(extra):
        if "userinfo" in scope or scope == "openid":
            continue
        print(f"  note  extra scope granted: {scope}")


def probe(label: str, fn) -> None:
    try:
        detail = fn()
    except Exception as exc:
        message = str(exc)
        if "SERVICE_DISABLED" in message or "has not been used in project" in message:
            hint = "API not enabled in Google Cloud — see SETUP.md step 3"
        elif "insufficient" in message.lower():
            hint = "permission not granted — run .\\run.ps1 --auth again"
        else:
            hint = f"{type(exc).__name__}: {message[:140]}"
        print(f"  FAIL  {label:<10} {hint}")
        failures.append(f"{label}: {hint}")
        return
    print(f"  OK    {label:<10} {detail}")


def check_apis() -> None:
    print("\nLive API calls (read-only)")

    def gmail():
        p = service("gmail", "v1").users().getProfile(userId="me").execute()
        return f"{p['emailAddress']} · {p.get('messagesTotal', 0):,} messages"

    def drive():
        r = service("drive", "v3").files().list(pageSize=1, fields="files(name)").execute()
        files = r.get("files", [])
        return f"reachable · most recent: {files[0]['name'][:40]}" if files else "reachable · no files"

    def calendar():
        r = service("calendar", "v3").calendarList().list(maxResults=5).execute()
        return f"{len(r.get('items', []))} calendar(s)"

    def contacts():
        r = (
            service("people", "v1")
            .people()
            .connections()
            .list(resourceName="people/me", pageSize=1, personFields="names")
            .execute()
        )
        return f"{r.get('totalPeople', 0)} saved contact(s)"

    def tasks():
        r = service("tasks", "v1").tasklists().list(maxResults=5).execute()
        return f"{len(r.get('items', []))} task list(s)"

    probe("Gmail", gmail)
    probe("Drive", drive)
    probe("Calendar", calendar)
    probe("Contacts", contacts)
    probe("Tasks", tasks)


def main() -> int:
    print("Verifying the Google connection\n")
    check_scopes()
    check_apis()

    email = refresh_account_email()
    if email:
        print(f"\nActing as {email}")

    if failures:
        print(f"\n{len(failures)} problem(s):")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\nEverything works. Start the agent with:  .\\run.ps1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
