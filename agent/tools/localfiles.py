"""Local file tools, confined to the agent's workspace folder.

Every path the model supplies is resolved and checked against the workspace
root before any I/O happens, so `..`, absolute paths, and symlinks cannot
escape it.

`resolve_in_workspace` is that check, and it is deliberately the only one: the
Gmail attachment path and the browser's file upload both call it rather than
writing the comparison out again, so there is one place to get it right.
"""

from __future__ import annotations

from pathlib import Path

from ..config import load_config
from ..registry import obj, tool

GROUP = "files"
MAX_READ_CHARS = 40_000


def resolve_in_workspace(relative: str) -> Path:
    """Absolute path for a workspace-relative one, or raise if it escapes."""
    workspace = load_config().workspace
    candidate = (workspace / relative).resolve()
    if candidate != workspace and workspace not in candidate.parents:
        raise ValueError(
            f"path escapes the workspace folder ({workspace}); "
            "only relative paths inside it are allowed"
        )
    return candidate


@tool(
    group=GROUP,
    name="file_list",
    description=(
        "List files in the agent's local workspace folder. This is the only "
        "place on disk the agent can read or write, and it is where "
        "drive_download_file saves files."
    ),
    schema=obj(
        {
            "path": {
                "type": "string",
                "description": "Subfolder relative to the workspace root. Default '.' for the root.",
            }
        }
    ),
)
def file_list(path: str = ".") -> dict:
    target = resolve_in_workspace(path)
    if not target.exists():
        return {"path": str(target), "exists": False, "entries": []}
    entries = []
    for item in sorted(target.iterdir()):
        entries.append(
            {
                "name": item.name,
                "type": "directory" if item.is_dir() else "file",
                "size_bytes": item.stat().st_size if item.is_file() else None,
            }
        )
    return {"path": str(target), "exists": True, "count": len(entries), "entries": entries}


@tool(
    group=GROUP,
    name="file_read",
    description=(
        "Read a text file from the agent's local workspace folder. Use "
        "file_list first if you are not sure of the exact filename. This reads "
        "local disk only — for Google Drive contents use drive_read_file."
    ),
    schema=obj(
        {
            "path": {
                "type": "string",
                "description": "File path relative to the workspace root, e.g. 'notes.md'.",
            }
        },
        required=["path"],
    ),
)
def file_read(path: str) -> dict:
    target = resolve_in_workspace(path)
    if not target.is_file():
        raise FileNotFoundError(f"no such file in the workspace: {path}")
    text = target.read_text(encoding="utf-8", errors="replace")
    truncated = len(text) > MAX_READ_CHARS
    if truncated:
        text = text[:MAX_READ_CHARS] + f"\n\n[truncated at {MAX_READ_CHARS} characters]"
    return {"path": str(target), "content": text, "truncated": truncated}


@tool(
    group=GROUP,
    name="file_write",
    description=(
        "Write a text file into the agent's local workspace folder, creating "
        "parent folders as needed. Overwrites an existing file unless append is true."
    ),
    schema=obj(
        {
            "path": {
                "type": "string",
                "description": "File path relative to the workspace root, e.g. 'reports/summary.md'.",
            },
            "content": {"type": "string", "description": "Text to write."},
            "append": {
                "type": "boolean",
                "description": "Append instead of overwriting. Default false.",
            },
        },
        required=["path", "content"],
    ),
)
def file_write(path: str, content: str, append: bool = False) -> dict:
    target = resolve_in_workspace(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if append and target.exists():
        with target.open("a", encoding="utf-8") as handle:
            handle.write(content)
    else:
        target.write_text(content, encoding="utf-8")
    return {
        "status": "appended" if append else "written",
        "path": str(target),
        "size_bytes": target.stat().st_size,
    }
