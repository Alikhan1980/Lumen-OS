"""Runtime configuration and on-disk layout.

Two locations matter:

* **Bundled** (read-only, ships inside the .exe) — the OAuth client that
  identifies this app to Google.
* **Per-user data** (`%APPDATA%\\WorkspaceAgent` once packaged) — the .env, the
  Google token, the provider choices, and the workspace folder. Keeping these
  out of the app folder is what makes each Windows user a separate agent
  identity, and what lets the app live somewhere read-only like Program Files.

In a dev checkout both collapse to the project root, so nothing changes while
working on the code.

AI provider credentials are **not** here. They live in the operating system's
credential store and are reached only through `agent/providers/` — see
`agent/providers/keystore.py`. Nothing in this module reads, writes or holds an
AI API key, and this app ships with none of its own.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "WorkspaceAgent"


def is_frozen() -> bool:
    """True when running from a PyInstaller build rather than a checkout."""
    return bool(getattr(sys, "frozen", False))


def _data_dir() -> Path:
    if is_frozen():
        base = Path(os.environ.get("APPDATA") or Path.home())
        return base / APP_NAME
    return ROOT


DATA_DIR = _data_dir()
ENV_PATH = DATA_DIR / ".env"
CREDENTIALS_DIR = DATA_DIR / "credentials"
TOKEN_PATH = CREDENTIALS_DIR / "token.json"
ACCOUNT_PATH = CREDENTIALS_DIR / "account.json"
DEFAULT_WORKSPACE = DATA_DIR / "workspace"


def bundled_dir() -> Path:
    """Where PyInstaller unpacks bundled data files at runtime."""
    return Path(getattr(sys, "_MEIPASS", ROOT))


def _find_client_secret(directory: Path) -> Path | None:
    exact = directory / "client_secret.json"
    if exact.exists():
        return exact
    # Google's download keeps a long default name like
    # client_secret_1234-abcd.apps.googleusercontent.com.json. Accept it rather
    # than making an exact rename load-bearing.
    try:
        matches = sorted(directory.glob("client_secret*.json"))
    except OSError:
        return None
    return matches[0] if matches else None


def client_secret_path() -> Path | None:
    """The OAuth client to use, or None if the app has none.

    A file dropped into the user's own credentials folder wins, so a shipped
    build can be pointed at someone else's Google Cloud project.
    """
    return _find_client_secret(CREDENTIALS_DIR) or _find_client_secret(bundled_dir())


def supabase_config() -> tuple[str, str] | None:
    """(project URL, anon key) for the accounts backend, or None if unset.

    Bundled into a build the same way the OAuth client is, because accounts
    have to work on a machine that has no .env -- which is every machine the
    app is downloaded onto. Environment variables win so a checkout can point
    at a different project without rebuilding.

    The anon key belongs in the build. It is the public half of a Supabase
    project, designed to ship inside clients, and row-level security is what
    actually restricts it. The service-role key is the dangerous one and is
    never read here.
    """
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (os.getenv("SUPABASE_ANON_KEY") or "").strip()
    if url and key:
        return url.rstrip("/"), key

    for directory in (CREDENTIALS_DIR, bundled_dir()):
        path = directory / "supabase.json"
        if not path.exists():
            continue
        try:
            # utf-8-sig, not utf-8: PowerShell 5.1's `Out-File -Encoding utf8`
            # writes a BOM, and build.ps1 generates this file. Plain utf-8
            # raises on the BOM, which read as "no accounts configured" and
            # shipped a build with the gate silently off.
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        url = str(data.get("url") or "").strip().rstrip("/")
        key = str(data.get("anon_key") or "").strip()
        if url and key:
            return url, key
    return None


# Web search backends, best first. Whichever one has its key set is used;
# DuckDuckGo needs none and is the fallback, so search works out of the box.
SEARCH_PROVIDERS = ("brave", "tavily", "google", "duckduckgo")


@dataclass
class Config:
    """Everything except which AI provider to use.

    Model and provider selection live in `agent/providers/settings.py`, because
    they are per-provider choices the user makes in the app rather than
    environment configuration.
    """

    max_tokens: int
    effort: str | None
    auto_approve: bool
    show_thinking: bool
    workspace: Path

    # --- web search ---------------------------------------------------------
    # All optional. With no key at all, search falls back to DuckDuckGo.
    search_provider: str | None = None
    brave_api_key: str | None = None
    tavily_api_key: str | None = None
    google_search_api_key: str | None = None
    google_search_engine_id: str | None = None

    # --- browser automation -------------------------------------------------
    # Visible by default: this drives a real browser through the user's own
    # logins, and they should be able to see what it is doing and take over.
    browser_headless: bool = False
    # Ask before every click and keystroke, not only the ones that look
    # consequential. Off by default; on, it makes the browser fully supervised.
    browser_confirm_all: bool = False
    # Empty means anywhere. Otherwise the browser may only open these hosts
    # (subdomains included), which is how you pin it to one intranet.
    browser_allowed_domains: tuple[str, ...] = ()


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _text(name: str) -> str | None:
    return (os.getenv(name) or "").strip() or None


def _domains(name: str) -> tuple[str, ...]:
    raw = os.getenv(name) or ""
    return tuple(
        part.strip().lower().removeprefix("www.")
        for part in raw.replace(";", ",").split(",")
        if part.strip()
    )


def load_config() -> Config:
    load_dotenv(ENV_PATH, override=False)

    effort = (os.getenv("AGENT_EFFORT") or "").strip().lower() or None
    if effort and effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError(
            f"AGENT_EFFORT must be one of low|medium|high|xhigh|max (got {effort!r})"
        )

    workspace = Path(os.getenv("AGENT_WORKSPACE") or DEFAULT_WORKSPACE).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)

    provider = (os.getenv("AGENT_SEARCH_PROVIDER") or "").strip().lower() or None
    if provider and provider not in SEARCH_PROVIDERS:
        raise ValueError(
            f"AGENT_SEARCH_PROVIDER must be one of {'|'.join(SEARCH_PROVIDERS)} "
            f"(got {provider!r})"
        )

    return Config(
        max_tokens=int(os.getenv("AGENT_MAX_TOKENS", "32000")),
        effort=effort,
        auto_approve=_bool("AGENT_AUTO_APPROVE", False),
        show_thinking=_bool("AGENT_SHOW_THINKING", False),
        workspace=workspace.resolve(),
        search_provider=provider,
        brave_api_key=_text("BRAVE_SEARCH_API_KEY"),
        tavily_api_key=_text("TAVILY_API_KEY"),
        google_search_api_key=_text("GOOGLE_SEARCH_API_KEY"),
        google_search_engine_id=_text("GOOGLE_SEARCH_ENGINE_ID"),
        browser_headless=_bool("AGENT_BROWSER_HEADLESS", False),
        browser_confirm_all=_bool("AGENT_BROWSER_CONFIRM_ALL", False),
        browser_allowed_domains=_domains("AGENT_BROWSER_ALLOWED_DOMAINS"),
    )


# There is deliberately no helper for writing a secret into the .env. The only
# thing this app stores on a user's behalf is an AI provider key, and those go
# to the OS credential store — see agent/providers/keystore.py. Leaving no such
# helper here means no future caller can reach for one by accident.
