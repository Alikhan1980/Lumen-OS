"""Gmail tools: search, read, send, draft, label, trash."""

from __future__ import annotations

import base64
import html
import mimetypes
import re
from collections.abc import Iterator
from email.message import EmailMessage
from typing import Any

from ..registry import obj, tool
from .google_auth import service
from .localfiles import resolve_in_workspace

GROUP = "gmail"
MAX_BODY_CHARS = 20_000

# Gmail refuses a message over 25 MB, and base64 inflates whatever we attach by
# about a third. Cap the raw bytes below that so the limit is hit here, with a
# useful message, rather than as an opaque API error after a long upload.
MAX_ATTACHMENT_BYTES = 18 * 1024 * 1024

# mimetypes reads the Windows registry, where .csv is commonly claimed by Excel
# and .md by nothing at all. Pin the text formats the agent actually produces so
# an attachment arrives labelled the same way on every machine.
for _extension, _type in (
    (".csv", "text/csv"),
    (".md", "text/markdown"),
    (".json", "application/json"),
    (".yaml", "application/yaml"),
    (".yml", "application/yaml"),
    (".log", "text/plain"),
):
    mimetypes.add_type(_type, _extension)


def _svc():
    return service("gmail", "v1")


def _header(headers: list[dict], name: str) -> str:
    target = name.lower()
    for h in headers:
        if h.get("name", "").lower() == target:
            return h.get("value", "")
    return ""


def _walk(payload: dict) -> Iterator[dict]:
    queue = [payload]
    while queue:
        part = queue.pop(0)
        yield part
        queue.extend(part.get("parts") or [])


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
_BLANKS_RE = re.compile(r"\n{3,}")


def _html_to_text(raw: str) -> str:
    raw = _SCRIPT_RE.sub(" ", raw)
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    raw = re.sub(r"</p\s*>", "\n\n", raw, flags=re.IGNORECASE)
    text = html.unescape(_TAG_RE.sub("", raw))
    return _BLANKS_RE.sub("\n\n", text).strip()


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8", "replace")


def _extract_body(payload: dict) -> str:
    plain, rich = "", ""
    for part in _walk(payload):
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        if mime == "text/plain" and not plain:
            plain = _decode(data)
        elif mime == "text/html" and not rich:
            rich = _decode(data)
    body = plain or (_html_to_text(rich) if rich else "")
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + f"\n\n[truncated at {MAX_BODY_CHARS} characters]"
    return body


def _attachments(payload: dict) -> list[dict]:
    out = []
    for part in _walk(payload):
        filename = part.get("filename")
        if filename:
            out.append(
                {
                    "filename": filename,
                    "mime_type": part.get("mimeType"),
                    "size_bytes": (part.get("body") or {}).get("size"),
                }
            )
    return out


def _summarize(message: dict) -> dict:
    headers = (message.get("payload") or {}).get("headers", [])
    return {
        "message_id": message.get("id"),
        "thread_id": message.get("threadId"),
        "from": _header(headers, "From"),
        "to": _header(headers, "To"),
        "subject": _header(headers, "Subject"),
        "date": _header(headers, "Date"),
        "labels": message.get("labelIds", []),
        "snippet": html.unescape(message.get("snippet", "")),
    }


# Each row costs its own metadata fetch — Gmail's list call returns bare ids —
# so this is the point where a longer list starts being felt as lag.
UI_INBOX_LIMIT = 25


def ui_inbox(query: str | None = None, limit: int = UI_INBOX_LIMIT) -> dict:
    """The message list the browser's inbox view shows.

    Kept beside the other Gmail code so header parsing and label conventions
    live in one place. Read-only: nothing here marks mail as read.
    """
    from email.utils import parseaddr

    q = (query or "in:inbox").strip()
    limit = max(1, min(int(limit), 50))
    svc = _svc()

    listing = (
        svc.users()
        .messages()
        .list(userId="me", q=q, maxResults=limit)
        .execute()
    )

    messages = []
    for entry in listing.get("messages", []):
        detail = (
            svc.users()
            .messages()
            .get(
                userId="me",
                id=entry["id"],
                format="metadata",
                metadataHeaders=["From", "To", "Subject", "Date"],
            )
            .execute()
        )
        headers = (detail.get("payload") or {}).get("headers", [])
        name, address = parseaddr(_header(headers, "From"))
        labels = detail.get("labelIds", [])
        messages.append(
            {
                "message_id": detail.get("id"),
                "thread_id": detail.get("threadId"),
                "from_name": name or address or "(unknown sender)",
                "from_email": address,
                "subject": _header(headers, "Subject") or "(no subject)",
                "snippet": html.unescape(detail.get("snippet", "")),
                # Epoch milliseconds, unlike the Date header, which is a
                # free-form string the sender chooses and cannot be sorted on.
                "timestamp": int(detail.get("internalDate") or 0),
                "unread": "UNREAD" in labels,
                "starred": "STARRED" in labels,
                "labels": labels,
            }
        )

    return {
        "query": q,
        "count": len(messages),
        "messages": messages,
    }


