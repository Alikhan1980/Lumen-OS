"""Google Drive tools: search, read, create, share, download."""

from __future__ import annotations

import io
from pathlib import Path

from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from ..config import load_config
from ..registry import obj, tool
from .google_auth import service

GROUP = "drive"
MAX_TEXT_CHARS = 40_000

FILE_FIELDS = "id, name, mimeType, modifiedTime, size, owners(displayName,emailAddress), webViewLink, parents"

# Google-native formats have no bytes to download; they must be exported.
EXPORT_AS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.script": "application/vnd.google-apps.script+json",
}

READABLE_PREFIXES = ("text/", "application/json", "application/xml")


def _svc():
    return service("drive", "v3")


def _clean(entry: dict) -> dict:
    owners = entry.get("owners") or []
    return {
        "file_id": entry.get("id"),
        "name": entry.get("name"),
        "mime_type": entry.get("mimeType"),
        "modified": entry.get("modifiedTime"),
        "size_bytes": entry.get("size"),
        "owner": owners[0].get("emailAddress") if owners else None,
        "link": entry.get("webViewLink"),
    }


@tool(
    group=GROUP,
    name="drive_search_files",
    description=(
        "Search the user's Google Drive. Pass plain words to search file names "
        "and contents, or a raw Drive query string for precision — e.g. "
        "\"mimeType='application/vnd.google-apps.spreadsheet'\", "
        "\"modifiedTime > '2026-01-01T00:00:00'\", \"'me' in owners\", "
        "\"name contains 'budget'\". Returns file ids needed by the other Drive tools."
    ),
    schema=obj(
        {
            "query": {
                "type": "string",
                "description": "Plain search words, or a raw Drive API query if it contains an operator like contains/=/>.",
            },
            "max_results": {
                "type": "integer",
                "description": "How many files to return (1-100). Default 20.",
            },
            "order_by": {
                "type": "string",
                "description": "Sort order, e.g. 'modifiedTime desc' (default) or 'name'.",
            },
        },
        required=["query"],
    ),
)
def drive_search_files(
    query: str, max_results: int = 20, order_by: str = "modifiedTime desc"
) -> dict:
    max_results = max(1, min(int(max_results), 100))
    raw = query.strip()
    # Heuristic: treat it as a Drive query only if it looks like one.
    looks_structured = any(
        token in raw for token in ("contains", "mimeType", " in ", "=", ">", "<")
    )
    q = raw if looks_structured else f"fullText contains '{raw.replace(chr(39), '')}'"
    if "trashed" not in q:
        q = f"({q}) and trashed = false"

    result = (
        _svc()
        .files()
        .list(
            q=q,
            pageSize=max_results,
            orderBy=order_by,
            fields=f"files({FILE_FIELDS})",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    files = [_clean(f) for f in result.get("files", [])]
    return {"query": q, "count": len(files), "files": files}


@tool(
    group=GROUP,
    name="drive_read_file",
    description=(
        "Read the contents of a Drive file as text. Google Docs export as plain "
        "text, Sheets as CSV, Slides as plain text; text/JSON/XML files are read "
        "directly. Binary formats (PDF, images, zip) cannot be read as text — "
        "use drive_download_file for those."
    ),
    schema=obj(
        {"file_id": {"type": "string", "description": "The Drive file id."}},
        required=["file_id"],
    ),
)
def drive_read_file(file_id: str) -> dict:
    svc = _svc()
    meta = svc.files().get(fileId=file_id, fields=FILE_FIELDS, supportsAllDrives=True).execute()
    mime = meta.get("mimeType", "")
    info = _clean(meta)

    if mime in EXPORT_AS:
        data = svc.files().export(fileId=file_id, mimeType=EXPORT_AS[mime]).execute()
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
    elif mime.startswith(READABLE_PREFIXES):
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(
            buffer, svc.files().get_media(fileId=file_id, supportsAllDrives=True)
        )
        done = False
        while not done:
            _, done = downloader.next_chunk()
        text = buffer.getvalue().decode("utf-8", "replace")
    else:
        return {
            **info,
            "content": None,
            "note": f"{mime} is not text. Use drive_download_file to save it locally.",
        }

    truncated = len(text) > MAX_TEXT_CHARS
    if truncated:
        text = text[:MAX_TEXT_CHARS] + f"\n\n[truncated at {MAX_TEXT_CHARS} characters]"
    return {**info, "content": text, "truncated": truncated}


@tool(
    group=GROUP,
    name="drive_create_file",
    description=(
        "Create a new file in Google Drive from text content. Set mime_type to "
        "'application/vnd.google-apps.document' to create a Google Doc, "
        "'application/vnd.google-apps.spreadsheet' with CSV content to create a "
        "Sheet, or 'text/plain'/'text/markdown' for a plain file."
    ),
    schema=obj(
        {
            "name": {"type": "string", "description": "File name."},
            "content": {"type": "string", "description": "Text content of the file."},
            "mime_type": {
                "type": "string",
                "description": "Target Drive mime type. Default 'application/vnd.google-apps.document'.",
            },
            "folder_id": {
                "type": "string",
                "description": "Parent folder id. Omit for My Drive root.",
            },
        },
        required=["name", "content"],
    ),
)
def drive_create_file(
    name: str,
    content: str,
    mime_type: str = "application/vnd.google-apps.document",
    folder_id: str | None = None,
) -> dict:
    metadata: dict = {"name": name, "mimeType": mime_type}
    if folder_id:
        metadata["parents"] = [folder_id]

    # Upload as text/csv and let Drive convert into the Google-native format.
    source_mime = "text/csv" if "spreadsheet" in mime_type else "text/plain"
    media = MediaIoBaseUpload(
        io.BytesIO(content.encode("utf-8")), mimetype=source_mime, resumable=False
    )
    created = (
        _svc()
        .files()
        .create(body=metadata, media_body=media, fields=FILE_FIELDS, supportsAllDrives=True)
        .execute()
    )
    return {"status": "created", **_clean(created)}


@tool(
    group=GROUP,
    name="drive_share_file",
    description=(
        "Grant another person access to a Drive file. This exposes the file to "
        "someone outside this machine, so confirm the address and role are what "
        "the user intended."
    ),
    schema=obj(
        {
            "file_id": {"type": "string", "description": "The Drive file id."},
            "email": {"type": "string", "description": "Email address to share with."},
            "role": {
                "type": "string",
                "enum": ["reader", "commenter", "writer"],
                "description": "Access level to grant. Default 'reader'.",
            },
            "message": {
                "type": "string",
                "description": "Optional note included in the notification email.",
            },
        },
        required=["file_id", "email"],
    ),
    confirm=True,
)
def drive_share_file(
    file_id: str, email: str, role: str = "reader", message: str | None = None
) -> dict:
    _svc().permissions().create(
        fileId=file_id,
        body={"type": "user", "role": role, "emailAddress": email},
        sendNotificationEmail=True,
        emailMessage=message,
        supportsAllDrives=True,
    ).execute()
    return {"status": "shared", "file_id": file_id, "email": email, "role": role}


@tool(
    group=GROUP,
    name="drive_download_file",
    description=(
        "Download a Drive file to the local workspace folder. Works for binary "
        "files (PDF, images, zip) as well as text. Google-native files are "
        "exported to PDF unless export_mime_type says otherwise."
    ),
    schema=obj(
        {
            "file_id": {"type": "string", "description": "The Drive file id."},
            "filename": {
                "type": "string",
                "description": "Name to save as inside the workspace folder. Defaults to the Drive name.",
            },
            "export_mime_type": {
                "type": "string",
                "description": "For Google Docs/Sheets/Slides, the export format, e.g. 'application/pdf'.",
            },
        },
        required=["file_id"],
    ),
)
def drive_download_file(
    file_id: str, filename: str | None = None, export_mime_type: str | None = None
) -> dict:
    svc = _svc()
    meta = svc.files().get(fileId=file_id, fields=FILE_FIELDS, supportsAllDrives=True).execute()
    mime = meta.get("mimeType", "")
    name = filename or meta.get("name") or file_id

    if mime.startswith("application/vnd.google-apps."):
        export_mime = export_mime_type or "application/pdf"
        request = svc.files().export_media(fileId=file_id, mimeType=export_mime)
        if export_mime == "application/pdf" and not Path(name).suffix:
            name += ".pdf"
    else:
        request = svc.files().get_media(fileId=file_id, supportsAllDrives=True)

    workspace = load_config().workspace
    destination = (workspace / Path(name).name).resolve()
    if workspace not in destination.parents and destination != workspace:
        raise ValueError("refusing to write outside the workspace folder")

    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    destination.write_bytes(buffer.getvalue())

    return {
        "status": "downloaded",
        "path": str(destination),
        "size_bytes": destination.stat().st_size,
        "name": meta.get("name"),
    }


@tool(
    group=GROUP,
    name="drive_trash_file",
    description=(
        "Move a Drive file to the trash. Recoverable from Drive's trash for 30 days."
    ),
    schema=obj(
        {"file_id": {"type": "string", "description": "The Drive file id to trash."}},
        required=["file_id"],
    ),
    confirm=True,
)
def drive_trash_file(file_id: str) -> dict:
    _svc().files().update(
        fileId=file_id, body={"trashed": True}, supportsAllDrives=True
    ).execute()
    return {"status": "trashed", "file_id": file_id}
