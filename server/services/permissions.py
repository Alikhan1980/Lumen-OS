"""What the agent may do on a user's behalf, expressed as capabilities.

A scope string is not a permission model. `https://www.googleapis.com/auth/drive`
tells a user nothing, and a consent screen listing nine of them tells them less.
So the unit here is a *capability* -- "Read your email", "Create calendar
events" -- each of which maps to the scopes it actually needs. The user picks
capabilities; the scopes follow.

Two things fall out of that, both of them requirements rather than niceties:

* **Least privilege becomes possible.** A user who only wants calendar help
  grants calendar scopes, and the agent physically cannot read their mail --
  not because a check says no, but because the token does not carry the scope.
* **Revocation is comprehensible.** The Connected Accounts screen renders this
  table, so "what can this thing see?" has an answer in the user's own terms.

The scope sets match what the existing tool modules in agent/tools/ actually
call, so enabling a capability enables exactly the tools that work with it and
no others. Where a narrower scope would break an existing tool, that is called
out in `note` rather than quietly requested -- see the Drive entry.
"""

from __future__ import annotations

from dataclasses import dataclass

GOOGLE = "google"


@dataclass(frozen=True)
class Capability:
    key: str
    label: str
    description: str
    scopes: tuple[str, ...]
    # Tools in agent/tools/ that stop working without this capability.
    tools: tuple[str, ...]
    # Whether this one lets the agent affect the outside world. Drives the
    # extra warning on the consent screen and pairs with the approval gate in
    # agent/core.py.
    outbound: bool = False
    note: str = ""


# Identity. Always requested: it is how we know which Google account was
# connected, and it is the narrowest pair Google offers.
IDENTITY_SCOPES = ("openid", "https://www.googleapis.com/auth/userinfo.email")


GOOGLE_CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        key="gmail.read",
        label="Read and organise your email",
        description=(
            "Search your mailbox, read messages and threads, apply labels, and "
            "move things to the bin. It cannot send anything."
        ),
        scopes=("https://www.googleapis.com/auth/gmail.modify",),
        tools=(
            "gmail_search",
            "gmail_read_message",
            "gmail_read_thread",
            "gmail_list_labels",
            "gmail_get_profile",
            "gmail_modify_labels",
            "gmail_trash_message",
        ),
        note=(
            "Reading and organising are one permission because Google makes them "
            "one scope: the read-only scope cannot mark a message read or apply a "
            "label, which is most of what 'tidy my inbox' means. `gmail.modify` "
            "is the narrowest scope that covers both, and it still cannot send "
            "mail or permanently delete anything -- binned messages are "
            "recoverable from Trash."
        ),
    ),
    Capability(
        key="gmail.send",
        label="Send email as you",
        description="Compose, draft and send messages from your address.",
        scopes=("https://www.googleapis.com/auth/gmail.send",),
        tools=("gmail_send_email", "gmail_create_draft"),
        outbound=True,
    ),
    Capability(
        key="calendar.read",
        label="Read your calendar",
        description="See your events, your calendars, and when you are free.",
        scopes=("https://www.googleapis.com/auth/calendar.readonly",),
        tools=("calendar_list_events", "calendar_search_events", "calendar_list_calendars",
               "calendar_find_free_time"),
    ),
    Capability(
        key="calendar.write",
        label="Manage your calendar",
        description="Create, change and delete events, and invite people to them.",
        scopes=("https://www.googleapis.com/auth/calendar",),
        tools=("calendar_create_event", "calendar_update_event", "calendar_delete_event"),
        outbound=True,
    ),
    Capability(
        key="drive.files",
        label="Work with files it creates",
        description=(
            "Create documents and spreadsheets, and read the ones it made. "
            "It cannot see the rest of your Drive."
        ),
        scopes=("https://www.googleapis.com/auth/drive.file",),
        tools=("drive_create_file",),
    ),
    Capability(
        key="drive.all",
        label="Read and manage your whole Drive",
        description="Search every file in your Drive, read them, share them, and bin them.",
        scopes=("https://www.googleapis.com/auth/drive",),
        tools=("drive_search_files", "drive_read_file", "drive_download_file",
               "drive_share_file", "drive_trash_file"),
        outbound=True,
        note=(
            "This is the broad one. Google has no scope that allows searching "
            "across a Drive without also allowing reading and modifying what it "
            "finds, so 'find me that contract' needs full Drive access. Grant "
            "'Work with files it creates' instead if you only want it writing "
            "new documents."
        ),
    ),
    Capability(
        key="contacts.read",
        label="Look up your contacts",
        description="Resolve a name to an address so it can mail the right person.",
        scopes=(
            "https://www.googleapis.com/auth/contacts.readonly",
            "https://www.googleapis.com/auth/contacts.other.readonly",
        ),
        tools=("contacts_search", "contacts_list"),
    ),
    Capability(
        key="tasks.write",
        label="Manage your Google Tasks",
        description="See your task lists and add, complete or remove tasks.",
        scopes=("https://www.googleapis.com/auth/tasks",),
        tools=("tasks_list", "tasks_list_tasklists", "tasks_create", "tasks_complete",
               "tasks_delete"),
    ),
)