def ui_message(message_id: str) -> dict:
    """One message, with its body, for the reading pane."""
    from email.utils import parseaddr

    detail = gmail_read_message(message_id)
    name, address = parseaddr(detail.get("from", ""))
    detail["from_name"] = name or address
    detail["from_email"] = address
    detail["unread"] = "UNREAD" in (detail.get("labels") or [])
    return detail


@tool(
    group=GROUP,
    name="gmail_search",
    description=(
        "Search the user's Gmail with standard Gmail query syntax and return "
        "message summaries (sender, subject, date, snippet, labels). Use this "
        "before reading anything: it is how you find message_ids. Examples of "
        "queries: 'is:unread', 'from:alice@example.com newer_than:7d', "
        "'has:attachment subject:invoice', 'in:sent to:bob@example.com', "
        "'label:important older_than:1m'. Returns summaries only — call "
        "gmail_read_message for full body text."
    ),
    schema=obj(
        {
            "query": {
                "type": "string",
                "description": "Gmail search query, e.g. 'is:unread from:boss@corp.com'. Use an empty string for the most recent mail in the inbox.",
            },
            "max_results": {
                "type": "integer",
                "description": "How many messages to return (1-50). Default 10.",
            },
            "include_spam_trash": {
                "type": "boolean",
                "description": "Include spam and trash in the search. Default false.",
            },
        },
        required=["query"],
    ),
)
def gmail_search(
    query: str, max_results: int = 10, include_spam_trash: bool = False
) -> dict:
    max_results = max(1, min(int(max_results), 50))
    svc = _svc()
    listing = (
        svc.users()
        .messages()
        .list(
            userId="me",
            q=query or None,
            maxResults=max_results,
            includeSpamTrash=include_spam_trash,
        )
        .execute()
    )
    ids = [m["id"] for m in listing.get("messages", [])]
    messages = []
    for message_id in ids:
        detail = (
            svc.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "To", "Subject", "Date"],
            )
            .execute()
        )
        messages.append(_summarize(detail))
    return {"query": query, "count": len(messages), "messages": messages}


@tool(
    group=GROUP,
    name="gmail_read_message",
    description=(
        "Read one Gmail message in full: headers, plain-text body, and the list "
        "of attachments. Get message_id from gmail_search first."
    ),
    schema=obj(
        {
            "message_id": {"type": "string", "description": "The message id."},
        },
        required=["message_id"],
    ),
)
def gmail_read_message(message_id: str) -> dict:
    message = (
        _svc().users().messages().get(userId="me", id=message_id, format="full").execute()
    )
    payload = message.get("payload") or {}
    headers = payload.get("headers", [])
    result = _summarize(message)
    result.update(
        {
            "cc": _header(headers, "Cc"),
            "reply_to": _header(headers, "Reply-To"),
            "body": _extract_body(payload),
            "attachments": _attachments(payload),
        }
    )
    return result


@tool(
    group=GROUP,
    name="gmail_read_thread",
    description=(
        "Read every message in a Gmail conversation thread, oldest first. Use "
        "this instead of gmail_read_message when the user asks about a "
        "conversation, or when you need the history before drafting a reply."
    ),
    schema=obj(
        {
            "thread_id": {"type": "string", "description": "The thread id."},
        },
        required=["thread_id"],
    ),
)
def gmail_read_thread(thread_id: str) -> dict:
    thread = _svc().users().threads().get(userId="me", id=thread_id, format="full").execute()
    messages = []
    for message in thread.get("messages", []):
        payload = message.get("payload") or {}
        item = _summarize(message)
        item["body"] = _extract_body(payload)
        messages.append(item)
    return {"thread_id": thread_id, "count": len(messages), "messages": messages}


