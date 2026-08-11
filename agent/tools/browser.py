"""Browser automation: a real Chromium the agent can drive.

`web_fetch` reads a page as an anonymous visitor. This drives one instead —
clicking, typing, choosing, scrolling, downloading, uploading — for everything
that needs a session, a script to have run, or a form to be filled.

Three things shape the design:

**One browser, one thread.** Playwright's sync objects belong to the thread that
made them, and the web UI runs every turn on a fresh thread. So all Playwright
work is funnelled through a single worker thread owned by this module, and the
browser survives from one message to the next: "open the report page" and, a
message later, "now download it".

**A real profile.** Chromium runs against a persistent profile under the app's
data folder, so a site the user logs into by hand stays logged in — including
through their own MFA. Nothing here types a password.

**Refs, not selectors.** `browser_read` numbers every interactive element and
stamps the number onto it, and the other tools take that number. After a
navigation the numbers are gone, which is exactly what should happen: the tool
says the page changed and the agent re-reads it rather than clicking blind.

Visible by default, so the user can watch it work and take the wheel. Clicks
that look consequential — paying, deleting, submitting — stop and ask first.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from .. import approvals
from ..config import DATA_DIR, load_config
from ..logs import logger
from ..registry import obj, tool

GROUP = "browser"
log = logger("browser")

# Chromium's own profile: cookies, logins, extensions state. Beside the Google
# token, which is the credential this sits next to in sensitivity.
PROFILE_DIR = DATA_DIR / "browser-profile"
# Everything the browser saves lands here, inside the workspace, so file_read
# and drive_create_file can pick it up without a second path convention.
DOWNLOAD_DIR = "downloads"

ACTION_TIMEOUT_MS = 20_000
NAVIGATION_TIMEOUT_MS = 45_000
# The outer wait: whatever Playwright is doing plus room to fail on its own
# terms, so a timeout here really means the worker thread is wedged.
CALL_TIMEOUT_S = 90
DOWNLOAD_WAIT_S = 60

MAX_ELEMENTS = 80
DEFAULT_TEXT_CHARS = 6_000
MAX_TEXT_CHARS = 40_000

INSTALL_HELP = (
    "The browser tools need Playwright and its Chromium build. Install them with:\n"
    "    pip install -r requirements.txt\n"
    "    python -m playwright install chromium"
)


class BrowserUnavailable(RuntimeError):
    """Playwright or its Chromium is not installed."""


class PageChanged(RuntimeError):
    """A ref pointed at an element that is no longer on the page."""


# --------------------------------------------------------------------- naming


def safe_name(raw: str) -> str:
    """A filename that cannot escape the folder it is meant for."""
    name = PurePosixPath((raw or "").replace("\\", "/")).name.strip()
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name)
    name = name.lstrip(".") or "download"
    return name[:120]


def unique_path(folder: Path, filename: str) -> Path:
    """A path in `folder` that does not clobber anything already there."""
    target = folder / safe_name(filename)
    stem, suffix = target.stem, target.suffix
    counter = 2
    while target.exists():
        target = folder / f"{stem} ({counter}){suffix}"
        counter += 1
    return target


def _downloads_folder() -> Path:
    folder = load_config().workspace / DOWNLOAD_DIR
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# ----------------------------------------------------------------- URL policy

# "http:", "javascript:", "data:" — the scheme as written, before any guessing.
# The lookahead keeps "localhost:8080" a host and a port rather than a scheme.
_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):(?!\d)")


def check_url(raw: str) -> str:
    """Validate a URL to open, and apply the domain allowlist if there is one."""
    candidate = (raw or "").strip()
    if not candidate:
        raise ValueError("no URL given")
    # Reject a bad scheme before adding one: "javascript:alert(1)" has no "//"
    # in it, and prefixing https:// would launder it into something that parses.
    scheme = _SCHEME.match(candidate)
    if scheme and scheme.group(1).lower() not in {"http", "https"}:
        raise ValueError(
            f"only http and https pages can be opened, not {scheme.group(1)!r}: — "
            "file:, javascript: and data: URLs are refused"
        )
    if "://" not in candidate:
        candidate = "https://" + candidate
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"only http and https pages can be opened, not {parsed.scheme!r}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not host:
        raise ValueError("that URL has no host")

    allowed = load_config().browser_allowed_domains
    if allowed and not any(host == d or host.endswith("." + d) for d in allowed):
        raise ValueError(
            f"{host} is not in AGENT_BROWSER_ALLOWED_DOMAINS "
            f"({', '.join(allowed)}), so the browser may not open it"
        )
    return candidate


# ----------------------------------------------------- consequential actions

# Clicking is usually harmless — a link, a tab, a "show more". These are the
# words that mean it is not. Matching one does not block the action: it turns
# the click into a question for the user.
_SENSITIVE = (
    (
        (
            r"\b(buy|purchase|checkout|check ?out|place (the |your )?order|complete (the )?"
            r"(order|purchase)|pay|pay now|payment|add (a )?card)\b"
        ),
        "this looks like a purchase or payment",
    ),
    (
        r"\b(subscribe|start (my |a )?(free )?trial|upgrade (my )?plan|renew)\b",
        "this looks like it starts a paid subscription",
    ),
    (
        r"\b(delete|erase|destroy|wipe|permanently remove|deactivate|close (my )?account)\b",
        "this looks like it deletes something",
    ),
    (
        (
            r"\b(submit|apply now|send|post|publish|book now|reserve|confirm|"
            r"i agree|accept (the )?terms|sign (the )?(contract|agreement|document)|e-?sign)\b"
        ),
        "this submits something on the user's behalf",
    ),
    (
        r"\b(transfer|withdraw|donate|refund|invoice)\b",
        "this looks like it moves money",
    ),
)

# Cookie and consent banners hit "accept" and "confirm" constantly and mean
# nothing. Asking about every one of them would train the user to click through.
_BENIGN = re.compile(
    r"cookie|consent|privacy preference|dismiss|reject all|not now", re.IGNORECASE
)

_RISKY_URL = re.compile(
    r"checkout|payment|billing|/cart|/pay\b|order/confirm", re.IGNORECASE
)


def sensitivity(description: str, url: str) -> str | None:
    """Why this action needs asking about, or None if it is routine."""
    text = " ".join((description or "").split())
    if _BENIGN.search(text):
        return None
    for pattern, reason in _SENSITIVE:
        if re.search(pattern, text, re.IGNORECASE):
            return reason
    if _RISKY_URL.search(url or ""):
        return "this page looks like a checkout or billing flow"
    return None


def _approved(action: str, details: dict, reason: str | None) -> bool:
    """Ask the user, unless nothing about this warrants it."""
    if not (reason or load_config().browser_confirm_all):
        return True
    if not approvals.available():
        # No front-end to ask (a script, a test). Refuse rather than guess.
        log.warning("no approver available for %s; refusing", action)
        return False
    return approvals.ask(action, {**details, "why": reason or "AGENT_BROWSER_CONFIRM_ALL is on"})


# -------------------------------------------------------------- page scripts

# Numbers every interactive element and stamps the number onto it, so the other
# tools can find it again by ref alone.
_SNAPSHOT_JS = """
({max, base}) => {
  const SEL = 'a[href],button,input,select,textarea,summary,[role="button"],' +
              '[role="link"],[role="checkbox"],[role="radio"],[role="tab"],' +
              '[role="menuitem"],[role="switch"],[contenteditable="true"]';
  const labelOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria;
    if (el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) return l.innerText;
    }
    const wrap = el.closest('label');
    return wrap ? wrap.innerText : '';
  };
  const rows = [];
  let n = 0;
  for (const el of document.querySelectorAll(SEL)) {
    if (n >= max) break;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'hidden') continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    const box = el.getBoundingClientRect();
    if (box.width === 0 && box.height === 0) continue;
    n += 1;
    el.setAttribute('data-lumen-ref', String(base + n));
    const row = {
      ref: base + n,
      tag: el.tagName.toLowerCase(),
      name: (labelOf(el) || el.innerText || el.placeholder || el.getAttribute('title') ||
             el.getAttribute('alt') || el.name || el.value || '')
             .replace(/\\s+/g, ' ').trim().slice(0, 120)
    };
    if (type) row.type = type;
    const role = el.getAttribute('role');
    if (role) row.role = role;
    if (el.tagName === 'A' && el.href) row.href = String(el.href).slice(0, 300);
    if (el.tagName === 'SELECT') {
      row.options = Array.from(el.options).slice(0, 30).map(o => (o.label || o.value));
    }
    if (type === 'checkbox' || type === 'radio') row.checked = !!el.checked;
    else if ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && el.value) {
      row.value = String(el.value).slice(0, 80);
    }
    if (el.disabled) row.disabled = true;
    if (box.bottom < 0 || box.top > innerHeight) row.offscreen = true;
    rows.push(row);
  }
  return rows;
}
"""

_PAGE_JS = """
() => ({
  title: document.title || '',
  url: location.href,
  text: document.body ? document.body.innerText : '',
  scroll_y: Math.round(window.scrollY),
  page_height: Math.round(document.body ? document.body.scrollHeight : 0),
  viewport_height: Math.round(window.innerHeight)
})
"""

_DESCRIBE_JS = """
el => ({
  tag: el.tagName.toLowerCase(),
  type: el.getAttribute('type') || '',
  name: (el.getAttribute('aria-label') || el.innerText || el.value ||
         el.getAttribute('title') || el.getAttribute('name') || '')
         .replace(/\\s+/g, ' ').trim().slice(0, 160),
  href: el.tagName === 'A' ? String(el.href || '').slice(0, 300) : '',
  form_text: el.form ? String(el.form.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 400) : ''
})
"""

_BLANKS = re.compile(r"\n\s*\n\s*\n+")


# ------------------------------------------------------------------- session


class _Session:
    """The browser, and the one thread allowed to touch it."""

    def __init__(self) -> None:
        self._pool: ThreadPoolExecutor | None = None
        self._gate = threading.Lock()  # one action at a time
        self._playwright: Any = None
        self._context: Any = None
        self._pages: list[Any] = []
        self._pending: list[Any] = []  # downloads Chromium has started
        self.downloads: list[dict] = []  # ones already written to disk
        # Element refs never repeat within a session. Numbering each snapshot
        # from 1 would let a ref quoted from an earlier message land on a
        # different element of the same page, and click the wrong thing
        # silently; this way an out-of-date ref simply is not found.
        self.ref_base = 0

    # -- plumbing ----------------------------------------------------------

    def call(self, work, timeout: float = CALL_TIMEOUT_S) -> Any:
        """Run `work(self)` on the browser thread and wait for its result."""
        if not self._gate.acquire(timeout=5):
            raise RuntimeError(
                "the browser is still busy with the previous action — wait for it "
                "to finish, or call browser_close to start over"
            )
        try:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser")
            future = self._pool.submit(self._invoke, work)
            try:
                return future.result(timeout=timeout)
            except FutureTimeout as exc:
                raise TimeoutError(
                    f"the browser did not respond within {timeout:.0f}s. The page may be "
                    "waiting on something; browser_close will reset it."
                ) from exc
        finally:
            self._gate.release()

    def _invoke(self, work) -> Any:
        self._start()
        result = work(self)
        self._collect_downloads()
        return result

    @property
    def running(self) -> bool:
        return self._context is not None

    def _start(self) -> None:
        if self._context is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserUnavailable(f"Playwright is not installed.\n\n{INSTALL_HELP}") from exc

        config = load_config()
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        playwright = sync_playwright().start()
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=config.browser_headless,
                accept_downloads=True,
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as exc:
            playwright.stop()
            raise BrowserUnavailable(
                f"Chromium would not start: {type(exc).__name__}: {exc}\n\n{INSTALL_HELP}"
            ) from exc

        context.set_default_timeout(ACTION_TIMEOUT_MS)
        context.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
        context.on("page", self._track)
        for page in context.pages:
            self._track(page)

        self._playwright, self._context = playwright, context
        log.info("browser started (headless=%s, profile=%s)", config.browser_headless, PROFILE_DIR)

    def _track(self, page: Any) -> None:
        """Follow a page the site opened as well as ones we opened ourselves."""
        if page in self._pages:
            return

        # Only bookkeeping in here — Playwright event handlers are not the place
        # to make blocking calls, so the download is saved later, from _invoke.
        # Both handlers must be plain functions: Playwright tags the callable it
        # is given, and a bound built-in like list.append has nowhere to take it.
        def started(download: Any) -> None:
            self._pending.append(download)

        def closed(page_closed: Any) -> None:
            if page_closed in self._pages:
                self._pages.remove(page_closed)

        page.on("download", started)
        page.on("close", closed)
        self._pages.append(page)

    def page(self) -> Any:
        """The page in front: the newest one still open."""
        self._pages = [p for p in self._pages if not p.is_closed()]
        if not self._pages:
            self._track(self._context.new_page())
        return self._pages[-1]

    def _collect_downloads(self) -> None:
        """Write anything Chromium downloaded during the last action."""
        while self._pending:
            download = self._pending.pop(0)
            try:
                target = unique_path(_downloads_folder(), download.suggested_filename)
                download.save_as(str(target))
            except Exception as exc:
                log.warning("download failed: %s: %s", type(exc).__name__, exc)
                self.downloads.append(
                    {"status": "failed", "url": getattr(download, "url", ""), "error": str(exc)}
                )
                continue
            record = {
                "status": "downloaded",
                "name": target.name,
                "path": f"{DOWNLOAD_DIR}/{target.name}",
                "size_bytes": target.stat().st_size,
                "url": download.url,
            }
            log.info("downloaded %s -> %s (%d bytes)", download.url, target, record["size_bytes"])
            self.downloads.append(record)

    def shutdown(self) -> dict:
        pages = len(self._pages)
        if self._context is not None:
            try:
                self._context.close()
            except Exception as exc:  # already gone, or the browser was killed
                log.warning("closing the context failed: %s", exc)
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception as exc:
                log.warning("stopping playwright failed: %s", exc)
        self._playwright = self._context = None
        self._pages, self._pending = [], []
        log.info("browser closed")
        return {"status": "closed", "pages_closed": pages}


_session = _Session()


def _close_at_exit() -> None:
    if _session.running:
        # Must happen on the browser thread — Playwright objects are bound to
        # it. On the way out there is nothing useful to do about a failure;
        # Chromium exits with the process regardless.
        with contextlib.suppress(Exception):
            _session.call(lambda s: s.shutdown(), timeout=20)


atexit.register(_close_at_exit)


def reset_session() -> None:
    """Drop the browser without going through a tool. Used by the tests."""
    if _session.running:
        _close_at_exit()


def _browser_cache() -> Path:
    """Where `playwright install` puts its browsers on this platform."""
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def status() -> dict:
    """Whether the browser looks usable, for --check.

    Deliberately a filesystem check rather than a trial launch: starting the
    Playwright driver only to stop it again leaves asyncio complaining on the
    way out, and this runs on a screen the user reads. Anything it gets wrong
    surfaces properly at first use, where BrowserUnavailable says what to run.
    """
    try:
        import playwright  # noqa: F401
    except ImportError:
        return {
            "ready": False,
            "detail": "Playwright is not installed (pip install -r requirements.txt)",
        }

    cache = _browser_cache()
    installed = cache.is_dir() and any(cache.glob("chromium*"))
    if not installed:
        return {
            "ready": False,
            "detail": "Chromium is not downloaded — run: python -m playwright install chromium",
        }
    return {"ready": True, "detail": str(cache)}


# ------------------------------------------------------------ element lookup

_REF = re.compile(r"^(?:ref[:= ]?\s*)?#?(\d{1,4})$", re.IGNORECASE)
_LOOKS_CSS = re.compile(r"^(css=|#|\.|\[)|[>#.\[]")


def _resolve(page: Any, target: str) -> tuple[Any, str]:
    """Turn what the model called an element into a locator.

    Accepts, in order: a ref from browser_read, a CSS selector, an accessible
    name, a field label or placeholder, and finally visible text.
    """
    text = (target or "").strip()
    if not text:
        raise ValueError("no target given")

    ref = _REF.match(text)
    if ref:
        locator = page.locator(f"[data-lumen-ref='{ref.group(1)}']")
        if locator.count() == 0:
            raise PageChanged(
                f"ref {ref.group(1)} is not on this page any more — the page has "
                "changed. Call browser_read to get the current refs."
            )
        return locator.first, f"ref {ref.group(1)}"

    attempts: list[tuple[str, Any]] = []
    if _LOOKS_CSS.search(text):
        attempts.append(("css", page.locator(text.removeprefix("css="))))
    # Exact before partial, in both passes. Playwright matches accessible names
    # by substring unless told otherwise, and on a real page that is how "new"
    # ends up clicking "Hacker News" — the whole-name match has to win.
    for exact in (True, False):
        attempts += [
            (f"button{'' if exact else ' (partial)'}", page.get_by_role("button", name=text, exact=exact)),
            (f"link{'' if exact else ' (partial)'}", page.get_by_role("link", name=text, exact=exact)),
            (f"label{'' if exact else ' (partial)'}", page.get_by_label(text, exact=exact)),
            (f"placeholder{'' if exact else ' (partial)'}", page.get_by_placeholder(text, exact=exact)),
            (f"text{'' if exact else ' (partial)'}", page.get_by_text(text, exact=exact)),
        ]

    for how, locator in attempts:
        try:
            count = locator.count()
        except Exception as exc:
            # An invalid selector, or a locator this page cannot take. The next
            # strategy may well work, so this is a step in the search, not a fault.
            log.debug("locator %s failed for %r: %s", how, text, exc)
            continue
        if count:
            extra = f" ({count} matches, took the first)" if count > 1 else ""
            return locator.first, f"{how} {text!r}{extra}"

    raise PageChanged(
        f"nothing on this page matches {text!r}. Call browser_read to see what is "
        "there — the page may have changed, or the element may need scrolling to."
    )


def _describe(locator: Any) -> dict:
    try:
        return locator.evaluate(_DESCRIBE_JS)
    except Exception:
        return {"tag": "", "type": "", "name": "", "href": "", "form_text": ""}


# ----------------------------------------------------------------- snapshots


def _snapshot(session: _Session, text_chars: int, include_elements: bool = True) -> dict:
    page = session.page()
    info = page.evaluate(_PAGE_JS)
    text = _BLANKS.sub("\n\n", (info.get("text") or "").strip())
    truncated = len(text) > text_chars
    if truncated:
        text = text[:text_chars] + f"\n\n[truncated at {text_chars} characters — scroll or raise max_chars]"

    payload: dict = {
        "url": info.get("url"),
        "title": info.get("title"),
        "text": text,
        "truncated": truncated,
        "scroll": {
            "y": info.get("scroll_y"),
            "page_height": info.get("page_height"),
            "viewport_height": info.get("viewport_height"),
        },
        "open_tabs": len(session._pages),
    }
    if include_elements:
        elements = page.evaluate(
            _SNAPSHOT_JS, {"max": MAX_ELEMENTS, "base": session.ref_base}
        )
        session.ref_base += len(elements)
        payload["elements"] = elements
        payload["element_count"] = len(elements)
        payload["hint"] = (
            "Pass a ref number as `target` to click, type or select. Refs belong to "
            "this reading of the page only — after anything that reloads or redraws "
            "it, read again and use the new numbers."
        )
    if session.downloads:
        payload["downloads"] = session.downloads[-5:]
    return payload


def _after_action(session: _Session, note: str, text_chars: int = 1_500) -> dict:
    """What the page looks like now — every action answers with this."""
    page = session.page()
    # A page that never settles is still worth reporting on.
    with contextlib.suppress(Exception):
        page.wait_for_load_state("domcontentloaded", timeout=8_000)
    return {"status": "done", "action": note, **_snapshot(session, text_chars)}


# --------------------------------------------------------------------- tools


@tool(
    group=GROUP,
    name="browser_open",
    description=(
        "Open a URL in the agent's browser and return what the page says, plus a "
        "numbered list of the things on it you can click, type into or choose "
        "from. Use this instead of web_fetch when the page needs a login, runs "
        "scripts to render, or has to be interacted with. The browser stays open "
        "and keeps its cookies between messages, so a site the user has signed "
        "into by hand stays signed in. The browser window is visible — say so if "
        "you need the user to log in or clear a captcha themselves."
    ),
    schema=obj(
        {
            "url": {"type": "string", "description": "Full URL, e.g. 'https://example.com/reports'."},
            "wait_for": {
                "type": "string",
                "description": "Optional text to wait for before reading, for pages that load late.",
            },
            "max_chars": {
                "type": "integer",
                "description": f"Cap on page text returned (default {DEFAULT_TEXT_CHARS}, max {MAX_TEXT_CHARS}).",
            },
        },
        required=["url"],
    ),
)
def browser_open(
    url: str, wait_for: str | None = None, max_chars: int = DEFAULT_TEXT_CHARS
) -> dict:
    target = check_url(url)
    limit = max(200, min(int(max_chars), MAX_TEXT_CHARS))

    def work(session: _Session) -> dict:
        page = session.page()
        try:
            response = page.goto(target, wait_until="domcontentloaded")
        except Exception as exc:
            raise RuntimeError(
                f"could not open {target}: {type(exc).__name__}: {exc}"
            ) from exc
        if wait_for:
            try:
                page.get_by_text(wait_for).first.wait_for(timeout=ACTION_TIMEOUT_MS)
            except Exception:
                log.info("wait_for %r never appeared on %s", wait_for, target)
        status = response.status if response else None
        snapshot = _snapshot(session, limit)
        if status and status >= 400:
            snapshot["http_status"] = status
            snapshot["note"] = f"The site answered HTTP {status}; the page may be an error page."
        return snapshot

    log.info("open %s", target)
    return _session.call(work)


@tool(
    group=GROUP,
    name="browser_navigate",
    description=(
        "Move within the browser's history, or reload. Use 'back' after following "
        "a result that turned out to be wrong, rather than opening the previous "
        "URL again from scratch — going back keeps the session and any form state."
    ),
    schema=obj(
        {
            "action": {
                "type": "string",
                "enum": ["back", "forward", "reload"],
                "description": "Which way to move.",
            }
        },
        required=["action"],
    ),
)
def browser_navigate(action: str) -> dict:
    if action not in {"back", "forward", "reload"}:
        raise ValueError("action must be back, forward or reload")

    def work(session: _Session) -> dict:
        page = session.page()
        moved = {"back": page.go_back, "forward": page.go_forward, "reload": page.reload}[action]()
        if moved is None and action != "reload":
            return {
                "status": "no_change",
                "action": action,
                "detail": f"there is no page to go {action} to",
            }
        return _after_action(session, action)

    log.info("navigate %s", action)
    return _session.call(work)


@tool(
    group=GROUP,
    name="browser_read",
    description=(
        "Re-read the page the browser is on: its text, and a numbered list of every "
        "link, button, field and dropdown. Call this before your first click on a "
        "page, and again after anything that reloads or changes it — the ref numbers "
        "are only valid for the version of the page you read them from. It can also "
        "wait for text to appear, which is how you handle a page that loads its "
        "content after the initial render."
    ),
    schema=obj(
        {
            "wait_for": {
                "type": "string",
                "description": "Wait until this text appears before reading. Useful for slow or dynamic pages.",
            },
            "wait_seconds": {
                "type": "integer",
                "description": "How long to wait for it (1-60). Default 10.",
            },
            "max_chars": {
                "type": "integer",
                "description": f"Cap on page text returned (default {DEFAULT_TEXT_CHARS}, max {MAX_TEXT_CHARS}).",
            },
            "elements": {
                "type": "boolean",
                "description": "Include the numbered element list. Default true; set false when you only want to read.",
            },
        }
    ),
)
def browser_read(
    wait_for: str | None = None,
    wait_seconds: int = 10,
    max_chars: int = DEFAULT_TEXT_CHARS,
    elements: bool = True,
) -> dict:
    limit = max(200, min(int(max_chars), MAX_TEXT_CHARS))
    patience = max(1, min(int(wait_seconds), 60))

    def work(session: _Session) -> dict:
        page = session.page()
        found = None
        if wait_for:
            try:
                page.get_by_text(wait_for).first.wait_for(timeout=patience * 1000)
                found = True
            except Exception:
                found = False
        snapshot = _snapshot(session, limit, include_elements=elements)
        if wait_for:
            snapshot["waited_for"] = wait_for
            snapshot["appeared"] = found
            if not found:
                snapshot["note"] = (
                    f"{wait_for!r} never appeared within {patience}s. The page below is "
                    "what is actually there."
                )
        return snapshot

    return _session.call(work, timeout=CALL_TIMEOUT_S + patience)


@tool(
    group=GROUP,
    name="browser_click",
    description=(
        "Click something on the page. Target it by the ref number from "
        "browser_read (most reliable), or by its visible text, label, or a CSS "
        "selector. Returns the page as it is after the click, so you can see what "
        "happened. A click that looks consequential — buying, paying, deleting, "
        "submitting a form, agreeing to terms — stops and asks the user first; "
        "never describe such an action as done unless the tool says it was."
    ),
    schema=obj(
        {
            "target": {
                "type": "string",
                "description": "Ref number from browser_read, e.g. '12', or the element's visible text, or a CSS selector.",
            },
            "expect_navigation": {
                "type": "boolean",
                "description": "Set true when the click should load a new page, so the tool waits for it. Default true.",
            },
        },
        required=["target"],
    ),
)
def browser_click(target: str, expect_navigation: bool = True) -> dict:
    # Resolve and describe first, on the browser thread; decide about asking
    # here, where the user's front-end can be reached.
    def look(session: _Session) -> tuple[dict, str, str]:
        page = session.page()
        locator, how = _resolve(page, target)
        return _describe(locator), how, page.url

    description, how, url = _session.call(look)
    label = description.get("name") or description.get("href") or how
    reason = sensitivity(f"{label} {description.get('type', '')}", url)
    if not _approved(
        "browser_click", {"page": url, "element": label, "matched_by": how}, reason
    ):
        log.info("click declined by user: %s on %s", label, url)
        return {
            "status": "cancelled",
            "detail": (
                f"The user did not approve clicking {label!r}. Do not retry it — "
                "tell them what you were about to do and ask how to proceed."
            ),
            "element": label,
        }

    def work(session: _Session) -> dict:
        page = session.page()
        locator, _ = _resolve(page, target)
        with contextlib.suppress(Exception):  # not scrollable, or already there
            locator.scroll_into_view_if_needed(timeout=5_000)
        try:
            locator.click(timeout=ACTION_TIMEOUT_MS)
        except Exception as exc:
            raise RuntimeError(
                f"could not click {label!r}: {type(exc).__name__}: {exc}. It may be "
                "covered by an overlay or disabled — read the page again."
            ) from exc
        if expect_navigation:
            # A page that keeps polling never goes idle; that is not a failure.
            with contextlib.suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=8_000)
        # `matched_by` is here so an ambiguous target is visible rather than
        # silent: it says whether this was a ref, an exact name, or one of
        # several partial matches.
        return {**_after_action(session, f"clicked {label!r}"), "matched_by": how}

    log.info("click %s (%s) on %s", label, how, url)
    return _session.call(work)


@tool(
    group=GROUP,
    name="browser_type",
    description=(
        "Type into a text field, search box or editor. Target it by the ref number "
        "from browser_read, its label, or its placeholder text. By default the "
        "field is cleared first. Set submit to press Enter afterwards — do that "
        "for search boxes, but for a form with real consequences click its button "
        "instead so the confirmation checks apply. Never type a password the user "
        "has not given you: ask them to sign in themselves in the browser window."
    ),
    schema=obj(
        {
            "target": {
                "type": "string",
                "description": "Ref number from browser_read, or the field's label or placeholder.",
            },
            "text": {"type": "string", "description": "Text to enter."},
            "submit": {
                "type": "boolean",
                "description": "Press Enter after typing. Default false.",
            },
            "clear": {
                "type": "boolean",
                "description": "Clear the field first. Default true; set false to append.",
            },
        },
        required=["target", "text"],
    ),
)
def browser_type(target: str, text: str, submit: bool = False, clear: bool = True) -> dict:
    def look(session: _Session) -> tuple[dict, str, str]:
        page = session.page()
        locator, how = _resolve(page, target)
        return _describe(locator), how, page.url

    description, how, url = _session.call(look)
    field = description.get("name") or how

    # Typing is harmless; pressing Enter can submit. Judge the surrounding form
    # — _approved lets it through unimpeded when there is nothing to judge.
    reason = sensitivity(description.get("form_text", ""), url) if submit else None
    if not _approved(
        "browser_type",
        {"page": url, "field": field, "text": text, "submits": submit},
        reason,
    ):
        log.info("typing declined by user: %s on %s", field, url)
        return {
            "status": "cancelled",
            "detail": f"The user did not approve entering that into {field!r}. Do not retry it.",
            "field": field,
        }

    def work(session: _Session) -> dict:
        page = session.page()
        locator, _ = _resolve(page, target)
        try:
            if clear:
                locator.fill(text, timeout=ACTION_TIMEOUT_MS)
            else:
                locator.click(timeout=ACTION_TIMEOUT_MS)
                locator.type(text, delay=15)
        except Exception as exc:
            raise RuntimeError(
                f"could not type into {field!r}: {type(exc).__name__}: {exc}. Check it "
                "is a text field and not covered by something."
            ) from exc
        if submit:
            locator.press("Enter")
            with contextlib.suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=8_000)
        note = f"typed into {field!r}" + (" and pressed Enter" if submit else "")
        return _after_action(session, note)

    log.info("type into %s (%s) submit=%s", field, how, submit)
    return _session.call(work)


@tool(
    group=GROUP,
    name="browser_select",
    description=(
        "Choose an option in a dropdown (a <select>). browser_read lists the "
        "available options for each one. Match by the visible option text, or by "
        "its underlying value if you know it. For dropdowns that are really styled "
        "menus rather than a <select>, click the control and then click the option."
    ),
    schema=obj(
        {
            "target": {
                "type": "string",
                "description": "Ref number from browser_read, or the dropdown's label.",
            },
            "option": {"type": "string", "description": "The option's visible text, or its value."},
        },
        required=["target", "option"],
    ),
)
def browser_select(target: str, option: str) -> dict:
    def work(session: _Session) -> dict:
        page = session.page()
        locator, how = _resolve(page, target)
        try:
            chosen = locator.select_option(label=option, timeout=ACTION_TIMEOUT_MS)
        except Exception:
            try:
                chosen = locator.select_option(value=option, timeout=ACTION_TIMEOUT_MS)
            except Exception as exc:
                raise RuntimeError(
                    f"could not choose {option!r} in {how}: {type(exc).__name__}: {exc}. "
                    "Read the page again to see the exact options — this may not be a "
                    "real dropdown, in which case click it and then click the option."
                ) from exc
        return _after_action(session, f"selected {option!r} ({', '.join(chosen) or 'no value'})")

    log.info("select %r in %s", option, target)
    return _session.call(work)


@tool(
    group=GROUP,
    name="browser_scroll",
    description=(
        "Scroll the page. Use it when browser_read says elements are offscreen, or "
        "when the text was truncated and there is more below. 'bottom' is the quick "
        "way to reach a footer, a Load more button, or the end of a long list."
    ),
    schema=obj(
        {
            "direction": {
                "type": "string",
                "enum": ["down", "up", "top", "bottom"],
                "description": "Which way to scroll.",
            },
            "amount": {
                "type": "integer",
                "description": "Pixels to scroll for up/down. Default one screen.",
            },
        },
        required=["direction"],
    ),
)
def browser_scroll(direction: str, amount: int | None = None) -> dict:
    if direction not in {"down", "up", "top", "bottom"}:
        raise ValueError("direction must be down, up, top or bottom")

    def work(session: _Session) -> dict:
        page = session.page()
        step = int(amount) if amount else 0
        if direction == "top":
            page.evaluate("window.scrollTo(0, 0)")
        elif direction == "bottom":
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        else:
            delta = step or "window.innerHeight * 0.9"
            sign = "" if direction == "down" else "-"
            page.evaluate(f"window.scrollBy(0, {sign}({delta}))")
        page.wait_for_timeout(400)  # let lazy content load in
        return _after_action(session, f"scrolled {direction}")

    return _session.call(work)


@tool(
    group=GROUP,
    name="browser_download",
    description=(
        "Download a file from the page into the agent's workspace, under "
        "'downloads/'. Give it either the ref number or text of the link or button "
        "that starts the download, or a direct file URL. The saved path comes back "
        "in the result and can be handed straight to file_read, or uploaded with "
        "drive_create_file. Ordinary clicks that happen to trigger a download are "
        "saved too and reported by browser_read."
    ),
    schema=obj(
        {
            "target": {
                "type": "string",
                "description": "Ref number or visible text of the download link/button on the current page.",
            },
            "url": {
                "type": "string",
                "description": "A direct file URL to download instead of clicking anything.",
            },
            "filename": {
                "type": "string",
                "description": "Save as this name. Defaults to the name the site suggests.",
            },
        }
    ),
)
def browser_download(
    target: str | None = None, url: str | None = None, filename: str | None = None
) -> dict:
    if not target and not url:
        raise ValueError("give either target (something to click) or url (a direct link)")

    if url:
        address = check_url(url)

        def fetch(session: _Session) -> dict:
            # Through the browser's own context, so cookies from a logged-in
            # session are sent — that is the whole point of downloading here
            # rather than with web_fetch.
            response = session._context.request.get(address, timeout=NAVIGATION_TIMEOUT_MS)
            if not response.ok:
                raise RuntimeError(f"{address} returned HTTP {response.status}")
            name = filename or Path(urlparse(address).path).name or "download"
            destination = unique_path(_downloads_folder(), name)
            destination.write_bytes(response.body())
            record = {
                "status": "downloaded",
                "name": destination.name,
                "path": f"{DOWNLOAD_DIR}/{destination.name}",
                "size_bytes": destination.stat().st_size,
                "url": address,
            }
            session.downloads.append(record)
            log.info("downloaded %s -> %s", address, destination)
            return record

        return _session.call(fetch, timeout=CALL_TIMEOUT_S + 60)

    def work(session: _Session) -> dict:
        page = session.page()
        locator, how = _resolve(page, target or "")
        description = _describe(locator)
        before = len(session.downloads)
        try:
            locator.click(timeout=ACTION_TIMEOUT_MS)
        except Exception as exc:
            raise RuntimeError(f"could not click {how}: {type(exc).__name__}: {exc}") from exc

        deadline = time.monotonic() + DOWNLOAD_WAIT_S
        while time.monotonic() < deadline:
            session._collect_downloads()
            if len(session.downloads) > before:
                break
            page.wait_for_timeout(300)  # also pumps Playwright's event loop

        if len(session.downloads) == before:
            return {
                "status": "no_download",
                "detail": (
                    f"Clicking {description.get('name') or how!r} did not produce a download "
                    f"within {DOWNLOAD_WAIT_S}s. It may have opened the file in the page "
                    "instead — read the page and look for a direct link to pass as url."
                ),
                **_snapshot(session, 1_500, include_elements=True),
            }

        record = dict(session.downloads[-1])
        if filename and record.get("status") == "downloaded":
            current = _downloads_folder() / record["name"]
            renamed = unique_path(_downloads_folder(), filename)
            current.rename(renamed)
            record.update(
                name=renamed.name, path=f"{DOWNLOAD_DIR}/{renamed.name}"
            )
            session.downloads[-1] = record
        return record

    return _session.call(work, timeout=CALL_TIMEOUT_S + DOWNLOAD_WAIT_S)


@tool(
    group=GROUP,
    name="browser_upload",
    description=(
        "Attach a file from the agent's workspace to a file field on the page. The "
        "path is relative to the workspace, e.g. 'downloads/report.pdf' — files "
        "elsewhere on the machine cannot be uploaded. This sends the user's file to "
        "someone else's website, so it always asks first. Uploading does not submit "
        "the form; click the submit button separately once the file is attached."
    ),
    schema=obj(
        {
            "target": {
                "type": "string",
                "description": "Ref number of the file input, or the Choose file / Upload control's text.",
            },
            "path": {
                "type": "string",
                "description": "File to upload, relative to the workspace folder.",
            },
        },
        required=["target", "path"],
    ),
    confirm=True,
)
def browser_upload(target: str, path: str) -> dict:
    from .localfiles import resolve_in_workspace

    source = resolve_in_workspace(path)
    if not source.is_file():
        raise FileNotFoundError(f"no such file in the workspace: {path}")

    def work(session: _Session) -> dict:
        page = session.page()
        locator, how = _resolve(page, target)
        try:
            locator.set_input_files(str(source), timeout=ACTION_TIMEOUT_MS)
        except Exception:
            # Not a real <input type=file> — a styled button that opens the
            # chooser. Click it and answer the chooser instead.
            try:
                with page.expect_file_chooser(timeout=ACTION_TIMEOUT_MS) as chooser:
                    locator.click()
                chooser.value.set_files(str(source))
            except Exception as exc:
                raise RuntimeError(
                    f"could not attach the file to {how}: {type(exc).__name__}: {exc}. "
                    "Read the page and target the file input itself."
                ) from exc
        log.info("uploaded %s to %s", source.name, page.url)
        # The page state goes in first: its own "status" must not win over this
        # one, which is what the caller is actually asking about.
        return {
            **_after_action(session, f"attached {source.name}"),
            "status": "attached",
            "file": source.name,
            "size_bytes": source.stat().st_size,
            "note": "The file is attached but nothing has been submitted yet.",
        }

    return _session.call(work, timeout=CALL_TIMEOUT_S + 60)


@tool(
    group=GROUP,
    name="browser_screenshot",
    description=(
        "Save a picture of the current page into the workspace. Useful as proof of "
        "what a page looked like when you finished, or to show the user why "
        "something could not be completed. You cannot see the image yourself — "
        "report the path so the user can open it."
    ),
    schema=obj(
        {
            "filename": {
                "type": "string",
                "description": "Name to save as. Defaults to a timestamped .png.",
            },
            "full_page": {
                "type": "boolean",
                "description": "Capture the whole scrollable page rather than the visible part. Default false.",
            },
        }
    ),
)
def browser_screenshot(filename: str | None = None, full_page: bool = False) -> dict:
    def work(session: _Session) -> dict:
        page = session.page()
        name = filename or f"screenshot-{time.strftime('%Y%m%d-%H%M%S')}.png"
        if not name.lower().endswith((".png", ".jpg", ".jpeg")):
            name += ".png"
        destination = unique_path(_downloads_folder(), name)
        page.screenshot(path=str(destination), full_page=full_page)
        log.info("screenshot of %s -> %s", page.url, destination)
        return {
            "status": "saved",
            "path": f"{DOWNLOAD_DIR}/{destination.name}",
            "url": page.url,
            "size_bytes": destination.stat().st_size,
        }

    return _session.call(work)


@tool(
    group=GROUP,
    name="browser_close",
    description=(
        "Close the browser. Do this when the browsing task is finished, or to "
        "recover from a page that has become stuck — the next browser_open starts "
        "a clean window. Logins are kept in the profile, so closing does not sign "
        "the user out of anything."
    ),
    schema=obj({}),
)
def browser_close() -> dict:
    if not _session.running:
        return {"status": "closed", "detail": "the browser was not open"}
    return _session.call(lambda session: session.shutdown(), timeout=30)