# What a first connection asks for when the user does not choose. Reading, and
# nothing that leaves the account: no send, no calendar writes, no full Drive.
# Everything else is one click away on the Connected Accounts screen, which is
# the right shape -- a user who never asks the agent to send mail should never
# have granted it the ability to.
DEFAULT_CAPABILITIES = ("gmail.read", "calendar.read", "contacts.read", "tasks.write")

_BY_KEY = {capability.key: capability for capability in GOOGLE_CAPABILITIES}


def capability(key: str) -> Capability | None:
    return _BY_KEY.get(key)


def scopes_for(keys: list[str] | tuple[str, ...]) -> list[str]:
    """The scope list to request for a set of capabilities, identity included.

    Unknown keys are dropped rather than raising: a client on an older build
    asking for a capability that has since been renamed should get a working
    connection with the rest, not a hard failure.
    """
    wanted: list[str] = list(IDENTITY_SCOPES)
    for key in keys:
        found = _BY_KEY.get(key)
        if found is None:
            continue
        for scope in found.scopes:
            if scope not in wanted:
                wanted.append(scope)
    return wanted


def capabilities_from_scopes(granted: list[str] | tuple[str, ...]) -> list[str]:
    """Which capabilities a granted scope list actually satisfies.

    Google does not always grant what was asked for -- a user can untick things
    on the consent screen, and an older grant may predate a capability. So the
    UI is rendered from what came back, never from what was requested.
    """
    have = set(granted)
    return [
        capability.key
        for capability in GOOGLE_CAPABILITIES
        if all(scope in have for scope in capability.scopes)
    ]


def tools_for_scopes(granted: list[str] | tuple[str, ...]) -> set[str]:
    """The tool names the agent may run with this grant.

    This is the enforcement point for "the agent can only use what the user
    allowed". The registry is filtered against this set before the tool list is
    ever shown to the model, so an ungranted tool is not something the model
    declines to call -- it is something the model never knew existed.
    """
    have = set(granted)
    allowed: set[str] = set()
    for entry in GOOGLE_CAPABILITIES:
        if all(scope in have for scope in entry.scopes):
            allowed.update(entry.tools)
    return allowed


def describe(granted: list[str] | tuple[str, ...]) -> list[dict]:
    """The permission screen's data: every capability, and whether it is held."""
    have = set(granted)
    return [
        {
            "key": entry.key,
            "label": entry.label,
            "description": entry.description,
            "granted": all(scope in have for scope in entry.scopes),
            "outbound": entry.outbound,
            "note": entry.note,
            "tools": list(entry.tools),
        }
        for entry in GOOGLE_CAPABILITIES
    ]