def _attach(message: EmailMessage, paths: list[str]) -> list[dict]:
    """Attach workspace files, and report what went on.

    Only the workspace is reachable — the same guard the local file tools use,
    so nothing outside it can be mailed out even if the model asks.
    """
    attached: list[dict] = []
    total = 0
    for raw in paths:
        source = resolve_in_workspace(raw)
        if not source.is_file():
            raise FileNotFoundError(
                f"no such file in the workspace: {raw}. Use file_list to see what "
                "is there; browser and Drive downloads land in 'downloads/'."
            )
        data = source.read_bytes()
        total += len(data)
        if total > MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"attachments come to more than {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB, "
                "which Gmail will not send. Share it from Drive instead: upload with "
                "drive_create_file, then drive_share_file, and put the link in the body."
            )
        guessed, _ = mimetypes.guess_type(source.name)
        maintype, _, subtype = (guessed or "application/octet-stream").partition("/")
        message.add_attachment(
            data, maintype=maintype, subtype=subtype or "octet-stream", filename=source.name
        )
        attached.append(
            {"filename": source.name, "path": raw, "size_bytes": len(data), "mime_type": guessed}
        )
    return attached


def _build_message(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
    reply_headers: dict | None = None,
    attachments: list[str] | None = None,
) -> tuple[EmailMessage, list[dict]]:
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    if cc:
        message["Cc"] = cc
    if bcc:
        message["Bcc"] = bcc
    for key, value in (reply_headers or {}).items():
        if value:
            message[key] = value
    message.set_content(body)
    # After set_content: adding an attachment is what turns this into a
    # multipart/mixed message, and the body has to be the first part.
    return message, _attach(message, attachments or [])


def _encode(message: EmailMessage) -> str:
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def _reply_context(message_id: str | None) -> tuple[dict, str | None]:
    """Return (headers, thread_id) so a reply lands in the right conversation."""
    if not message_id:
        return {}, None
    original = (
        _svc()
        .users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["Message-ID", "References", "Subject"],
        )
        .execute()
    )
    headers = (original.get("payload") or {}).get("headers", [])
    rfc_id = _header(headers, "Message-ID")
    references = " ".join(x for x in [_header(headers, "References"), rfc_id] if x)
    return (
        {"In-Reply-To": rfc_id, "References": references},
        original.get("threadId"),
    )


@tool(
    group=GROUP,
    name="gmail_send_email",
    description=(
        "Send an email from the user's Gmail account, optionally with files "
        "attached. This leaves the machine and cannot be unsent — only call it "
        "when the user has asked you to send. If they asked you to write or "
        "prepare something, use gmail_create_draft instead. Set "
        "reply_to_message_id to make it a threaded reply. Attachments come from "
        "the workspace folder, which is where browser_download and "
        "drive_download_file put files — attach 'downloads/report.pdf' to send "
        "something you just downloaded. Gmail caps a message at 25 MB; for "
        "anything larger, put it in Drive and send the link instead."
    ),
    schema=obj(
        {
            "to": {"type": "string", "description": "Recipient address(es), comma-separated."},
            "subject": {"type": "string", "description": "Subject line."},
            "body": {"type": "string", "description": "Plain-text body of the email."},
            "cc": {"type": "string", "description": "Cc address(es), comma-separated."},
            "bcc": {"type": "string", "description": "Bcc address(es), comma-separated."},
            "reply_to_message_id": {
                "type": "string",
                "description": "Message id being replied to; keeps the reply in that thread.",
            },
            "attachments": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Files to attach, as workspace-relative paths, e.g. "
                    "['downloads/report.pdf']. Files outside the workspace cannot be sent."
                ),
            },
        },
        required=["to", "subject", "body"],
    ),
    confirm=True,
)
def gmail_send_email(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
    reply_to_message_id: str | None = None,
    attachments: list[str] | None = None,
) -> dict:
    reply_headers, thread_id = _reply_context(reply_to_message_id)
    message, attached = _build_message(
        to, subject, body, cc, bcc, reply_headers, attachments
    )
    payload: dict[str, Any] = {"raw": _encode(message)}
    if thread_id:
        payload["threadId"] = thread_id
    sent = _svc().users().messages().send(userId="me", body=payload).execute()
    return {
        "status": "sent",
        "message_id": sent.get("id"),
        "thread_id": sent.get("threadId"),
        "to": to,
        "subject": subject,
        "attachments": attached,
    }


@tool(
    group=GROUP,
    name="gmail_create_draft",
    description=(
        "Save an email as a draft in Gmail without sending it, optionally with "
        "files attached. Prefer this over gmail_send_email whenever the user "
        "asked you to write, prepare, or draft something rather than to send it. "
        "Attachments come from the workspace folder, the same as for sending."
    ),
    schema=obj(
        {
            "to": {"type": "string", "description": "Recipient address(es), comma-separated."},
            "subject": {"type": "string", "description": "Subject line."},
            "body": {"type": "string", "description": "Plain-text body of the email."},
            "cc": {"type": "string", "description": "Cc address(es), comma-separated."},
            "bcc": {"type": "string", "description": "Bcc address(es), comma-separated."},
            "reply_to_message_id": {
                "type": "string",
                "description": "Message id being replied to; keeps the draft in that thread.",
            },
            "attachments": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Files to attach, as workspace-relative paths, e.g. "
                    "['downloads/report.pdf']. Files outside the workspace cannot be attached."
                ),
            },
        },
        required=["to", "subject", "body"],
    ),
)
def gmail_create_draft(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
    reply_to_message_id: str | None = None,
    attachments: list[str] | None = None,
) -> dict:
    reply_headers, thread_id = _reply_context(reply_to_message_id)
    message, attached = _build_message(
        to, subject, body, cc, bcc, reply_headers, attachments
    )
    draft_message: dict[str, Any] = {"raw": _encode(message)}
    if thread_id:
        draft_message["threadId"] = thread_id
    draft = (
        _svc()
        .users()
        .drafts()
        .create(userId="me", body={"message": draft_message})
        .execute()
    )
    return {
        "status": "draft_saved",
        "draft_id": draft.get("id"),
        "message_id": (draft.get("message") or {}).get("id"),
        "to": to,
        "subject": subject,
        "attachments": attached,
    }


@tool(
    group=GROUP,
    name="gmail_modify_labels",
    description=(
        "Add or remove Gmail labels on one or more messages. This is how you "
        "archive (remove INBOX), mark read (remove UNREAD), mark unread (add "
        "UNREAD), star (add STARRED), or apply a custom label. Use "
        "gmail_list_labels to find custom label ids."
    ),
    schema=obj(
        {
            "message_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Message ids to modify.",
            },
            "add_labels": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Label ids to add, e.g. ['STARRED'].",
            },
            "remove_labels": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Label ids to remove, e.g. ['UNREAD'] or ['INBOX'] to archive.",
            },
        },
        required=["message_ids"],
    ),
)
def gmail_modify_labels(
    message_ids: list[str],
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
) -> dict:
    body = {
        "addLabelIds": add_labels or [],
        "removeLabelIds": remove_labels or [],
    }
    svc = _svc()
    for message_id in message_ids:
        svc.users().messages().modify(userId="me", id=message_id, body=body).execute()
    return {
        "status": "updated",
        "modified": len(message_ids),
        "added": add_labels or [],
        "removed": remove_labels or [],
    }


@tool(
    group=GROUP,
    name="gmail_list_labels",
    description="List all Gmail labels on the account with their ids, including custom ones.",
    schema=obj({}),
)
def gmail_list_labels() -> dict:
    labels = _svc().users().labels().list(userId="me").execute().get("labels", [])
    return {
        "labels": [
            {"id": x["id"], "name": x["name"], "type": x.get("type")} for x in labels
        ]
    }


@tool(
    group=GROUP,
    name="gmail_trash_message",
    description=(
        "Move a Gmail message to the trash. Recoverable from Gmail's trash for "
        "30 days. To archive instead, remove the INBOX label with "
        "gmail_modify_labels — that is usually what a user means by 'clear' or "
        "'get it out of my inbox'."
    ),
    schema=obj(
        {"message_id": {"type": "string", "description": "The message id to trash."}},
        required=["message_id"],
    ),
    confirm=True,
)
def gmail_trash_message(message_id: str) -> dict:
    _svc().users().messages().trash(userId="me", id=message_id).execute()
    return {"status": "trashed", "message_id": message_id}


@tool(
    group=GROUP,
    name="gmail_get_profile",
    description="Get the signed-in Gmail account's email address and message counts.",
    schema=obj({}),
)
def gmail_get_profile() -> dict:
    profile = _svc().users().getProfile(userId="me").execute()
    return {
        "email_address": profile.get("emailAddress"),
        "messages_total": profile.get("messagesTotal"),
        "threads_total": profile.get("threadsTotal"),
    }
