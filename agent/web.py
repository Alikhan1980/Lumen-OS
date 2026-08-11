"""A local chat window in the browser — no terminal, no commands.

Serves one page on 127.0.0.1 and streams the agent's replies to it. Same Agent
core as the CLI, so tools, the approval gate, and history behave identically.

Locked down for a tool that can send mail: bound to loopback only, and every
request must carry a key minted at startup and handed to the page through the
URL the launcher opens.

An AI provider key entered in this page is posted once to this loopback server,
handed straight to the OS credential store, and never sent back: the provider
endpoints below return masked tails only, and the page has no way to ask for
more. The chat endpoint refuses to run at all until a provider is configured —
that check is the agent's, not the page's.
"""

from __future__ import annotations

import json
import os
import queue
import re
import secrets
import sys
import threading
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote

from . import registry
from .auth import accounts_enabled as auth_accounts_enabled
from .auth import routes as auth_routes
from .auth import shared as auth_shared
from .config import CREDENTIALS_DIR
from .core import Agent, Callbacks
from .prompts import STYLE_LABELS, STYLE_PROMPTS
from .providers import ProviderError, ProviderNotConfigured, catalog

DEFAULT_PORT = 8765
KEY_PATH = CREDENTIALS_DIR / "webkey"


def access_key() -> str:
    """A stable key for this install, so the chat URL can be bookmarked.

    Regenerating per launch would change the link every time. Kept next to the
    Google token — anyone who can read it already has the far more sensitive
    credential sitting beside it.
    """
    if KEY_PATH.exists():
        existing = KEY_PATH.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    key = secrets.token_urlsafe(24)
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    KEY_PATH.write_text(key, encoding="utf-8")
    return key


def chat_url(port: int = DEFAULT_PORT) -> str:
    return f"http://localhost:{port}/?k={access_key()}"


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # ThreadingHTTPServer sets allow_reuse_address = 1, which on Windows means
    # SO_REUSEADDR lets a *second* process bind a port already in use: bind
    # succeeds, two servers fight over connections, and which one answers is
    # anyone's guess. On POSIX the flag only skips TIME_WAIT, so keep it there.
    allow_reuse_address = sys.platform != "win32"


def _already_running(port: int, key: str) -> bool:
    """Is the thing holding this port our own agent, or something unrelated?"""
    import urllib.error
    import urllib.request

    try:
        request = urllib.request.Request(f"http://127.0.0.1:{port}/?k={key}")
        # Scheme and host are literals here, so S310's open-redirect concern
        # does not apply.
        with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False

# How long an approval prompt waits before giving up and denying. Guards against
# a browser tab closed mid-decision leaving the turn wedged forever.
APPROVAL_TIMEOUT_SECONDS = 300

# Serialises the page's own Google reads — see _google_view.
google_lock = threading.Lock()

# Attachments land in the workspace, which is the only place on disk the agent
# can read. Capped because the body is held in memory while it is written.
ATTACH_DIR = "attachments"
MAX_ATTACH_BYTES = 25 * 1024 * 1024


def _safe_name(raw: str) -> str:
    """A filename that cannot escape the folder it is meant for."""
    name = PurePosixPath(raw.replace("\\", "/")).name.strip()
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name)
    name = name.lstrip(".") or "attachment"
    return name[:120]


def save_attachment(workspace: Path, filename: str, data: bytes) -> dict:
    """Write an uploaded file into the workspace, without clobbering anything."""
    folder = workspace / ATTACH_DIR
    folder.mkdir(parents=True, exist_ok=True)

    name = _safe_name(filename)
    target = folder / name
    stem, suffix = target.stem, target.suffix
    counter = 2
    while target.exists():
        target = folder / f"{stem} ({counter}){suffix}"
        counter += 1

    target.write_bytes(data)
    # file_read decodes as text, so a binary file would reach the model as
    # replacement characters. Say so up front rather than letting it try.
    readable = b"\x00" not in data[:4096]
    return {
        "name": target.name,
        "path": f"{ATTACH_DIR}/{target.name}",
        "size_bytes": len(data),
        "readable": readable,
    }


class WebCallbacks(Callbacks):
    """Turns agent events into SSE messages, and approvals into a round trip."""

    def __init__(self, events: queue.Queue, show_thinking: bool):
        self.events = events
        self.show_thinking = show_thinking
        self._pending: dict[str, tuple[threading.Event, list[bool]]] = {}

    def emit(self, payload: dict) -> None:
        self.events.put(payload)

    def on_text(self, delta: str) -> None:
        self.emit({"type": "text", "text": delta})

    def on_thinking(self, delta: str) -> None:
        if self.show_thinking:
            self.emit({"type": "thinking", "text": delta})

    def on_tool_start(self, name: str, params: dict) -> None:
        spec = registry.get(name)
        self.emit(
            {
                "type": "tool",
                "name": name,
                "group": spec.group if spec else "",
                "params": params,
                # Sent with the event rather than mapped in the page, so the
                # terminal and the browser say the same thing.
                "activity": registry.activity(name),
            }
        )

    def on_tool_end(self, name: str, result: Any, is_error: bool) -> None:
        if is_error:
            summary = str(result)[:200]
        elif isinstance(result, dict):
            if "count" in result:
                summary = f"{result['count']} result(s)"
            elif "status" in result:
                summary = str(result["status"])
            else:
                summary = "done"
        else:
            summary = "done"
        self.emit({"type": "tool_end", "name": name, "summary": summary, "error": is_error})

    def on_notice(self, message: str) -> None:
        self.emit({"type": "notice", "text": message})

    def confirm(self, name: str, params: dict) -> bool:
        request_id = secrets.token_urlsafe(8)
        gate, answer = threading.Event(), [False]
        self._pending[request_id] = (gate, answer)
        self.emit({"type": "approve", "id": request_id, "name": name, "params": params})

        if not gate.wait(APPROVAL_TIMEOUT_SECONDS):
            self._pending.pop(request_id, None)
            self.emit({"type": "notice", "text": f"{name} timed out waiting for approval."})
            return False

        self._pending.pop(request_id, None)
        return answer[0]

    def resolve(self, request_id: str, approved: bool) -> bool:
        entry = self._pending.get(request_id)
        if not entry:
            return False
        gate, answer = entry
        answer[0] = approved
        gate.set()
        return True


class AccountManager:
    """Google sign-in and sign-out, driven from the page.

    The OAuth flow blocks until the user finishes in Google's tab, which can be
    minutes, so it runs on its own thread and the page polls for the result
    rather than holding a request open.
    """

    def __init__(self, session: ChatSession):
        self.session = session
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._error: str | None = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def state(self) -> dict:
        from .tools.google_auth import cached_account_email, is_signed_in

        with self._lock:
            return {
                "email": cached_account_email(),
                "signed_in": is_signed_in(),
                "signing_in": self.busy,
                "error": self._error,
            }

    def start_sign_in(self) -> bool:
        with self._lock:
            if self.busy:
                return False
            self._error = None
            self._thread = threading.Thread(target=self._sign_in, daemon=True)
            self._thread.start()
            return True

    def _sign_in(self) -> None:
        from .tools.google_auth import (
            get_credentials,
            refresh_account_email,
            reset_service_cache,
        )

        try:
            # force_login runs the consent flow with the account chooser and
            # only overwrites the stored token once it succeeds, so a cancelled
            # sign-in leaves the previous account working.
            get_credentials(force_login=True)
            reset_service_cache()
            refresh_account_email()
        except Exception as exc:
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
            return
        # A different person is now driving; their chat must not start with the
        # previous account's mail and files already in context.
        self.session.agent.reset()

    def sign_out(self) -> None:
        from .tools.google_auth import sign_out as forget_account

        forget_account()
        self.session.agent.reset()
        with self._lock:
            self._error = None


class ChatSession:
    """One agent, one conversation. Serialized — a turn at a time."""

    def __init__(self, agent: Agent):
        self.agent = agent
        self.lock = threading.Lock()
        self.callbacks: WebCallbacks | None = None

    def run_turn(self, message: str) -> queue.Queue:
        events: queue.Queue = queue.Queue()
        callbacks = WebCallbacks(events, self.agent.config.show_thinking)
        self.callbacks = callbacks
        self.agent.callbacks = callbacks

        def worker() -> None:
            try:
                self.agent.send(message)
            except ProviderNotConfigured as exc:
                # The lock. The page reopens setup on this signal rather than
                # showing it as an ordinary failure.
                events.put({"type": "locked", "text": str(exc)})
            except ProviderError as exc:
                events.put({"type": "error", "text": exc.message, "code": exc.code})
            except Exception as exc:
                # Type name only: an exception's str() can carry whatever a
                # library put in it, and this goes to the page.
                events.put({"type": "error", "text": f"Something went wrong ({type(exc).__name__})."})
            finally:
                events.put({"type": "done"})

        threading.Thread(target=worker, daemon=True).start()
        return events


def _handler_class(
    session: ChatSession,
    key: str,
    page: str,
    holder: dict | None = None,
    accounts: AccountManager | None = None,
):
    holder = holder if holder is not None else {}
    accounts = accounts if accounts is not None else AccountManager(session)
    # The signed-in Lumen user for this process. One per process because a
    # desktop app has one person at the keyboard; the API server derives
    # identity per request instead and never touches this. None when no accounts
    # server is configured, and then /api/auth/* is not served at all rather
    # than served by something with nowhere to send the request.
    auth_session = auth_shared() if auth_accounts_enabled() else None

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:
            pass  # the browser is the UI; keep the console quiet

        # ------------------------------------------------------------ helpers

        def _cookie_key(self) -> str:
            jar = SimpleCookie(self.headers.get("Cookie", ""))
            morsel = jar.get("agentkey")
            return morsel.value if morsel else ""

        def _authorized(self) -> bool:
            """Accept the key from the header (fetch) or the cookie (bookmark)."""
            return secrets.compare_digest(
                self.headers.get("X-Agent-Key", ""), key
            ) or secrets.compare_digest(self._cookie_key(), key)

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length))
            except ValueError:
                return {}

        # --------------------------------------------------- google-backed views

        @staticmethod
        def _params(query: str) -> dict:
            out: dict[str, str] = {}
            for part in query.split("&"):
                name, _, value = part.partition("=")
                if name:
                    out[name] = unquote(value.replace("+", " "))
            return out

        def _google_view(self, work) -> None:
            """Run a read against Google and answer with its result or its error.

            The page renders whatever comes back, so a failure has to arrive as
            a normal response with a message in it rather than an HTTP error
            the fetch layer would turn into a blank screen.
            """
            from .tools.google_auth import cached_account_email, is_signed_in

            if not is_signed_in():
                self._send(
                    200, json.dumps({"signed_in": False, "email": None}).encode(),
                    "application/json",
                )
                return

            try:
                # Serialised because the Google client this shares with the
                # agent's own tools keeps one connection per API, and two
                # threads driving it at once is not safe.
                with google_lock:
                    payload = work()
            except Exception as exc:
                payload = {"error": f"{type(exc).__name__}: {exc}"}

            payload["signed_in"] = True
            payload["email"] = cached_account_email()
            self._send(200, json.dumps(payload).encode(), "application/json")

        def _send_mail(self, path: str, query: str) -> None:
            from .tools.gmail import ui_inbox, ui_message

            params = self._params(query)
            if path == "/api/message":
                message_id = params.get("id", "")
                if not message_id:
                    self._send(400, b'{"error":"missing id"}', "application/json")
                    return
                self._google_view(lambda: ui_message(message_id))
                return

            try:
                limit = int(params.get("limit", 25))
            except ValueError:
                limit = 25
            self._google_view(lambda: ui_inbox(params.get("q") or None, limit))

        # ----------------------------------------------------------- calendar

        # ---------------------------------------------------------- reminders

        def _reminders_get(self, query: str) -> None:
            """The Reminders page's whole state in one call.

            Local, not Google, so it answers even when nobody is signed in —
            which is why it does not go through _google_view.
            """
            from .notify import task_status
            from .reminders import store

            params = self._params(query)
            scope = params.get("scope") or "upcoming"
            try:
                items = store().list(
                    scope=scope,
                    search=params.get("q") or None,
                    tag=params.get("tag") or None,
                    limit=200,
                )
                payload = {
                    "scope": scope,
                    "reminders": items,
                    "counts": store().counts(),
                }
                # Only when asked: reading the task state shells out to
                # schtasks, which is too slow to do on every list refresh.
                if params.get("schedule") == "1":
                    payload["schedule"] = task_status()
                if params.get("since") == "1":
                    payload["recently_fired"] = store().recently_fired()
            except Exception as exc:
                payload = {"error": f"{type(exc).__name__}: {exc}"}
            self._send(200, json.dumps(payload).encode(), "application/json")

        def _reminders_post(self) -> None:
            from .reminders import ReminderError, store

            body = self._json_body()
            action = str(body.get("action") or "").lower()
            reminder_id = str(body.get("id") or "")

            try:
                if action == "create":
                    result = store().create(
                        title=body.get("title") or "",
                        due=body.get("due") or "",
                        notes=body.get("notes") or "",
                        recurrence=body.get("recurrence"),
                        tags=body.get("tags") or "",
                    )
                elif action == "update":
                    result = store().update(
                        reminder_id,
                        title=body.get("title"),
                        due=body.get("due"),
                        notes=body.get("notes"),
                        recurrence=body.get("recurrence"),
                        tags=body.get("tags"),
                    )
                elif action == "complete":
                    result = store().complete(reminder_id)
                elif action == "reopen":
                    result = store().reopen(reminder_id)
                elif action == "snooze":
                    result = store().snooze(reminder_id, int(body.get("minutes") or 10))
                elif action == "delete":
                    result = store().delete(reminder_id)
                elif action in {"enable_notifications", "disable_notifications"}:
                    from .notify import install_task, remove_task

                    result = (
                        install_task()
                        if action == "enable_notifications"
                        else remove_task()
                    )
                else:
                    self._send(400, b'{"error":"unknown action"}', "application/json")
                    return
            except ReminderError as exc:
                # The user's own mistake — a bad date, a reminder already gone.
                # Answer 200 with a message the page can show inline.
                self._send(200, json.dumps({"error": str(exc)}).encode(), "application/json")
                return
            except Exception as exc:
                self._send(
                    200,
                    json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode(),
                    "application/json",
                )
                return

            self._send(200, json.dumps({"ok": True, "result": result}).encode(), "application/json")

        # ---------------------------------------------------------- providers

        def _providers_get(self) -> None:
            """The provider screen's whole state. Contains no key material.

            `status()` returns the masked tail and nothing else — there is no
            endpoint anywhere that can return a stored key, which is what makes
            "the key never leaves the keystore" true rather than aspirational.
            """
            manager = session.agent.manager
            try:
                payload = manager.status()
            except Exception as exc:
                payload = {"error": f"could not read provider settings ({type(exc).__name__})"}
            self._send(200, json.dumps(payload).encode(), "application/json")

        def _providers_post(self) -> None:
            manager = session.agent.manager
            body = self._json_body()
            action = str(body.get("action") or "").lower()
            provider_id = str(body.get("provider") or "")

            try:
                if action == "add":
                    # The only request in the app that carries a key, and it
                    # goes to 127.0.0.1. Popped out of the body immediately so
                    # nothing downstream can log the dict it arrived in.
                    key = str(body.pop("key", "") or "")
                    result = manager.add(provider_id, key, body.get("model"))
                    key = ""
                    payload = {"ok": result.ok, **result.as_dict()}
                elif action == "test":
                    result = manager.test(provider_id)
                    payload = {"ok": result.ok, **result.as_dict()}
                elif action == "activate":
                    manager.set_active(provider_id)
                    payload = {"ok": True}
                elif action == "model":
                    manager.set_model(provider_id, str(body.get("model") or ""))
                    payload = {"ok": True}
                elif action == "models":
                    models = manager.models_for(provider_id, live=bool(body.get("live")))
                    payload = {"ok": True, "models": [m.as_dict() for m in models]}
                elif action == "remove":
                    payload = {"ok": True, **manager.remove(provider_id)}
                elif action == "fallback":
                    manager.set_fallback(bool(body.get("enabled")), body.get("order"))
                    payload = {"ok": True}
                else:
                    self._send(400, b'{"error":"unknown action"}', "application/json")
                    return
            except ProviderNotConfigured as exc:
                payload = {"ok": False, "message": str(exc), "code": "NOT_CONFIGURED"}
            except ProviderError as exc:
                payload = {"ok": False, "message": exc.message, "code": exc.code}
            except ValueError as exc:
                payload = {"ok": False, "message": str(exc), "code": "INVALID_REQUEST"}
            except Exception as exc:
                payload = {
                    "ok": False,
                    "message": f"Could not save that ({type(exc).__name__}).",
                    "code": "UNKNOWN_PROVIDER_ERROR",
                }

            # Every mutation returns the new state, so the page never has to
            # guess what the lock or the active provider became.
            payload["state"] = manager.status()
            self._send(200, json.dumps(payload).encode(), "application/json")

        def _send_calendar(self, query: str) -> None:
            """The week the calendar view is showing, for the signed-in account."""
            from .tools.calendar import ui_agenda

            params = self._params(query)
            try:
                days = int(params.get("days", 7))
            except ValueError:
                days = 7
            self._google_view(lambda: ui_agenda(params.get("start") or None, days))

        # ---------------------------------------------------------------- GET

        def do_GET(self) -> None:
            path, _, query = self.path.partition("?")

            # Sign-in state, before anything else: this is the one API call the
            # page makes while signed out, and it is what tells it to draw the
            # sign-in screen instead of the app.
            if auth_routes.is_auth_path(path):
                if not self._authorized():
                    self._send(403, b'{"error":"forbidden"}', "application/json")
                    return
                if auth_session is None:
                    self._send(404, b'{"error":"no accounts"}', "application/json")
                    return
                status, payload = auth_routes.handle(
                    auth_session, path, "GET", {}
                )
                self._send(status, auth_routes.json_bytes(payload), "application/json")
                return

            if path == "/api/account":
                if not self._authorized():
                    self._send(403, b'{"error":"forbidden"}', "application/json")
                    return
                body = json.dumps(accounts.state()).encode()
                self._send(200, body, "application/json")
                return

            if path == "/api/style":
                if not self._authorized():
                    self._send(403, b'{"error":"forbidden"}', "application/json")
                    return
                self._send(
                    200,
                    json.dumps(
                        {
                            "style": session.agent.style,
                            "options": [
                                {"id": key, "label": label} for key, label in STYLE_LABELS
                            ],
                        }
                    ).encode(),
                    "application/json",
                )
                return

            if path == "/api/providers":
                if not self._authorized():
                    self._send(403, b'{"error":"forbidden"}', "application/json")
                    return
                self._providers_get()
                return

            if path == "/api/reminders":
                if not self._authorized():
                    self._send(403, b'{"error":"forbidden"}', "application/json")
                    return
                self._reminders_get(query)
                return

            if path in {"/api/calendar", "/api/inbox", "/api/message"}:
                if not self._authorized():
                    self._send(403, b'{"error":"forbidden"}', "application/json")
                    return
                if path == "/api/calendar":
                    self._send_calendar(query)
                else:
                    self._send_mail(path, query)
                return

            if path != "/":
                self._send(404, b"not found", "text/plain")
                return

            supplied = ""
            for part in query.split("&"):
                name, _, value = part.partition("=")
                if name == "k":
                    supplied = value

            from_url = secrets.compare_digest(supplied, key)
            if not (from_url or secrets.compare_digest(self._cookie_key(), key)):
                self._send(
                    403,
                    b"Open the app using its desktop shortcut, or the full link "
                    b"including the ?k=... part.",
                    "text/plain",
                )
                return

            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if from_url:
                # Remember this browser, so plain localhost:8765 works from now on.
                self.send_header(
                    "Set-Cookie",
                    f"agentkey={key}; Path=/; Max-Age=31536000; SameSite=Strict",
                )
            self.end_headers()
            self.wfile.write(body)

        # --------------------------------------------------------------- POST

        def do_POST(self) -> None:
            if not self._authorized():
                self._send(403, b'{"error":"forbidden"}', "application/json")
                return

            path, _, _query = self.path.partition("?")
            if auth_routes.is_auth_path(path):
                if auth_session is None:
                    self._send(404, b'{"error":"no accounts"}', "application/json")
                    return
                status, payload = auth_routes.handle(
                    auth_session, path, "POST", self._json_body()
                )
                self._send(status, auth_routes.json_bytes(payload), "application/json")
                return

            if self.path == "/api/approve":
                body = self._json_body()
                callbacks = session.callbacks
                ok = bool(
                    callbacks
                    and callbacks.resolve(body.get("id", ""), bool(body.get("approved")))
                )
                self._send(200, json.dumps({"ok": ok}).encode(), "application/json")
                return

            if self.path == "/api/style":
                wanted = str(self._json_body().get("style", "")).lower()
                if wanted != "default" and wanted not in STYLE_PROMPTS:
                    self._send(400, b'{"error":"unknown style"}', "application/json")
                    return
                # Takes effect on the next turn; a turn already streaming keeps
                # the style it started under.
                session.agent.style = wanted
                self._send(
                    200, json.dumps({"ok": True, "style": wanted}).encode(), "application/json"
                )
                return

            if self.path == "/api/attach":
                # Raw body with the name in a header, rather than multipart:
                # the stdlib lost its multipart parser in 3.13 and one file per
                # request needs no boundary handling.
                filename = unquote(self.headers.get("X-Filename", "")).strip()
                length = int(self.headers.get("Content-Length", 0) or 0)
                if not filename or length <= 0:
                    self._send(400, b'{"error":"missing file"}', "application/json")
                    return
                if length > MAX_ATTACH_BYTES:
                    self._send(
                        413,
                        json.dumps(
                            {"error": f"file is larger than {MAX_ATTACH_BYTES // (1024*1024)} MB"}
                        ).encode(),
                        "application/json",
                    )
                    return
                try:
                    saved = save_attachment(
                        session.agent.config.workspace, filename, self.rfile.read(length)
                    )
                except OSError as exc:
                    self._send(
                        200,
                        json.dumps({"error": f"could not save it: {exc}"}).encode(),
                        "application/json",
                    )
                    return
                self._send(200, json.dumps(saved).encode(), "application/json")
                return

            if self.path == "/api/providers":
                self._providers_post()
                return

            if self.path == "/api/reminders":
                self._reminders_post()
                return

            if self.path == "/api/reset":
                session.agent.reset()
                self._send(200, b'{"ok":true}', "application/json")
                return

            if self.path in {"/api/signin", "/api/signout"}:
                # Changing account mid-turn would swap the credentials out from
                # under running tools and clear history the turn still needs.
                if session.lock.locked():
                    self._send(
                        409,
                        b'{"error":"The agent is still working. Wait for it to '
                        b'finish before changing account."}',
                        "application/json",
                    )
                    return
                if self.path == "/api/signout":
                    accounts.sign_out()
                    self._send(200, b'{"ok":true}', "application/json")
                    return
                started = accounts.start_sign_in()
                self._send(
                    200 if started else 409,
                    json.dumps({"ok": started, "started": started}).encode(),
                    "application/json",
                )
                return

            if self.path == "/api/quit":
                self._send(200, b'{"ok":true}', "application/json")
                server = holder.get("server")
                if server is not None:
                    # shutdown() blocks until serve_forever returns, so it
                    # cannot run on the thread currently handling this request.
                    threading.Thread(target=server.shutdown, daemon=True).start()
                return

            if self.path != "/api/chat":
                self._send(404, b'{"error":"not found"}', "application/json")
                return

            message = (self._json_body().get("message") or "").strip()
            if not message:
                self._send(400, b'{"error":"empty message"}', "application/json")
                return

            # The lock, at the edge as well as in the agent. Answering 423 here
            # means a page with a stale setup screen cannot start a turn, and
            # `agent.send` refuses independently if this is ever bypassed.
            if not session.agent.manager.is_unlocked():
                self._send(
                    423,
                    json.dumps(
                        {"error": session.agent.manager.lock_reason(), "locked": True}
                    ).encode(),
                    "application/json",
                )
                return

            if not session.lock.acquire(blocking=False):
                self._send(409, b'{"error":"busy"}', "application/json")
                return

            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

                events = session.run_turn(message)
                while True:
                    event = events.get()
                    try:
                        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break  # tab closed; the turn finishes on its own
                    if event.get("type") == "done":
                        break
            finally:
                session.lock.release()
                self.close_connection = True

    return Handler


def serve(
    agent: Agent,
    account_email: str | None,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> None:
    from .ui import console

    key = access_key()
    page = _page(agent, account_email)
    session = ChatSession(agent)

    # Ask first rather than inferring from a bind failure — see _Server for why
    # a failure can't be relied on here. Double-clicking the shortcut twice
    # should reopen the tab, not fork a rival server.
    if _already_running(port, key):
        url = f"http://localhost:{port}/?k={key}"
        console.print("\n[green]Already running.[/green] Opening the existing window.")
        console.print(f"  [bold]{url}[/bold]")
        if open_browser and not os.environ.get("AGENT_NO_BROWSER"):
            webbrowser.open(url)
        return

    holder: dict = {}
    handler = _handler_class(session, key, page, holder, AccountManager(session))
    try:
        server = _Server(("127.0.0.1", port), handler)
    except OSError:
        try:
            server = _Server(("127.0.0.1", 0), handler)  # something else holds the port
        except OSError as exc:
            raise RuntimeError("could not bind a local port for the chat window") from exc

    holder["server"] = server  # lets /api/quit stop the server from the page

    url = f"http://localhost:{server.server_port}/?k={key}"
    console.print("\n[green]Chat window ready.[/green]")
    console.print(f"  [bold]{url}[/bold]")
    if server.server_port != port:
        console.print(
            f"  [yellow]Port {port} was busy, using {server.server_port} this time.[/yellow]"
        )
    console.print("\n[dim]Leave this window open while you use it. Close it to quit.[/dim]")
    if open_browser and not os.environ.get("AGENT_NO_BROWSER"):
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[dim]Closed.[/dim]")
    finally:
        server.server_close()


def _page(agent: Agent, account_email: str | None) -> str:
    groups: dict[str, int] = {}
    for spec in registry.all_tools():
        groups[spec.group] = groups.get(spec.group, 0) + 1
    apps = ", ".join(sorted(groups))
    # The account and the provider are both re-read from their APIs after load
    # and whenever they change, so these are only the values the page opens
    # with.
    active = agent.provider_id
    provider_class = catalog.provider_class(active) if active else None
    label = f"{provider_class.name} · {agent.model}" if provider_class else "no API key yet"
    subtitle = f"{len(registry.all_tools())} tools · {label} · your own API key"
    # The auth screens are injected rather than inlined so this file does not
    # grow a second UI. See agent/auth/screens.py for why they are part of this
    # page at all instead of a separate route.
    #
    # With no accounts server they are not injected at all, and the placeholders
    # collapse to nothing: no overlay to get past, no menu items that would fail
    # if clicked, and a page that behaves exactly as it did before there was a
    # server to sign in to. Hiding them in CSS would leave a sign-in form in the
    # document of an app that has no accounts.
    from .auth import accounts_enabled, screens

    parts = (
        {
            "/*__AUTH_CSS__*/": screens.CSS,
            "<!--__AUTH_HTML__-->": screens.HTML,
            "<!--__AUTH_MENU__-->": screens.MENU_ITEMS,
            "<!--__VERIFY_BAR__-->": screens.VERIFY_BAR,
            "/*__AUTH_JS__*/": screens.JS,
        }
        if accounts_enabled()
        else {}
    )

    page = (
        PAGE_TEMPLATE.replace("__SUBTITLE__", subtitle)
        .replace("__ACCOUNT__", account_email or "not signed in")
        .replace("__APPS__", apps)
    )
    for placeholder in (
        "/*__AUTH_CSS__*/",
        "<!--__AUTH_HTML__-->",
        "<!--__AUTH_MENU__-->",
        "<!--__VERIFY_BAR__-->",
        "/*__AUTH_JS__*/",
    ):
        page = page.replace(placeholder, parts.get(placeholder, ""))
    return page


PAGE_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lumen OS</title>
<!-- Inline, so the browser never requests /favicon.ico and logs a 404 for it. -->
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='9' fill='%234b4bd8'/><path d='M9 16.5l13-6-5 13-1.6-5.4z' fill='%23fff'/></svg>">
<style>
  /* Palette sampled from the design reference: page and rail #f6f6f6, the
     content panel white, hairlines #ededed. Light only — the reference has no
     dark variant, and inventing one would drift from it. */
  :root {
    --bg: #f6f6f6;
    --panel: #ffffff;
    --line: #ededed;
    --line-soft: #f1f1f1;
    --ink: #0f0f10;
    --muted: #8b8b8b;
    --faint: #a9a9a9;
    --head-soft: #7d7d7d;
    --dark: #0d0d0f;
    --navy: #0b1426;
    --indigo: #4b4bd8;
    --warn: #b45309;
    --err: #c0362c;
    --rail: 260px;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.55 "Inter", -apple-system, "Segoe UI Variable Text", "Segoe UI",
          system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  button { font: inherit; color: inherit; }
  .app { display: flex; height: 100%; }

  /* ------------------------------------------------------------------ rail */

  .sidebar {
    width: var(--rail); flex: none; display: flex; flex-direction: column;
    padding: 20px 18px 14px; gap: 18px; min-height: 0;
  }
  .brand { display: flex; align-items: center; gap: 9px; padding: 0 2px; }
  .brand .mark {
    width: 27px; height: 27px; border-radius: 8px; background: var(--indigo);
    display: grid; place-items: center; flex: none;
  }
  .brand .mark svg { width: 15px; height: 15px; }
  .wordmark { font-size: 20px; font-weight: 700; letter-spacing: -0.02em; }

  .search {
    display: flex; align-items: center; gap: 8px; height: 36px;
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 9px; padding: 0 10px; color: var(--faint);
  }
  .search svg { width: 15px; height: 15px; color: var(--ink); flex: none; }
  .search input {
    flex: 1; min-width: 0; border: 0; background: none; font: inherit;
    font-size: 13px; color: var(--ink); outline: none;
  }
  .search input::placeholder { color: var(--faint); }
  .search kbd {
    font: inherit; font-size: 11px; color: var(--faint); letter-spacing: .06em;
  }

  .nav { flex: 1; overflow-y: auto; min-height: 0; margin: 0 -4px; padding: 0 4px; }
  .nav::-webkit-scrollbar { width: 0; }
  .group + .group { margin-top: 16px; border-top: 1px solid var(--line); padding-top: 16px; }
  .group-head {
    display: flex; align-items: center; justify-content: space-between;
    padding: 0 6px 6px; color: var(--faint); font-size: 12.5px;
  }
  .chev {
    width: 17px; height: 17px; border-radius: 50%; border: 0; background: var(--dark);
    color: #fff; display: grid; place-items: center; cursor: pointer; flex: none;
    transition: transform .18s ease;
  }
  .chev svg { width: 9px; height: 9px; }
  .group.collapsed .chev { transform: rotate(-90deg); }
  .group.collapsed .items { display: none; }
  .item {
    display: flex; align-items: center; gap: 10px; padding: 7px 6px;
    border-radius: 8px; color: var(--muted); font-size: 13.5px;
  }
  .item svg { width: 16px; height: 16px; flex: none; opacity: .85; }
  /* Decorative: these name views this build does not have, so they are shown
     but deliberately not clickable — a pointer cursor would promise a screen
     that is not there. */
  .item[data-inert] { cursor: default; }
  .item[data-view] { cursor: pointer; }
  .item[data-view]:hover { background: #efefef; color: var(--ink); }
  .item.active { background: #ebebec; color: var(--ink); font-weight: 550; }
  .item.active svg { opacity: 1; }
  /* How many reminders want attention today. Sits in the rail rather than
     anywhere louder — it is a nudge, not an alert. */
  .item .count {
    margin-left: auto; min-width: 19px; height: 19px; padding: 0 6px;
    border-radius: 999px; background: var(--indigo); color: #fff;
    font-size: 11px; font-weight: 600; display: grid; place-items: center;
  }
  .item .count[hidden] { display: none; }
  /* The API key badge is the one thing in the rail that is not a count: it
     marks the screen standing between the user and a working agent. */
  .item .count.warn { background: var(--warn); }

  .side-foot { flex: none; }
  .help-card {
    background: var(--panel); border: 1px solid var(--line); border-radius: 15px;
    padding: 16px 14px 14px; text-align: center;
  }
  .help-card h3 { margin: 0; font-size: 14px; font-weight: 700; letter-spacing: -0.01em; }
  .help-card p { margin: 5px 0 12px; font-size: 11.5px; color: var(--faint); }
  .chat-pill {
    display: inline-flex; align-items: center; gap: 7px; margin: 0 auto;
    border: 1px solid #c9c9f2; background: #fff; color: var(--indigo);
    border-radius: 999px; padding: 8px 16px; font-size: 13px; font-weight: 600;
    cursor: pointer;
  }
  .chat-pill:hover { background: #f7f7ff; }
  .chat-pill svg { width: 14px; height: 14px; }

  .account {
    width: 100%; display: flex; align-items: center; gap: 10px; margin-top: 14px;
    padding: 8px 6px; border: 0; background: none; border-radius: 11px;
    cursor: pointer; text-align: left;
  }
  .account:hover { background: #efefef; }
  .avatar {
    width: 34px; height: 34px; border-radius: 50%; flex: none;
    background: linear-gradient(140deg, #6f6ae8, #b06ae8 55%, #e8a06a);
    display: grid; place-items: center; color: #fff; font-size: 13px; font-weight: 700;
  }
  .who { flex: 1; min-width: 0; }
  .who b {
    display: block; font-size: 13.5px; font-weight: 650; letter-spacing: -0.01em;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .who i { font-style: normal; font-size: 11.5px; color: var(--faint); }
  .account .updown { color: var(--ink); flex: none; }
  .account .updown svg { width: 13px; height: 13px; }

  .menu {
    position: absolute; bottom: 62px; left: 18px; width: calc(var(--rail) - 36px);
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    box-shadow: 0 14px 40px rgba(0,0,0,.13); padding: 6px; z-index: 30;
  }
  .menu button {
    display: block; width: 100%; text-align: left; border: 0; background: none;
    padding: 9px 10px; border-radius: 8px; font-size: 13px; cursor: pointer;
  }
  .menu button:hover { background: #f4f4f4; }
  .menu button:disabled { opacity: .4; cursor: default; }
  .menu button:disabled:hover { background: none; }
  .menu button.danger:hover { color: var(--err); }
  .menu .meta {
    padding: 8px 10px 6px; margin-top: 4px; border-top: 1px solid var(--line);
    font-size: 11px; color: var(--faint); line-height: 1.45;
  }

  /* ----------------------------------------------------------------- panel */

  .panel {
    flex: 1; min-width: 0; background: var(--panel); border-radius: 14px;
    margin: 5px 5px 5px 0; display: flex; flex-direction: column; overflow: hidden;
  }
  .topbar {
    height: 46px; flex: none; display: flex; align-items: center; gap: 12px;
    padding: 0 20px; border-bottom: 1px solid var(--line-soft);
  }
  .crumb { font-size: 12.5px; color: var(--faint); display: flex; gap: 7px; }
  .crumb b { font-weight: 500; color: var(--ink); }
  .topbar .spacer { flex: 1; }
  .icon-btn {
    width: 28px; height: 28px; border-radius: 8px; border: 0; background: none;
    color: var(--ink); display: grid; place-items: center; cursor: pointer;
  }
  .icon-btn:hover { background: #f3f3f3; }
  .icon-btn svg { width: 15px; height: 15px; }
  .icon-btn[data-inert] { cursor: default; }
  .icon-btn[data-inert]:hover { background: none; }

  .head {
    flex: none; display: flex; align-items: center; gap: 14px;
    padding: 22px 30px 6px;
  }
  .head h1 { margin: 0; font-size: 33px; font-weight: 600; letter-spacing: -0.03em; }
  .head-actions { margin-left: auto; display: flex; gap: 10px; }
  .pill {
    display: inline-flex; align-items: center; gap: 8px; height: 36px;
    padding: 0 16px; border-radius: 999px; border: 1px solid var(--line);
    background: #fff; font-size: 13px; cursor: pointer; white-space: nowrap;
  }
  .pill:hover { background: #f7f7f7; }
  .pill svg { width: 14px; height: 14px; }
  .pill[data-inert] { cursor: default; }
  .pill[data-inert]:hover { background: #fff; }
  .pill.dark { background: var(--dark); border-color: var(--dark); color: #fff; font-weight: 500; }
  .pill.dark:hover { background: #26262a; }

  .body {
    flex: 1; min-height: 0; overflow-y: auto; padding: 0 30px;
    display: flex; flex-direction: column;
  }
  /* Class rules set display, so the hidden attribute needs saying explicitly
     for anything the view switch hides. */
  .body[hidden], .composer-zone[hidden], .head-actions[hidden],
  .cal-grid[hidden], .cal-state[hidden],
  .mail-list[hidden], .mail-read[hidden], .mail-state[hidden],
  .rem-list[hidden], .rem-state[hidden] { display: none; }
  .wrap { max-width: 850px; width: 100%; margin: 0 auto; padding-bottom: 8px;
          flex: 1 0 auto; display: flex; flex-direction: column; }

  /* ------------------------------------------------------------------ hero */

  /* Grows to fill whatever the transcript is not using, which centres the
     greeting in the gap above the composer. */
  .hero {
    flex: 1; display: flex; flex-direction: column;
    align-items: center; justify-content: center;
  }
  /* A class rule beats the UA sheet's [hidden] { display: none }, so the
     attribute alone would leave the hero on screen behind the transcript. */
  .hero[hidden] { display: none; }
  .greet {
    margin: 0; font-size: 43px; font-weight: 500; letter-spacing: -0.035em;
    text-align: center; color: var(--head-soft);
  }
  .greet b { font-weight: 700; color: var(--ink); }

  /* ------------------------------------------------------------ transcript */

  .msg { margin-bottom: 22px; }
  .who-label {
    font-size: 10.5px; text-transform: uppercase; letter-spacing: .1em;
    color: var(--faint); margin-bottom: 6px;
  }
  .you .bubble {
    background: #f4f4f5; border-radius: 14px; padding: 12px 16px;
    white-space: pre-wrap; overflow-wrap: anywhere; font-size: 14.5px;
  }
  .agent .bubble { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 14.5px; }
  .tool {
    font-size: 12.5px; color: var(--muted); margin: 7px 0; display: flex; gap: 9px;
    align-items: flex-start;
    font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
  }
  .tool .dot { color: var(--indigo); }
  .tool .args { overflow-wrap: anywhere; opacity: .75; }
  .tool.err .dot, .tool.err .args { color: var(--err); }
  .notice { color: var(--warn); font-size: 13px; margin: 8px 0; }
  .thinking {
    color: var(--muted); font-style: italic; font-size: 13.5px;
    white-space: pre-wrap; margin-bottom: 8px;
  }
  .thinking.live::after {
    content: ''; display: inline-block; width: 2px; height: 1em;
    background: var(--muted); vertical-align: -2px; margin-left: 2px;
    animation: caret 1s step-end infinite;
  }
  @keyframes caret { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }

  /* The only thing on screen during the silent wait for the model, so it has
     to move — a static line reads as a stall. */
  .working {
    display: flex; align-items: center; gap: 9px; margin: 8px 0 4px;
    color: var(--muted); font-size: 13px;
  }
  .working .dots { display: inline-flex; gap: 4px; }
  .working .dots i {
    width: 6px; height: 6px; border-radius: 50%; background: var(--indigo);
    animation: bob 1.15s ease-in-out infinite;
  }
  .working .dots i:nth-child(2) { animation-delay: .15s; }
  .working .dots i:nth-child(3) { animation-delay: .3s; }
  .working .label { animation: breathe 2.4s ease-in-out infinite; }
  @keyframes bob {
    0%, 70%, 100% { transform: translateY(0); opacity: .3; }
    35% { transform: translateY(-4px); opacity: 1; }
  }
  @keyframes breathe { 0%, 100% { opacity: .65; } 50% { opacity: 1; } }
  @media (prefers-reduced-motion: reduce) {
    .working .dots i, .working .label, .thinking.live::after { animation: none; }
    .working .dots i { opacity: .6; }
  }

  /* -------------------------------------------------------------- composer */

  .composer-zone { flex: none; padding: 10px 30px 32px; }
  .composer {
    max-width: 850px; margin: 0 auto; position: relative;
    background: #fff; border: 1px solid var(--line); border-radius: 17px;
    box-shadow: 0 1px 2px rgba(0,0,0,.03); padding: 16px 18px 12px;
  }
  .composer:focus-within { border-color: #dcdcdc; }
  .ph {
    position: absolute; top: 16px; left: 18px; right: 18px; font-size: 14.5px;
    pointer-events: none; color: var(--faint); display: flex; gap: 8px;
    align-items: baseline;
  }
  .ph b { font-weight: 400; color: var(--ink); }
  .ph .spark { color: var(--ink); font-size: 13px; }
  .composer.filled .ph { display: none; }
  #input {
    display: block; width: 100%; min-height: 140px; max-height: 280px;
    border: 0; outline: none; resize: none; background: none;
    font: inherit; font-size: 14.5px; color: var(--ink);
  }
  .composer-foot { display: flex; align-items: center; gap: 18px; padding-top: 6px; }
  .chip {
    display: inline-flex; align-items: center; gap: 6px; border: 0; background: none;
    padding: 0; font-size: 12.5px; color: var(--muted); cursor: default;
  }
  .chip.live { cursor: pointer; }
  .chip.live:hover { color: var(--ink); }
  /* Listening has to be unmistakable — the mic is recording the room. */
  .chip.rec { color: var(--err); }
  .chip.rec svg { animation: pulse 1.4s ease-in-out infinite; }
  .chip.rec #micLabel { margin-left: 2px; font-size: 12px; }
  @keyframes pulse { 0%, 100% { opacity: .45; } 50% { opacity: 1; } }
  @media (prefers-reduced-motion: reduce) { .chip.rec svg { animation: none; } }

  /* Opens upward: the composer sits at the bottom of the panel. */
  .style-menu {
    position: absolute; bottom: 44px; left: 14px; z-index: 20; min-width: 190px;
    background: #fff; border: 1px solid var(--line); border-radius: 12px;
    box-shadow: 0 14px 40px rgba(0,0,0,.14); padding: 6px;
  }
  .style-menu button {
    display: flex; align-items: center; gap: 9px; width: 100%; text-align: left;
    border: 0; background: none; padding: 8px 10px; border-radius: 8px;
    font-size: 13px; cursor: pointer;
  }
  .style-menu button:hover { background: #f4f4f4; }
  .style-menu .tick { width: 13px; flex: none; color: var(--indigo); visibility: hidden; }
  .style-menu button[aria-checked="true"] .tick { visibility: visible; }
  .style-menu button[aria-checked="true"] { font-weight: 600; }

  .attached { display: flex; flex-wrap: wrap; gap: 8px; padding: 2px 0 8px; }
  .attached:empty { display: none; }
  .att {
    display: inline-flex; align-items: center; gap: 8px; max-width: 320px;
    border: 1px solid var(--line); border-radius: 9px; padding: 6px 8px 6px 10px;
    font-size: 12.5px; background: #fafafa;
  }
  .att .nm { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .att .sz { color: var(--faint); font-size: 11.5px; flex: none; }
  .att .x {
    border: 0; background: none; color: var(--faint); cursor: pointer; padding: 0 2px;
    font-size: 14px; line-height: 1; flex: none;
  }
  .att .x:hover { color: var(--err); }
  .att.binary { border-color: #e8d9b8; background: #fdf8ec; }
  .att.binary .sz { color: var(--warn); }
  .att.pending { opacity: .55; }
  .chip svg { width: 14px; height: 14px; }
  .composer-foot .right { margin-left: auto; display: flex; align-items: center; gap: 12px; }
  .send {
    width: 36px; height: 36px; border-radius: 50%; border: 0; background: var(--navy);
    color: #fff; display: grid; place-items: center; cursor: pointer;
  }
  .send:hover { background: #16223a; }
  .send:disabled { opacity: .4; cursor: default; }
  .send svg { width: 15px; height: 15px; }

  /* The composer with no API key behind it. It replaces the box rather than
     covering the window: chat is the only screen that cannot work without a
     key, and mail, calendar and reminders all still can. */
  #keylock {
    max-width: 850px; margin: 0 auto; display: flex; align-items: center; gap: 14px;
    background: #fff; border: 1px solid var(--line); border-radius: 17px;
    padding: 15px 16px 15px 18px; box-shadow: 0 1px 2px rgba(0,0,0,.03);
  }
  #keylock[hidden] { display: none; }
  #keylock .ico {
    width: 34px; height: 34px; border-radius: 50%; flex: none; display: grid;
    place-items: center; background: #fdf3e3; color: var(--warn);
  }
  #keylock .ico svg { width: 17px; height: 17px; }
  #keylock .body { flex: 1; min-width: 0; }
  #keylock .name { font-weight: 600; font-size: 14px; }
  #keylock .sub { font-size: 12.5px; color: var(--muted); margin-top: 2px; }
  #keylock button {
    flex: none; border: 0; border-radius: 999px; background: var(--dark); color: #fff;
    padding: 10px 18px; font: inherit; font-size: 13px; font-weight: 600; cursor: pointer;
  }
  #keylock button:hover { background: #16223a; }

  /* -------------------------------------------------------------- calendar */

  /* Same tokens as the rest of the shell; the only colours that come from
     outside are the per-calendar ones Google itself assigns. */
  .cal { display: none; flex-direction: column; min-height: 0; flex: 1; }
  .cal.on { display: flex; }
  .cal-legend {
    display: flex; flex-wrap: wrap; gap: 8px 18px; padding: 0 30px 14px;
    font-size: 12.5px; color: var(--muted);
  }
  .cal-legend span { display: inline-flex; align-items: center; gap: 7px; }
  .cal-legend i { width: 9px; height: 9px; border-radius: 3px; flex: none; }
  .cal-state { padding: 40px 30px; color: var(--muted); font-size: 14px; text-align: center; }
  .cal-state b { color: var(--ink); font-weight: 600; }

  .cal-grid {
    flex: 1; min-height: 0; overflow-y: auto; border-top: 1px solid var(--line);
    margin: 0 30px 26px; border-left: 1px solid var(--line);
    border-right: 1px solid var(--line); border-radius: 0 0 12px 12px;
  }
  /* Day names and the all-day strip pin together: scrolling to the afternoon
     must not hide which day you are looking at, or an all-day event. */
  .cal-sticky { position: sticky; top: 0; z-index: 3; background: var(--panel); }
  .cal-head {
    display: grid; grid-template-columns: 62px repeat(7, 1fr);
    border-bottom: 1px solid var(--line);
  }
  .cal-head .cell { padding: 9px 6px 10px; text-align: center; border-left: 1px solid var(--line); }
  .cal-head .cell:first-child { border-left: 0; }
  .cal-head .dow { font-size: 11px; letter-spacing: .07em; text-transform: uppercase; color: var(--faint); }
  .cal-head .dom { font-size: 19px; font-weight: 600; letter-spacing: -0.02em; margin-top: 3px; }
  .cal-head .cell.today .dom {
    background: var(--dark); color: #fff; border-radius: 999px;
    width: 32px; height: 32px; line-height: 32px; margin: 3px auto 0;
  }
  .cal-head .cell.today .dow { color: var(--ink); }

  .allday {
    display: grid; grid-template-columns: 62px repeat(7, 1fr);
    border-bottom: 1px solid var(--line); background: #fcfcfc;
  }
  .allday .rowlabel {
    font-size: 10.5px; color: var(--faint); text-align: right; padding: 6px 8px 6px 0;
    text-transform: uppercase; letter-spacing: .06em;
  }
  .allday .daycol { border-left: 1px solid var(--line); padding: 5px 5px; min-height: 30px; }
  .allday .evt { position: static; margin-bottom: 4px; }

  .cal-body { display: grid; grid-template-columns: 62px repeat(7, 1fr); position: relative; }
  .hours { position: relative; }
  .hours .hr {
    height: 52px; font-size: 11px; color: var(--faint); text-align: right;
    padding-right: 8px; transform: translateY(-6px);
  }
  .daycol { position: relative; border-left: 1px solid var(--line); }
  .daycol .slot { height: 52px; border-bottom: 1px solid var(--line-soft); }
  .daycol.weekend { background: #fcfcfc; }
  .nowline { position: absolute; left: 0; right: 0; height: 0; border-top: 2px solid #e5484d; z-index: 2; }
  .nowline::before {
    content: ''; position: absolute; left: -4px; top: -5px; width: 8px; height: 8px;
    border-radius: 50%; background: #e5484d;
  }

  .evt {
    position: absolute; left: 3px; right: 3px; overflow: hidden;
    border-radius: 7px; border-left: 3px solid var(--evt, var(--indigo));
    background: var(--evt-bg, #eef); padding: 4px 7px; font-size: 11.5px;
    line-height: 1.35; color: var(--ink); cursor: default; z-index: 1;
  }
  .evt .t { font-weight: 600; display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .evt .when { color: var(--muted); font-size: 10.5px; }
  .evt.short { padding: 2px 7px; }
  .evt.short .when { display: none; }

  /* ----------------------------------------------------------------- inbox */

  .mail { display: none; flex: 1; min-height: 0; }
  .mail.on { display: flex; }
  .mail-list {
    width: 400px; flex: none; overflow-y: auto; border-right: 1px solid var(--line);
    border-top: 1px solid var(--line);
  }
  .mail-read {
    flex: 1; min-width: 0; overflow-y: auto; padding: 26px 30px 30px;
    border-top: 1px solid var(--line);
  }
  .mail-state { padding: 40px 30px; color: var(--muted); font-size: 14px; text-align: center; }
  .mail-state b { color: var(--ink); font-weight: 600; }

  .mrow {
    /* minmax(0,1fr) rather than 1fr: a long unbroken subject would otherwise
       widen the track past the pane and scroll the whole list sideways. */
    display: grid; grid-template-columns: 34px minmax(0, 1fr) auto; gap: 11px;
    padding: 12px 18px 12px 14px; border-bottom: 1px solid var(--line-soft);
    cursor: pointer; align-items: start;
  }
  .mrow:hover { background: #fafafa; }
  .mrow.active { background: #f1f1f2; }
  .mrow .pic {
    width: 34px; height: 34px; border-radius: 50%; display: grid; place-items: center;
    color: #fff; font-size: 13px; font-weight: 650; flex: none;
  }
  /* Each on its own line: as inline spans they share a line whenever sender
     and subject happen to be short, which reads as one run-on string. */
  .mrow .who, .mrow .subj, .mrow .snip {
    display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .mrow .who { font-size: 13.5px; color: var(--muted); }
  .mrow .subj { font-size: 13px; color: var(--ink); margin-top: 1px; }
  .mrow .snip { font-size: 12px; color: var(--faint); margin-top: 2px; }
  .mrow .when { font-size: 11.5px; color: var(--faint); white-space: nowrap; padding-top: 2px; }
  /* Unread is carried by weight and a dot, not colour — the palette has no
     spare accent that would not read as an error or a calendar. */
  .mrow.unread .who, .mrow.unread .subj { font-weight: 650; color: var(--ink); }
  .mrow.unread .when { color: var(--ink); }
  .mrow .dot {
    width: 6px; height: 6px; border-radius: 50%; background: var(--indigo);
    display: inline-block; margin-right: 6px; vertical-align: 1px;
  }
  .mrow:not(.unread) .dot { visibility: hidden; }

  .mail-read h2 {
    margin: 0 0 14px; font-size: 21px; font-weight: 600; letter-spacing: -0.02em;
    overflow-wrap: anywhere;
  }
  .mail-meta { display: flex; align-items: center; gap: 11px; margin-bottom: 18px; }
  .mail-meta .pic { width: 38px; height: 38px; border-radius: 50%; display: grid;
                    place-items: center; color: #fff; font-weight: 650; font-size: 14px; flex: none; }
  .mail-meta .lines { min-width: 0; flex: 1; }
  .mail-meta .lines b { display: block; font-size: 14px; font-weight: 600; }
  .mail-meta .lines span { font-size: 12.5px; color: var(--faint); }
  .mail-body {
    white-space: pre-wrap; overflow-wrap: anywhere; font-size: 14px; line-height: 1.65;
    border-top: 1px solid var(--line); padding-top: 18px; max-width: 760px;
  }
  .mail-attach { margin-top: 18px; font-size: 12.5px; color: var(--muted); }
  .mail-attach b { color: var(--ink); font-weight: 600; }
  .mail-open {
    display: inline-flex; align-items: center; gap: 7px; margin-top: 20px;
    border: 1px solid var(--line); border-radius: 999px; padding: 8px 15px;
    font-size: 12.5px; color: var(--ink); text-decoration: none;
  }
  .mail-open:hover { background: #f7f7f7; }
  .mail-open svg { width: 13px; height: 13px; }

  /* ------------------------------------------------------------- reminders */

  .rem { display: none; flex: 1; min-height: 0; flex-direction: column; }
  .rem.on { display: flex; }
  .rem-bar {
    flex: none; display: flex; align-items: center; gap: 10px;
    padding: 8px 30px 14px;
  }
  .rem-search {
    display: flex; align-items: center; gap: 8px; height: 34px; width: 260px;
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 9px; padding: 0 10px; color: var(--faint);
  }
  .rem-search svg { width: 14px; height: 14px; color: var(--ink); flex: none; }
  .rem-search input {
    flex: 1; min-width: 0; border: 0; background: none; font: inherit;
    font-size: 13px; color: var(--ink); outline: none;
  }
  .rem-note { margin-left: auto; font-size: 12.5px; color: var(--faint); }
  .rem-note button {
    border: 1px solid var(--line); background: #fff; border-radius: 999px;
    padding: 5px 12px; font-size: 12.5px; cursor: pointer; margin-left: 8px;
  }
  .rem-note button:hover { background: #f7f7f7; }
  .rem-note .live { color: var(--indigo); font-weight: 600; }

  .rem-list { flex: 1; min-height: 0; overflow-y: auto; border-top: 1px solid var(--line); }
  .rem-state { padding: 44px 30px; color: var(--muted); font-size: 14px; text-align: center; }
  .rem-state b { color: var(--ink); font-weight: 600; }

  .rem-group {
    padding: 16px 30px 6px; font-size: 11.5px; letter-spacing: .07em;
    text-transform: uppercase; color: var(--faint); font-weight: 600;
  }
  .rrow {
    display: grid; grid-template-columns: 22px minmax(0, 1fr) auto;
    gap: 12px; align-items: start;
    padding: 11px 30px; border-bottom: 1px solid var(--line-soft);
  }
  .rrow:hover { background: #fafafa; }
  .rrow:hover .rem-acts { opacity: 1; }
  /* Named rtick, not tick: the writing-style menu already owns `.tick`, and an
     unscoped rule here would put a checkbox border round its checkmarks. */
  .rtick {
    width: 19px; height: 19px; margin-top: 1px; border-radius: 50%;
    border: 1.5px solid #cfcfcf; background: #fff; cursor: pointer; padding: 0;
    display: grid; place-items: center; flex: none;
  }
  .rtick:hover { border-color: var(--indigo); }
  .rtick svg { width: 11px; height: 11px; color: #fff; opacity: 0; }
  .rrow.done .rtick { background: var(--indigo); border-color: var(--indigo); }
  .rrow.done .rtick svg { opacity: 1; }
  .rrow .what { min-width: 0; }
  .rrow .name {
    font-size: 14px; color: var(--ink); overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap;
  }
  .rrow.done .name { color: var(--faint); text-decoration: line-through; }
  .rrow .sub { font-size: 12px; color: var(--faint); margin-top: 2px;
               overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .rrow .when { font-size: 12.5px; color: var(--muted); white-space: nowrap; padding-top: 1px; }
  .rrow.late .when { color: var(--err); font-weight: 600; }
  .chip-tag {
    display: inline-block; margin-left: 7px; padding: 1px 7px; border-radius: 999px;
    background: #f1f1f2; color: var(--muted); font-size: 11px; vertical-align: 1px;
  }
  .chip-rep { background: rgba(75,75,216,.1); color: var(--indigo); }
  .rem-acts { display: flex; gap: 4px; opacity: 0; transition: opacity .12s; }
  .rem-acts button {
    border: 0; background: none; color: var(--muted); cursor: pointer;
    border-radius: 7px; padding: 3px 7px; font-size: 12px;
  }
  .rem-acts button:hover { background: #ededed; color: var(--ink); }
  .rem-acts .danger:hover { color: var(--err); }

  .rem-form { display: grid; gap: 12px; margin-bottom: 20px; }
  .rem-form label { display: grid; gap: 5px; font-size: 12.5px; color: var(--muted); }
  .rem-form input, .rem-form select, .rem-form textarea {
    font: inherit; font-size: 13.5px; color: var(--ink); background: #fff;
    border: 1px solid var(--line); border-radius: 9px; padding: 9px 11px; outline: none;
  }
  .rem-form input:focus, .rem-form select:focus, .rem-form textarea:focus { border-color: #d3d3d8; }
  .rem-form textarea { resize: vertical; min-height: 58px; }
  .rem-form .pair { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .rem-err { color: var(--err); font-size: 12.5px; min-height: 16px; }

  /* A reminder that has just rung, while the page is open. */
  .rem-toast {
    position: fixed; right: 20px; bottom: 20px; z-index: 45; width: 320px;
    background: #fff; border: 1px solid var(--line); border-radius: 14px;
    padding: 14px 16px; box-shadow: 0 18px 44px rgba(0,0,0,.18);
  }
  .rem-toast b { display: block; font-size: 14px; margin-bottom: 2px; }
  .rem-toast span { font-size: 12.5px; color: var(--muted); }
  .rem-toast .close {
    position: absolute; top: 8px; right: 10px; border: 0; background: none;
    color: var(--faint); cursor: pointer; font-size: 15px;
  }

  /* ----------------------------------------------------------------- modal */

  .modal-bg {
    position: fixed; inset: 0; background: rgba(15,15,16,.42); z-index: 40;
    display: flex; align-items: center; justify-content: center; padding: 20px;
  }
  .modal {
    background: #fff; border-radius: 18px; max-width: 560px; width: 100%;
    padding: 24px; box-shadow: 0 24px 60px rgba(0,0,0,.28);
  }
  .modal h2 { margin: 0 0 4px; font-size: 17px; font-weight: 650; letter-spacing: -0.01em; }
  .modal p.lead { margin: 0 0 18px; color: var(--muted); font-size: 13.5px; }
  .modal dl {
    margin: 0 0 22px; display: grid; grid-template-columns: auto 1fr; gap: 8px 16px;
    font-size: 13.5px;
  }
  .modal dt { font-weight: 600; color: var(--muted); }
  .modal dd { margin: 0; overflow-wrap: anywhere; white-space: pre-wrap; }
  .modal .row { display: flex; gap: 10px; justify-content: flex-end; }
  .modal button { border-radius: 999px; padding: 10px 20px; font-size: 13.5px; cursor: pointer; }
  .deny { background: #fff; border: 1px solid var(--line); }
  .allow { background: var(--dark); border: 1px solid var(--dark); color: #fff; font-weight: 550; }

  #gone {
    position: fixed; inset: 0; background: var(--bg); z-index: 50;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 8px; text-align: center; padding: 24px;
  }
  #gone h2 { margin: 0; font-size: 19px; font-weight: 650; }
  #gone p { margin: 0; color: var(--muted); font-size: 14px; }

  /* ------------------------------------------------------------- providers */

  /* The gate. Covers everything while it is up, so the key is the only thing
     on screen — but it is dismissible, and what it uncovers is an app whose
     mail, calendar and reminders never needed an AI key in the first place. */
  #setup {
    position: fixed; inset: 0; background: var(--bg); z-index: 60;
    overflow-y: auto; padding: 40px 24px;
  }
  .setup-card {
    max-width: 620px; margin: 0 auto; background: var(--panel);
    border: 1px solid var(--line); border-radius: 20px; padding: 32px;
    box-shadow: 0 20px 60px rgba(0,0,0,.07);
  }
  .setup-card h1 { margin: 0 0 6px; font-size: 22px; font-weight: 700; letter-spacing: -0.02em; }
  .setup-card .lede { margin: 0 0 6px; color: var(--muted); font-size: 14px; }
  .setup-card .byok {
    margin: 18px 0 22px; padding: 13px 15px; border-radius: 12px;
    background: #f6f6fb; border: 1px solid #e6e6f4; font-size: 13px; color: #414166;
  }
  .setup-card .byok b { font-weight: 600; }

  .prov-list { display: flex; flex-direction: column; gap: 10px; }
  .prov {
    display: flex; align-items: center; gap: 13px; text-align: left; width: 100%;
    padding: 15px 17px; border: 1px solid var(--line); border-radius: 14px;
    background: #fff; cursor: pointer; transition: border-color .12s, background .12s;
  }
  .prov:hover { border-color: #d8d8e8; background: #fcfcfe; }
  .prov[aria-current="true"] { border-color: var(--indigo); background: #f8f8ff; }
  .prov .dot {
    width: 22px; height: 22px; border-radius: 50%; flex: none; display: grid;
    place-items: center; border: 1.6px solid #d3d3d3; color: #fff; font-size: 12px;
  }
  .prov.on .dot { background: #12855a; border-color: #12855a; }
  .prov .body { flex: 1; min-width: 0; }
  .prov .name { font-weight: 600; font-size: 14.5px; }
  .prov .sub { font-size: 12.5px; color: var(--muted); margin-top: 1px; }
  .prov .sub.key { font-family: ui-monospace, "Cascadia Mono", Menlo, monospace; letter-spacing: .02em; }
  .prov .tag {
    font-size: 11px; font-weight: 600; padding: 3px 9px; border-radius: 999px;
    background: var(--dark); color: #fff; flex: none;
  }
  .prov .tag.env { background: #fdf3e3; color: var(--warn); }

  .key-form { margin-top: 18px; display: none; }
  .key-form.open { display: block; }
  .key-form label { display: block; font-size: 12.5px; color: var(--muted); margin-bottom: 6px; }
  .key-row { display: flex; gap: 8px; }
  .key-row input {
    flex: 1; min-width: 0; padding: 11px 13px; border: 1px solid var(--line);
    border-radius: 11px; font: inherit; font-size: 14px;
    font-family: ui-monospace, "Cascadia Mono", Menlo, monospace;
  }
  .key-row input:focus { outline: 2px solid var(--indigo); outline-offset: -1px; }
  .key-row .peek {
    border: 1px solid var(--line); background: #fff; border-radius: 11px;
    padding: 0 13px; cursor: pointer; font-size: 12.5px; color: var(--muted); flex: none;
  }
  .key-form .go {
    margin-top: 12px; width: 100%; padding: 12px; border: 0; border-radius: 11px;
    background: var(--dark); color: #fff; font-weight: 600; font-size: 14px; cursor: pointer;
  }
  .key-form .go[disabled] { opacity: .55; cursor: default; }
  .key-form .hint { margin-top: 10px; font-size: 12.5px; color: var(--muted); }
  .key-form .hint a { color: var(--indigo); }
  /* A key that is already on this computer needs a decision, not a paste —
     so the gate leads with "use it" and keeps the input one click away. */
  .key-form .alt {
    margin-top: 10px; width: 100%; padding: 10px; border: 1px solid var(--line);
    background: #fff; border-radius: 11px; font: inherit; font-size: 13px;
    color: var(--muted); cursor: pointer;
  }
  .key-form .alt:hover { border-color: #d8d8e8; color: var(--ink); }
  .setup-card .skip {
    display: block; margin: 18px auto 0; border: 0; background: none; padding: 4px;
    font: inherit; font-size: 12.5px; color: var(--faint); cursor: pointer;
    text-decoration: underline; text-underline-offset: 3px;
  }
  .setup-card .skip:hover { color: var(--muted); }

  /* The same warning as the composer's, at the top of the API Keys screen. */
  .needs-key {
    display: flex; align-items: center; gap: 14px; margin: 4px 0 6px;
    padding: 15px 16px 15px 18px; border-radius: 14px;
    background: #fdf3e3; border: 1px solid #f0dfc0;
  }
  .needs-key .body { flex: 1; min-width: 0; }
  .needs-key .name { font-weight: 600; font-size: 14px; color: #6b4b12; }
  .needs-key .sub { font-size: 12.5px; color: #7a5c22; margin-top: 2px; }
  .needs-key button {
    flex: none; border: 0; border-radius: 999px; background: var(--dark); color: #fff;
    padding: 9px 17px; font: inherit; font-size: 13px; font-weight: 600; cursor: pointer;
  }
  .key-msg { margin-top: 11px; font-size: 13px; min-height: 18px; }
  .key-msg.bad { color: var(--err); }
  .key-msg.good { color: #12855a; }
  .key-msg.busy { color: var(--muted); }

  .store-note {
    margin-top: 20px; padding-top: 16px; border-top: 1px solid var(--line-soft);
    font-size: 12px; color: var(--faint); line-height: 1.5;
  }
  .store-note.warn { color: var(--warn); }

  /* The same provider list, as a settings view rather than a gate. The panel
     is a flex column, so this has to claim the leftover height and scroll
     inside itself — matching .rem / .mail / .cal. */
  .prov-view {
    display: none; flex: 1; min-height: 0; overflow-y: auto;
    padding: 4px 24px 30px 0;
  }
  .prov-view.on { display: block; }
  .prov-view h2 {
    margin: 22px 0 10px; font-size: 12px; font-weight: 650; letter-spacing: .07em;
    text-transform: uppercase; color: var(--head-soft);
  }
  .prov-view h2:first-child { margin-top: 4px; }
  .prov-actions { display: flex; gap: 7px; flex-wrap: wrap; margin-top: 11px; }
  .prov-actions button {
    border: 1px solid var(--line); background: #fff; border-radius: 999px;
    padding: 7px 15px; font-size: 12.5px; cursor: pointer;
  }
  .prov-actions button:hover { border-color: #d8d8e8; }
  .prov-actions button.danger { color: var(--err); }
  .prov-actions button.primary { background: var(--dark); border-color: var(--dark); color: #fff; }
  .prov-detail {
    border: 1px solid var(--line); border-radius: 14px; padding: 16px 18px;
    margin-bottom: 10px; background: #fff;
  }
  .prov-detail .head { display: flex; align-items: center; gap: 10px; }
  .prov-detail .head .name { font-weight: 600; font-size: 14.5px; flex: 1; }
  .prov-detail select {
    margin-top: 11px; width: 100%; padding: 9px 11px; border: 1px solid var(--line);
    border-radius: 10px; font: inherit; font-size: 13.5px; background: #fff;
  }
  .prov-toggle {
    display: flex; align-items: flex-start; gap: 12px; padding: 15px 18px;
    border: 1px solid var(--line); border-radius: 14px; background: #fff;
  }
  .prov-toggle .body { flex: 1; }
  .prov-toggle .name { font-weight: 600; font-size: 14px; }
  .prov-toggle .sub { font-size: 12.5px; color: var(--muted); margin-top: 3px; }
  .prov-toggle input { width: 17px; height: 17px; margin-top: 2px; flex: none; }

  @media (max-width: 900px) {
    .sidebar { display: none; }
    .panel { margin: 5px; }
  }

/*__AUTH_CSS__*/
</style>
</head>
<body>
<!--__AUTH_HTML__-->
<div class="app">

  <aside class="sidebar">
    <div class="brand">
      <span class="mark">
        <svg viewBox="0 0 24 24" fill="none"><path d="M3 12.5L20 5l-6.5 16-2.2-6.9z" fill="#fff"/></svg>
      </span>
      <span class="wordmark">Lumen OS</span>
    </div>

    <div class="search">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>
      <input id="search" placeholder="Search..." autocomplete="off">
      <kbd>&#8984; S</kbd>
    </div>

    <nav class="nav">
      <div class="group">
        <div class="group-head"><span>Overview</span>
          <button class="chev" aria-label="Collapse"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M6 9l6 6 6-6"/></svg></button>
        </div>
        <div class="items">
          <div class="item" data-view="chat" id="navDashboard" role="button" tabindex="0"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>Dashboard</div>
          <div class="item" data-view="inbox" id="navInbox" role="button" tabindex="0"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>Inbox</div>
          <div class="item" data-view="calendar" id="navCalendar" role="button" tabindex="0"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="5" width="18" height="16" rx="2.5"/><path d="M3 10h18M8 3v4M16 3v4"/></svg>Multi-calendar</div>
          <div class="item" data-view="reminders" id="navReminders" role="button" tabindex="0"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M18 8a6 6 0 10-12 0c0 6-2 7-2 7h16s-2-1-2-7"/><path d="M13.7 20a2 2 0 01-3.4 0"/></svg>Reminders<span class="count" id="remBadge" hidden></span></div>
          <div class="item" data-view="providers" id="navProviders" role="button" tabindex="0"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M15 7a4 4 0 11-3.9 5H7v3H4v-3l3.1-3H11A4 4 0 0115 7z"/><circle cx="16" cy="8" r="1.2" fill="currentColor" stroke="none"/></svg>API Keys<span class="count warn" id="provBadge" hidden>!</span></div>
        </div>
      </div>

    </nav>

    <div class="side-foot">
      <div class="help-card">
        <h3>How can I help?</h3>
        <p>Ask me anything just a voice</p>
        <button class="chat-pill" id="focusInput">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M20 13v6a2 2 0 01-2 2H6a2 2 0 01-2-2V7a2 2 0 012-2h6"/><path d="M15 4h5v5M20 4l-8 8"/></svg>
          Chat with AI
        </button>
      </div>

      <button class="account" id="account" aria-haspopup="true" aria-expanded="false">
        <span class="avatar" id="avatar">L</span>
        <span class="who"><b id="acct">__ACCOUNT__</b><i id="acctrole">Google account</i></span>
        <span class="updown"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M8 9l4-4 4 4M8 15l4 4 4-4"/></svg></span>
      </button>
    </div>
  </aside>

  <div class="menu" id="acctMenu" hidden>
    <button id="switch">Switch account</button>
    <button id="signout">Sign out</button>
    <!-- The Lumen account items, when there is a Lumen account: see
         agent/auth/screens.py. They sit under the Google ones because the
         distinction is real -- the sign-out above forgets the connected Google
         account, the one they add ends the Lumen session itself, and people
         want to do those independently. -->
    <!--__AUTH_MENU__-->
    <button id="quit" class="danger">Quit Lumen OS</button>
    <div class="meta">__SUBTITLE__<br>Connected: __APPS__</div>
  </div>

  <main class="panel">
    <div class="topbar">
      <button class="icon-btn" id="back" title="Back to Ask Lumen"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 5l-7 7 7 7"/></svg></button>
      <span class="crumb"><span>Overview</span><span>/</span><b id="crumbLeaf">Ask Lumen</b></span>
      <span class="spacer"></span>
      <button class="icon-btn" data-inert><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M20 12a8 8 0 11-2.3-5.6M20 4v4h-4"/></svg></button>
      <button class="icon-btn" data-inert><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="M7 10h10M7 14h6"/></svg></button>
      <button class="icon-btn" data-inert><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 14v-2a8 8 0 0116 0v2"/><rect x="2.5" y="13.5" width="4.5" height="6" rx="2"/><rect x="17" y="13.5" width="4.5" height="6" rx="2"/></svg></button>
    </div>

    <!--__VERIFY_BAR__-->

    <div class="head">
      <h1 id="title">Ask Lumen</h1>
      <div class="head-actions" id="chatActions">
        <button class="pill" data-inert><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>Search thread</button>
        <button class="pill" data-inert><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 7a2 2 0 012-2h4l2 2.5h8a2 2 0 012 2V18a2 2 0 01-2 2H5a2 2 0 01-2-2z"/></svg>Create folder</button>
        <button class="pill dark" id="newchat"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 5v14M5 12h14"/></svg>New chat</button>
      </div>
      <div class="head-actions" id="mailActions" hidden>
        <button class="pill" id="mailAll">All mail</button>
        <button class="pill" id="mailUnread">Unread</button>
        <button class="pill dark" id="mailRefresh"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 12a8 8 0 11-2.3-5.6M20 4v4h-4"/></svg>Refresh</button>
      </div>
      <div class="head-actions" id="remActions" hidden>
        <button class="pill" id="remToday">Today</button>
        <button class="pill" id="remUpcoming">Upcoming</button>
        <button class="pill" id="remDone">Completed</button>
        <button class="pill dark" id="remNew"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 5v14M5 12h14"/></svg>New reminder</button>
      </div>
      <div class="head-actions" id="calActions" hidden>
        <button class="pill" id="calPrev" aria-label="Previous week"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 5l-7 7 7 7"/></svg></button>
        <button class="pill" id="calToday">Today</button>
        <button class="pill" id="calNext" aria-label="Next week"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5l7 7-7 7"/></svg></button>
        <button class="pill dark" id="calRefresh"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 12a8 8 0 11-2.3-5.6M20 4v4h-4"/></svg>Refresh</button>
      </div>
    </div>

    <div class="mail" id="mailView">
      <div class="mail-state" id="mailState" hidden></div>
      <div class="mail-list" id="mailList" hidden></div>
      <div class="mail-read" id="mailRead" hidden></div>
    </div>

    <div class="rem" id="remView">
      <div class="rem-bar">
        <div class="rem-search">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>
          <input id="remSearch" placeholder="Search reminders..." autocomplete="off">
        </div>
        <div class="rem-note" id="remSchedule"></div>
      </div>
      <div class="rem-state" id="remState" hidden></div>
      <div class="rem-list" id="remList" hidden></div>
    </div>

    <div class="prov-view" id="provView"></div>

    <div class="cal" id="calView">
      <div class="cal-legend" id="calLegend"></div>
      <div class="cal-state" id="calState" hidden></div>
      <div class="cal-grid" id="calGrid" hidden>
        <div class="cal-sticky">
          <div class="cal-head" id="calHead"></div>
          <div class="allday" id="calAllDay"></div>
        </div>
        <div class="cal-body" id="calBody"></div>
      </div>
    </div>

    <div class="body" id="log">
      <div class="wrap">
        <div class="hero" id="hero">
          <h2 class="greet">Hello, what's on <b>your mind?</b></h2>
        </div>
      </div>
    </div>

    <div class="composer-zone">
      <div id="keylock" hidden>
        <span class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="4" y="10" width="16" height="10" rx="2.5"/><path d="M8 10V7a4 4 0 018 0v3"/></svg></span>
        <div class="body">
          <div class="name">Add an API key to start chatting</div>
          <div class="sub" id="keylockWhy">Lumen has no AI provider connected yet.</div>
        </div>
        <button type="button" id="keylockGo">Add API key</button>
      </div>
      <form class="composer" id="composer">
        <div class="ph" id="ph">
          <span class="spark">&#10022;</span>
          <span><b>Ask me anything - I'm your AI assistant</b> with advanced capabilities!</span>
        </div>
        <textarea id="input" rows="1" autofocus></textarea>
        <div class="attached" id="attached"></div>
        <input type="file" id="filePicker" multiple hidden>
        <div class="composer-foot">
          <button type="button" class="chip live" id="attachBtn" title="Copy files into the agent's workspace so it can read them"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M20 11l-8.5 8.5a4.5 4.5 0 01-6.4-6.4l9-9a3 3 0 014.3 4.3l-9 9a1.5 1.5 0 01-2.2-2.2l8.3-8.3"/></svg>Attach</button>
          <button type="button" class="chip live" id="styleBtn" aria-haspopup="true" aria-expanded="false"><span id="styleLabel">Writing Styles</span> <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9l6 6 6-6"/></svg></button>
          <div class="style-menu" id="styleMenu" hidden></div>
          <div class="right">
            <button type="button" class="chip live" id="micBtn" title="Dictate with your microphone"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0014 0M12 18v3"/></svg><span id="micLabel"></span></button>
            <button class="send" id="send" type="submit" aria-label="Send">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 19V5M6 11l6-6 6 6"/></svg>
            </button>
          </div>
        </div>
      </form>
    </div>
  </main>
</div>

<!-- The gate. Rendered hidden and shown by the provider state that arrives on
     load, so a page opened with no key never flashes a usable chat box. It can
     be dismissed, because the rest of the app works without an AI key — but
     that only puts the chat behind the bar in the composer. It is a courtesy
     to the user either way, not the enforcement: /api/chat answers 423 and the
     agent refuses independently. -->
<div id="setup" hidden>
  <div class="setup-card">
    <h1 id="setupTitle">Add your API key</h1>
    <p class="lede" id="setupLede">Lumen needs an API key before it can answer anything.</p>
    <div class="byok">
      <b>You bring your own API key.</b> This app includes no AI usage of its own.
      You pay your chosen provider directly for what you use, at their prices and
      under their terms. Your key is stored on this computer and is sent only to
      that provider.
    </div>
    <div class="prov-list" id="setupList"></div>
    <div class="key-form" id="setupForm">
      <button type="button" class="go" id="setupUse" hidden></button>
      <div id="setupKeyWrap">
        <label for="setupKey" id="setupKeyLabel">API key</label>
        <div class="key-row">
          <input type="password" id="setupKey" autocomplete="off" spellcheck="false"
                 autocapitalize="off" placeholder="">
          <button type="button" class="peek" id="setupPeek">Show</button>
        </div>
        <button type="button" class="go" id="setupGo">Connect</button>
        <div class="hint" id="setupHint"></div>
      </div>
      <button type="button" class="alt" id="setupReplace" hidden>Use a different key</button>
      <div class="key-msg" id="setupMsg"></div>
    </div>
    <div class="store-note" id="setupStore"></div>
    <button type="button" class="skip" id="setupSkip">Not now — look around without the AI</button>
  </div>
</div>

<script>
const KEY = new URLSearchParams(location.search).get('k');
history.replaceState(null, '', location.pathname);   // keep the key out of the address bar

/*__AUTH_JS__*/

const log = document.getElementById('log');
const wrap = log.querySelector('.wrap');
const hero = document.getElementById('hero');
const form = document.getElementById('composer');
const input = document.getElementById('input');
const send = document.getElementById('send');
const composer = form;
let busy = false;

function atBottom() { return log.scrollHeight - log.scrollTop - log.clientHeight < 80; }
function scroll(force) { if (force || atBottom()) log.scrollTop = log.scrollHeight; }

function block(cls, who) {
  hero.hidden = true;
  const el = document.createElement('div');
  el.className = 'msg ' + cls;
  el.innerHTML = '<div class="who-label"></div><div class="bubble"></div>';
  el.querySelector('.who-label').textContent = who;
  wrap.appendChild(el);
  scroll(true);
  return el.querySelector('.bubble');
}

function line(cls, html) {
  hero.hidden = true;
  const el = document.createElement('div');
  el.className = cls;
  el.innerHTML = html;
  wrap.appendChild(el);
  scroll();
  return el;
}

// The working indicator always sits at the bottom of the log: every new line
// removes it first and re-adds it after, so it stays below whatever just landed.
let working = null;

function showWorking(label) {
  hideWorking();
  working = line('working', '<span class="dots"><i></i><i></i><i></i></span><span class="label"></span>');
  working.querySelector('.label').textContent = label;
  scroll();
}

function hideWorking() {
  if (working) { working.remove(); working = null; }
}

const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const short = (o, n) => { const s = typeof o === 'string' ? o : JSON.stringify(o);
                          return s.length > n ? esc(s.slice(0, n)) + '…' : esc(s); };

// Every call here goes to the agent on this machine, which stops answering the
// moment the user quits it or closes the terminal. fetch() rejects on that, so
// a bare call would leave an uncaught rejection in the console on the way out.
// Returning null instead lets each caller say something useful on screen.
async function api(path, options) {
  const opts = options || {};
  const headers = {...opts.headers};
  if (KEY) headers['X-Agent-Key'] = KEY;  // absent after a reload; the cookie carries it then
  try {
    return await fetch(path, {...opts, headers});
  } catch {
    return null;
  }
}

const DEAD = 'Lumen OS is not responding. It may have been quit or closed.';

function approve(ev) {
  return new Promise(resolve => {
    const bg = document.createElement('div');
    bg.className = 'modal-bg';
    const rows = Object.entries(ev.params || {})
      .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${short(v, 600)}</dd>`).join('');
    bg.innerHTML = `<div class="modal">
        <h2>Approve this action?</h2>
        <p class="lead">${esc(ev.name)} — this leaves your computer or is hard to undo.</p>
        <dl>${rows}</dl>
        <div class="row"><button class="deny">Skip</button><button class="allow">Run it</button></div>
      </div>`;
    document.body.appendChild(bg);
    const answer = ok => {
      bg.remove();
      api('/api/approve', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: ev.id, approved: ok})
      });
      line('tool', `<span class="dot">${ok ? '✓' : '✗'}</span><span class="args">${ok ? 'approved' : 'skipped'} ${esc(ev.name)}</span>`);
      resolve();
    };
    bg.querySelector('.allow').onclick = () => answer(true);
    bg.querySelector('.deny').onclick = () => answer(false);
    bg.querySelector('.allow').focus();
  });
}

// `display` is what goes in the bubble; `text` is what the model receives. They
// differ when attachments append a note the user does not need read back.
async function ask(text, display, attachedPaths) {
  if (busy) return;
  // The composer is hidden while no key is connected, so this is only reachable
  // if the key went away mid-session. Send them to the screen that fixes it
  // rather than to a 423 they have to read.
  if (provState && !provState.unlocked) { showView('providers'); openSetup(); return; }
  busy = true; send.disabled = true;
  block('you', 'you').textContent = display === undefined ? text : display;
  if (attachedPaths && attachedPaths.length) {
    line('tool', '<span class="dot">+</span><span class="args">attached ' +
                 attachedPaths.map(esc).join(', ') + '</span>');
  }

  let bubble = null, thinking = null;
  const settleThinking = () => { if (thinking) { thinking.classList.remove('live'); thinking = null; } };
  showWorking('thinking…');

  try {
    const res = await api('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text})
    });
    if (!res) throw new Error(DEAD);
    if (res.status === 423) {
      // The server's own lock. It refuses before the agent is even reached, so
      // there is nothing to stream — reopen the gate and stop here.
      const body = await res.json().catch(() => ({}));
      await loadProviders();
      throw new Error(body.error || 'Connect an AI provider to use this agent.');
    }
    if (!res.ok) throw new Error(res.status === 409 ? 'still working on the previous message' : 'request failed');

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      const parts = buffer.split('\n\n');
      buffer = parts.pop();
      for (const part of parts) {
        if (!part.startsWith('data: ')) continue;
        const ev = JSON.parse(part.slice(6));
        if (ev.type === 'text') {
          hideWorking(); settleThinking();
          if (!bubble) bubble = block('agent', 'lumen');
          bubble.textContent += ev.text;
          scroll();
        } else if (ev.type === 'thinking') {
          hideWorking();
          if (!thinking) thinking = line('thinking live', '');
          thinking.textContent += ev.text;
          scroll();
        } else if (ev.type === 'tool') {
          hideWorking(); settleThinking();
          bubble = null;
          line('tool', `<span class="dot">▸</span><span><b>${esc(ev.name)}</b> <span class="args">${short(ev.params, 150)}</span></span>`);
          showWorking((ev.activity || 'running ' + ev.name) + '…');
        } else if (ev.type === 'tool_end') {
          hideWorking();
          line('tool' + (ev.error ? ' err' : ''), `<span class="dot">${ev.error ? '✗' : '✓'}</span><span class="args">${esc(ev.summary)}</span>`);
          showWorking('thinking…');
        } else if (ev.type === 'approve') {
          hideWorking(); settleThinking();
          bubble = null;
          await approve(ev);
          showWorking('thinking…');
        } else if (ev.type === 'notice') {
          hideWorking(); settleThinking();
          line('notice', esc(ev.text));
          showWorking('thinking…');
        } else if (ev.type === 'locked') {
          // The provider went away mid-session — removed in another tab, or
          // the active one was cleared. Reopen the gate rather than leave a
          // dead composer sitting there.
          hideWorking(); settleThinking();
          line('notice', esc(ev.text));
          loadProviders();
        } else if (ev.type === 'error') {
          hideWorking(); settleThinking();
          line('notice', esc(ev.text));
        }
      }
    }
  } catch (err) {
    line('notice', esc(err.message || String(err)));
  } finally {
    hideWorking();
    if (thinking) thinking.classList.remove('live');
    busy = false; send.disabled = false; input.focus(); scroll();
  }
}

// ------------------------------------------------------------ writing style

// The style is held by the agent, not the page, so it survives a reload and
// applies to whatever the next turn sends.
const styleBtn = document.getElementById('styleBtn');
const styleMenu = document.getElementById('styleMenu');
const styleLabel = document.getElementById('styleLabel');
let styleOptions = [];
let currentStyle = 'default';

const TICK = '<svg class="tick" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6"><path d="M5 13l4 4 10-10"/></svg>';

function paintStyle() {
  const picked = styleOptions.find(o => o.id === currentStyle);
  styleLabel.textContent = !picked || picked.id === 'default' ? 'Writing Styles' : picked.label;
  styleMenu.innerHTML = '';
  for (const option of styleOptions) {
    const b = document.createElement('button');
    b.type = 'button';
    b.setAttribute('aria-checked', String(option.id === currentStyle));
    b.innerHTML = TICK + '<span></span>';
    b.querySelector('span').textContent = option.label;
    b.onclick = async () => {
      closeStyleMenu();
      const previous = currentStyle;
      currentStyle = option.id;
      paintStyle();
      const res = await api('/api/style', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({style: option.id}),
      });
      if (!res || !res.ok) {          // put the label back rather than lie about it
        currentStyle = previous;
        paintStyle();
        line('notice', res ? 'That writing style was not accepted.' : DEAD);
      }
    };
    styleMenu.appendChild(b);
  }
}

function closeStyleMenu() {
  styleMenu.hidden = true;
  styleBtn.setAttribute('aria-expanded', 'false');
}

styleBtn.onclick = e => {
  e.stopPropagation();
  styleMenu.hidden = !styleMenu.hidden;
  styleBtn.setAttribute('aria-expanded', String(!styleMenu.hidden));
};
document.addEventListener('click', e => { if (!styleMenu.contains(e.target)) closeStyleMenu(); });

(async () => {
  const res = await api('/api/style');
  if (!res || !res.ok) return;
  const data = await res.json().catch(() => null);
  if (!data) return;
  styleOptions = data.options || [];
  currentStyle = data.style || 'default';
  paintStyle();
})();

// ---------------------------------------------------------------- dictation

// Chrome's built-in speech recognition. Note this is the one part of the app
// that leaves the machine on its own: Chrome streams the audio to Google to
// transcribe it. Nothing is sent until the button is pressed.
const micBtn = document.getElementById('micBtn');
const micLabel = document.getElementById('micLabel');
const SpeechAPI = window.SpeechRecognition || window.webkitSpeechRecognition;
let recogniser = null;
let micBase = '';          // what was already typed when dictation started

function setMic(on, label) {
  composer.classList.toggle('filled', on || input.value.length > 0);
  micBtn.classList.toggle('rec', on);
  micLabel.textContent = label || '';
}

function stopDictation() {
  if (recogniser) { const r = recogniser; recogniser = null; try { r.stop(); } catch {} }
  setMic(false);
}

micBtn.onclick = () => {
  if (recogniser) { stopDictation(); return; }
  if (!SpeechAPI) {
    line('notice', 'This browser has no speech recognition. Chrome supports it.');
    return;
  }

  const r = new SpeechAPI();
  r.continuous = true;
  r.interimResults = true;
  r.lang = navigator.language || 'en-US';
  micBase = input.value.trim();

  r.onstart = () => setMic(true, 'listening…');

  r.onresult = event => {
    let settled = '', pending = '';
    for (let i = 0; i < event.results.length; i++) {
      const chunk = event.results[i][0].transcript;
      if (event.results[i].isFinal) settled += chunk; else pending += chunk;
    }
    input.value = [micBase, (settled + pending).trim()].filter(Boolean).join(' ');
    input.dispatchEvent(new Event('input'));   // keeps autosize and placeholder in step
  };

  r.onerror = event => {
    const why = {
      'not-allowed': 'Microphone access was blocked. Allow it in the address bar, then try again.',
      'service-not-allowed': 'Microphone access was blocked by your browser settings.',
      'no-speech': 'I did not catch anything — try again.',
      'audio-capture': 'No microphone was found.',
    }[event.error];
    // 'aborted' is what a deliberate stop reports; it is not worth a message.
    if (why) line('notice', why);
    stopDictation();
  };

  r.onend = () => { if (recogniser) stopDictation(); };

  recogniser = r;
  try {
    r.start();
  } catch {
    recogniser = null;
    line('notice', 'Could not start the microphone.');
    setMic(false);
  }
};

// ---------------------------------------------------------------- attaching

// Picked files are copied into the agent's workspace, which is the only place
// on disk its file tools can reach. The message then names the paths so it
// knows to open them.
const attached = document.getElementById('attached');
const filePicker = document.getElementById('filePicker');
let attachments = [];

const humanSize = n => n < 1024 ? n + ' B'
  : n < 1024 * 1024 ? (n / 1024).toFixed(0) + ' KB'
  : (n / (1024 * 1024)).toFixed(1) + ' MB';

function paintAttachments() {
  attached.innerHTML = '';
  attachments.forEach((a, index) => {
    const el = document.createElement('div');
    el.className = 'att' + (a.pending ? ' pending' : '') + (a.readable === false ? ' binary' : '');
    el.innerHTML = `<span class="nm"></span><span class="sz"></span>` +
                   (a.pending ? '' : '<button type="button" class="x" aria-label="Remove">&times;</button>');
    el.querySelector('.nm').textContent = a.name;
    el.querySelector('.sz').textContent = a.pending ? 'uploading…'
      : (a.readable === false ? humanSize(a.size_bytes) + ' · not readable as text' : humanSize(a.size_bytes));
    if (a.readable === false) el.title = 'Saved to the workspace, but the agent reads text — it can see the name, not the contents.';
    const x = el.querySelector('.x');
    if (x) x.onclick = () => { attachments.splice(index, 1); paintAttachments(); };
    attached.appendChild(el);
  });
}

document.getElementById('attachBtn').onclick = () => filePicker.click();

filePicker.onchange = async () => {
  const files = [...filePicker.files];
  filePicker.value = '';                       // so the same file can be picked twice
  for (const file of files) {
    const placeholder = {name: file.name, size_bytes: file.size, pending: true};
    attachments.push(placeholder);
    paintAttachments();
    const res = await api('/api/attach', {
      method: 'POST',
      headers: {'X-Filename': encodeURIComponent(file.name)},
      body: file,
    });
    const slot = attachments.indexOf(placeholder);
    if (slot === -1) continue;                 // removed while it was uploading
    if (!res) { attachments.splice(slot, 1); paintAttachments(); line('notice', DEAD); continue; }
    const saved = await res.json().catch(() => ({error: 'upload failed'}));
    if (!res.ok || saved.error) {
      attachments.splice(slot, 1);
      paintAttachments();
      line('notice', file.name + ': ' + esc(saved.error || 'could not be attached (HTTP ' + res.status + ')'));
      continue;
    }
    attachments[slot] = saved;
    paintAttachments();
  }
};

function attachmentNote() {
  const ready = attachments.filter(a => !a.pending);
  if (!ready.length) return '';
  const lines = ready.map(a => '- ' + a.path + (a.readable === false ? ' (binary — not readable as text)' : ''));
  return '\n\n[Files attached to your workspace:\n' + lines.join('\n') +
         '\nUse file_read to open them.]';
}

form.onsubmit = e => {
  e.preventDefault();
  stopDictation();          // the mic must not keep running past the send
  const typed = input.value.trim();
  const ready = attachments.filter(a => !a.pending);
  if (!typed && !ready.length) return;
  if (attachments.some(a => a.pending)) { line('notice', 'Still uploading — try again in a moment.'); return; }

  const shown = typed || 'Take a look at the attached file(s).';
  const paths = ready.map(a => a.path);
  const forModel = shown + attachmentNote();   // before the list is cleared below
  input.value = ''; input.style.height = 'auto'; composer.classList.remove('filled');
  attachments = []; paintAttachments();
  ask(forModel, shown, paths);
};

input.addEventListener('input', () => {
  composer.classList.toggle('filled', input.value.length > 0);
  input.style.height = 'auto';
  input.style.height = Math.min(Math.max(input.scrollHeight, 140), 280) + 'px';
});
input.addEventListener('focus', () => composer.classList.add('filled'));
input.addEventListener('blur', () => composer.classList.toggle('filled', input.value.length > 0));

input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
});

document.getElementById('focusInput').onclick = () => showView('chat');

// The section headers collapse, as the chevrons in the design imply.
document.querySelectorAll('.chev').forEach(btn => {
  btn.onclick = () => btn.closest('.group').classList.toggle('collapsed');
});

document.getElementById('newchat').onclick = async () => {
  const res = await api('/api/reset', {method: 'POST'});
  if (!res || !res.ok) { line('notice', DEAD); return; }  // don't clear what the agent still remembers
  wrap.innerHTML = '';
  wrap.appendChild(hero);
  hero.hidden = false;
};

// -------------------------------------------------------------------- inbox

const mailList = document.getElementById('mailList');
const mailRead = document.getElementById('mailRead');
const mailState = document.getElementById('mailState');
let mailQuery = 'in:inbox';
let mailLoading = false;

// A stable colour per sender, so the same person keeps the same disc. Hue only
// — saturation and lightness are fixed so nothing clashes with the palette.
function senderColor(seed) {
  let hash = 0;
  for (const ch of String(seed || '?')) hash = (hash * 31 + ch.charCodeAt(0)) | 0;
  return `hsl(${Math.abs(hash) % 360} 45% 52%)`;
}

function whenLabel(ms) {
  if (!ms) return '';
  const d = new Date(ms), now = new Date();
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
  if (d.getFullYear() === now.getFullYear()) return d.toLocaleDateString([], {day: 'numeric', month: 'short'});
  return d.toLocaleDateString([], {day: 'numeric', month: 'short', year: 'numeric'});
}

function mailMessage(html) {
  mailState.innerHTML = html;
  mailState.hidden = false;
  mailList.hidden = true;
  mailRead.hidden = true;
}

document.getElementById('mailRefresh').onclick = () => loadInbox();
document.getElementById('mailAll').onclick = () => { mailQuery = 'in:inbox'; loadInbox(); };
document.getElementById('mailUnread').onclick = () => { mailQuery = 'in:inbox is:unread'; loadInbox(); };

async function loadInbox() {
  if (mailLoading) return;
  mailLoading = true;
  mailMessage('Loading your mail…');
  try {
    const res = await api('/api/inbox?limit=25&q=' + encodeURIComponent(mailQuery));
    if (!res) { mailMessage(esc(DEAD)); return; }
    if (!res.ok) { mailMessage('Could not read your mail (HTTP ' + res.status + ').'); return; }
    const data = await res.json();
    if (!data.signed_in) {
      mailMessage('<b>Not signed in to Google.</b><br>Use the account button at the bottom of the sidebar to sign in, then come back.');
      return;
    }
    if (data.error) { mailMessage('<b>Gmail returned an error.</b><br>' + esc(data.error)); return; }
    if (!data.messages || !data.messages.length) {
      mailMessage(mailQuery.includes('is:unread')
        ? '<b>No unread mail.</b><br>Nothing in the inbox is waiting on you.'
        : '<b>Inbox is empty.</b>');
      return;
    }
    renderInbox(data);
  } catch (err) {
    mailMessage(esc(err.message || String(err)));
  } finally {
    mailLoading = false;
  }
}

function renderInbox(data) {
  mailState.hidden = true;
  mailList.hidden = false;
  mailRead.hidden = false;
  mailList.innerHTML = '';

  data.messages.forEach((m, index) => {
    const row = document.createElement('div');
    row.className = 'mrow' + (m.unread ? ' unread' : '');
    row.innerHTML =
      `<span class="pic" style="background:${senderColor(m.from_email || m.from_name)}">${esc((m.from_name || '?').trim().charAt(0).toUpperCase())}</span>` +
      `<span style="min-width:0">
         <span class="who"><i class="dot"></i>${esc(m.from_name)}</span>
         <span class="subj">${esc(m.subject)}</span>
         <span class="snip">${esc(m.snippet)}</span>
       </span>` +
      `<span class="when">${esc(whenLabel(m.timestamp))}</span>`;
    row.onclick = () => {
      mailList.querySelectorAll('.mrow.active').forEach(e => e.classList.remove('active'));
      row.classList.add('active');
      openMessage(m);
    };
    mailList.appendChild(row);
    if (index === 0) { row.classList.add('active'); openMessage(m); }
  });
}

async function openMessage(summary) {
  mailRead.innerHTML = '<div class="mail-state">Opening…</div>';
  const res = await api('/api/message?id=' + encodeURIComponent(summary.message_id));
  if (!res || !res.ok) { mailRead.innerHTML = '<div class="mail-state">' + esc(DEAD) + '</div>'; return; }
  const m = await res.json();
  if (m.error) { mailRead.innerHTML = '<div class="mail-state">' + esc(m.error) + '</div>'; return; }

  const who = m.from_name || m.from_email || '(unknown sender)';
  const attachments = (m.attachments || []).length
    ? `<div class="mail-attach"><b>${m.attachments.length} attachment(s):</b> ` +
      m.attachments.map(a => esc(a.filename)).join(', ') + '</div>'
    : '';
  mailRead.innerHTML =
    `<h2>${esc(m.subject || '(no subject)')}</h2>
     <div class="mail-meta">
       <span class="pic" style="background:${senderColor(m.from_email || who)}">${esc(who.trim().charAt(0).toUpperCase())}</span>
       <span class="lines">
         <b>${esc(who)}${m.from_email && m.from_email !== who ? ' <span style="font-weight:400;color:var(--faint)">&lt;' + esc(m.from_email) + '&gt;</span>' : ''}</b>
         <span>to ${esc(m.to || 'me')} · ${esc(m.date || '')}</span>
       </span>
     </div>
     <div class="mail-body">${esc(m.body || '(no text content)')}</div>
     ${attachments}
     <a class="mail-open" target="_blank" rel="noopener"
        href="https://mail.google.com/mail/u/0/#all/${encodeURIComponent(m.message_id)}">
       <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M15 4h5v5M20 4l-9 9"/><path d="M19 13v6a2 2 0 01-2 2H6a2 2 0 01-2-2V7a2 2 0 012-2h6"/></svg>
       Open in Gmail
     </a>`;
  mailRead.scrollTop = 0;
}

// ---------------------------------------------------------------- reminders

// These are the app's own reminders, not Google's — everything here talks to
// /api/reminders, which is backed by a local database and answers whether or
// not anyone is signed in to Google.
// Declared here rather than with the calendar's elements below: this block runs
// first, and `const` gives no hoisting to lean on.
const remView = document.getElementById('remView');
const remActions = document.getElementById('remActions');
const navReminders = document.getElementById('navReminders');
const remList = document.getElementById('remList');
const remState = document.getElementById('remState');
const remSearch = document.getElementById('remSearch');
const remSchedule = document.getElementById('remSchedule');
const remBadge = document.getElementById('remBadge');
const REM_DAY_MS = 86400000;
let remScope = 'upcoming';
let remQuery = '';
let remLoading = false;

const pad2 = n => String(n).padStart(2, '0');
const localStamp = d => `${d.getFullYear()}-${pad2(d.getMonth()+1)}-${pad2(d.getDate())}T${pad2(d.getHours())}:${pad2(d.getMinutes())}`;

function whenLabel(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso || '';
  const now = new Date();
  const time = d.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
  const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const days = Math.round((new Date(d.getFullYear(), d.getMonth(), d.getDate()) - midnight) / REM_DAY_MS);
  if (days === 0) return `Today ${time}`;
  if (days === 1) return `Tomorrow ${time}`;
  if (days === -1) return `Yesterday ${time}`;
  const date = d.toLocaleDateString([], {weekday: 'short', day: 'numeric', month: 'short'});
  return `${date}, ${time}`;
}

function remGroup(reminder) {
  if (reminder.status === 'completed') return 'Completed';
  const d = new Date(reminder.due);
  const now = new Date();
  if (d < now) return 'Overdue';
  const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const days = Math.round((new Date(d.getFullYear(), d.getMonth(), d.getDate()) - midnight) / REM_DAY_MS);
  if (days === 0) return 'Today';
  if (days === 1) return 'Tomorrow';
  return 'Upcoming';
}

async function remAction(payload) {
  const res = await api('/api/reminders', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  if (!res) { remMessage(`<b>${esc(DEAD)}</b>`); return null; }
  const data = await res.json().catch(() => null);
  if (!data || data.error) {
    remMessage(`<b>That did not work.</b><br>${esc((data && data.error) || 'unknown error')}`);
    return null;
  }
  return data.result;
}

function remMessage(html) {
  remState.innerHTML = html;
  remState.hidden = false;
  remList.hidden = true;
}

async function loadReminders(options) {
  const opts = options || {};
  if (remLoading) return;
  remLoading = true;
  if (!remList.childElementCount) remMessage('Loading your reminders…');

  const params = new URLSearchParams({scope: remScope});
  if (remQuery) params.set('q', remQuery);
  if (opts.schedule) params.set('schedule', '1');

  const res = await api('/api/reminders?' + params.toString());
  remLoading = false;
  if (!res) { remMessage(`<b>${esc(DEAD)}</b>`); return; }
  const data = await res.json().catch(() => null);
  if (!data || data.error) {
    remMessage(`<b>Could not read your reminders.</b><br>${esc((data && data.error) || '')}`);
    return;
  }

  if (data.counts) {
    const badge = data.counts.today || 0;
    remBadge.textContent = badge > 99 ? '99+' : String(badge);
    remBadge.hidden = badge === 0;
  }
  if (data.schedule) paintSchedule(data.schedule);
  paintReminders(data.reminders || []);
}

function paintReminders(items) {
  remList.innerHTML = '';
  if (!items.length) {
    const blank = {
      today: 'Nothing due today.',
      upcoming: 'No reminders yet.',
      completed: 'Nothing completed yet.',
      overdue: 'Nothing overdue — all caught up.',
      all: 'No reminders yet.',
    }[remScope] || 'Nothing here.';
    remMessage(remQuery
      ? `<b>No reminders match “${esc(remQuery)}”.</b>`
      : `<b>${esc(blank)}</b><br>Use <b>New reminder</b>, or just ask in the chat — “remind me tomorrow at 5pm to study”.`);
    return;
  }

  remState.hidden = true;
  remList.hidden = false;
  let heading = null;
  for (const reminder of items) {
    const group = remGroup(reminder);
    if (group !== heading) {
      heading = group;
      const label = document.createElement('div');
      label.className = 'rem-group';
      label.textContent = group;
      remList.appendChild(label);
    }
    remList.appendChild(remRow(reminder));
  }
}

function remRow(reminder) {
  const done = reminder.status === 'completed';
  const row = document.createElement('div');
  row.className = 'rrow' + (done ? ' done' : '') + (reminder.overdue && !done ? ' late' : '');
  row.innerHTML =
    '<button class="rtick" title="' + (done ? 'Mark as not done' : 'Mark as done') + '">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.4"><path d="M5 13l4 4 10-10"/></svg>' +
    '</button>' +
    '<div class="what"><div class="name"></div><div class="sub"></div></div>' +
    '<div class="when"></div>' +
    '<div class="rem-acts">' +
      '<button class="snooze">Snooze</button>' +
      '<button class="edit">Edit</button>' +
      '<button class="danger del">Delete</button>' +
    '</div>';

  row.querySelector('.name').textContent = reminder.title;
  const name = row.querySelector('.name');
  if (reminder.recurs) {
    const chip = document.createElement('span');
    chip.className = 'chip-tag chip-rep';
    chip.textContent = reminder.recurs;
    name.appendChild(chip);
  }
  for (const tag of reminder.tags || []) {
    const chip = document.createElement('span');
    chip.className = 'chip-tag';
    chip.textContent = tag;
    name.appendChild(chip);
  }

  const sub = row.querySelector('.sub');
  const bits = [];
  if (reminder.notes) bits.push(reminder.notes);
  if (reminder.snoozed) bits.push('snoozed');
  sub.textContent = bits.join(' · ');
  if (!bits.length) sub.remove();

  row.querySelector('.when').textContent = whenLabel(reminder.due);

  row.querySelector('.rtick').onclick = async () => {
    await remAction({action: done ? 'reopen' : 'complete', id: reminder.id});
    loadReminders();
  };
  row.querySelector('.snooze').onclick = () => snoozeDialog(reminder);
  row.querySelector('.edit').onclick = () => reminderDialog(reminder);
  row.querySelector('.del').onclick = () => deleteDialog(reminder);
  // The actions only appear on hover, so the whole row has to be a way in.
  row.querySelector('.what').onclick = () => reminderDialog(reminder);
  return row;
}

// ------------------------------------------------------------ the dialogs

function modal(html) {
  const bg = document.createElement('div');
  bg.className = 'modal-bg';
  bg.innerHTML = `<div class="modal">${html}</div>`;
  document.body.appendChild(bg);
  bg.onclick = e => { if (e.target === bg) bg.remove(); };
  return bg;
}

const REPEATS = [
  ['none', 'Never'], ['daily', 'Every day'], ['weekdays', 'Every weekday'],
  ['weekly', 'Every week'], ['monthly', 'Every month'], ['yearly', 'Every year'],
];

function reminderDialog(existing) {
  const editing = !!existing;
  const when = editing ? existing.due : localStamp(new Date(Date.now() + 60 * 60 * 1000));
  const options = REPEATS.map(([value, label]) =>
    `<option value="${value}">${label}</option>`).join('');

  const bg = modal(`
    <h2>${editing ? 'Edit reminder' : 'New reminder'}</h2>
    <p class="lead">It will notify you at the time you set${editing ? '' : ', even if this app is closed'}.</p>
    <div class="rem-form">
      <label>Title<input id="rfTitle" maxlength="200" placeholder="Study trigonometry"></label>
      <div class="pair">
        <label>When<input id="rfWhen" type="datetime-local"></label>
        <label>Repeat<select id="rfRepeat">${options}</select></label>
      </div>
      <label>Tags <input id="rfTags" placeholder="school, maths"></label>
      <label>Notes<textarea id="rfNotes" maxlength="2000" placeholder="Optional"></textarea></label>
      <div class="rem-err" id="rfErr"></div>
    </div>
    <div class="row"><button class="deny">Cancel</button><button class="allow">${editing ? 'Save' : 'Create'}</button></div>`);

  const field = id => bg.querySelector('#' + id);
  field('rfTitle').value = editing ? existing.title : '';
  field('rfWhen').value = when;
  field('rfRepeat').value = editing ? (REPEATS.some(r => r[0] === existing.recurrence) ? existing.recurrence : 'none') : 'none';
  field('rfTags').value = editing ? (existing.tags || []).join(', ') : '';
  field('rfNotes').value = editing ? (existing.notes || '') : '';
  field('rfTitle').focus();

  bg.querySelector('.deny').onclick = () => bg.remove();
  bg.querySelector('.allow').onclick = async () => {
    const payload = {
      action: editing ? 'update' : 'create',
      id: editing ? existing.id : undefined,
      title: field('rfTitle').value.trim(),
      due: field('rfWhen').value,
      recurrence: field('rfRepeat').value,
      tags: field('rfTags').value,
      notes: field('rfNotes').value,
    };
    if (!payload.title) { field('rfErr').textContent = 'Give it a title.'; return; }
    if (!payload.due) { field('rfErr').textContent = 'Pick a date and time.'; return; }

    const res = await api('/api/reminders', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = res ? await res.json().catch(() => null) : null;
    if (!data || data.error) {
      field('rfErr').textContent = (data && data.error) || DEAD;
      return;
    }
    bg.remove();
    loadReminders();
  };
}

function snoozeDialog(reminder) {
  const choices = [['10 minutes', 10], ['1 hour', 60], ['3 hours', 180], ['Tomorrow morning', null]];
  const buttons = choices.map(([label], i) =>
    `<button class="deny" data-i="${i}">${label}</button>`).join('');
  const bg = modal(`
    <h2>Snooze</h2>
    <p class="lead">${esc(reminder.title)} — currently ${esc(whenLabel(reminder.due))}.</p>
    <div class="row" style="flex-wrap:wrap">${buttons}</div>`);

  bg.querySelectorAll('button[data-i]').forEach(button => {
    button.onclick = async () => {
      const [, minutes] = choices[Number(button.dataset.i)];
      let mins = minutes;
      if (mins === null) {
        // 9am tomorrow, expressed as minutes from now so the server needs no
        // opinion about what "morning" means.
        const morning = new Date();
        morning.setDate(morning.getDate() + 1);
        morning.setHours(9, 0, 0, 0);
        mins = Math.max(1, Math.round((morning - new Date()) / 60000));
      }
      bg.remove();
      await remAction({action: 'snooze', id: reminder.id, minutes: mins});
      loadReminders();
    };
  });
}

function deleteDialog(reminder) {
  const repeats = reminder.recurs ? ` This deletes the whole repeating series (${esc(reminder.recurs)}).` : '';
  const bg = modal(`
    <h2>Delete this reminder?</h2>
    <p class="lead">${esc(reminder.title)} — ${esc(whenLabel(reminder.due))}.${repeats} This cannot be undone.</p>
    <div class="row"><button class="deny">Keep it</button><button class="allow">Delete</button></div>`);
  bg.querySelector('.deny').onclick = () => bg.remove();
  bg.querySelector('.allow').onclick = async () => {
    bg.remove();
    await remAction({action: 'delete', id: reminder.id});
    loadReminders();
  };
}

// ------------------------------------------------- notifications while shut

function paintSchedule(schedule) {
  remSchedule.innerHTML = '';
  if (!schedule.supported) {
    remSchedule.textContent = 'Scheduled notifications need Windows.';
    return;
  }
  const label = document.createElement('span');
  const button = document.createElement('button');
  if (schedule.installed) {
    label.innerHTML = '<span class="live">Notifications on</span> — reminders ring even when Lumen is closed.';
    button.textContent = 'Turn off';
    button.onclick = async () => {
      button.disabled = true;
      await remAction({action: 'disable_notifications'});
      loadReminders({schedule: true});
    };
  } else {
    label.textContent = 'Reminders only ring while Lumen is open.';
    button.textContent = 'Turn on notifications';
    button.onclick = async () => {
      button.disabled = true;
      button.textContent = 'Setting up…';
      const result = await remAction({action: 'enable_notifications'});
      if (result && result.error) remMessage(`<b>Windows would not schedule it.</b><br>${esc(result.error)}`);
      loadReminders({schedule: true});
    };
  }
  remSchedule.appendChild(label);
  remSchedule.appendChild(button);
}

// A reminder that rings while the page is open should be visible here too, not
// only in Action Center. The server has already claimed and announced it; this
// only mirrors what happened, so nothing fires twice.
const remSeen = new Set();

function remToast(item) {
  const box = document.createElement('div');
  box.className = 'rem-toast';
  box.innerHTML = '<button class="close" aria-label="Dismiss">&times;</button><b></b><span></span>';
  box.querySelector('b').textContent = item.title || 'Reminder';
  box.querySelector('span').textContent = whenLabel(item.due_local);
  box.querySelector('.close').onclick = () => box.remove();
  document.body.appendChild(box);
  setTimeout(() => box.remove(), 30000);
  if (window.Notification && Notification.permission === 'granted') {
    try { new Notification(item.title || 'Reminder', {body: whenLabel(item.due_local)}); } catch {}
  }
}

async function pollReminders() {
  const res = await api('/api/reminders?scope=today&since=1');
  if (!res) return;
  const data = await res.json().catch(() => null);
  if (!data) return;
  if (data.counts) {
    const badge = data.counts.today || 0;
    remBadge.textContent = badge > 99 ? '99+' : String(badge);
    remBadge.hidden = badge === 0;
  }
  for (const item of data.recently_fired || []) {
    const key = item.reminder_id + '@' + item.at_utc;
    if (remSeen.has(key)) continue;
    remSeen.add(key);
    remToast(item);
    if (remView.classList.contains('on')) loadReminders();
  }
}

document.getElementById('remNew').onclick = () => reminderDialog(null);
for (const [id, scope] of [['remToday', 'today'], ['remUpcoming', 'upcoming'], ['remDone', 'completed']]) {
  document.getElementById(id).onclick = () => { remScope = scope; loadReminders(); };
}
let remTypingTimer = null;
remSearch.oninput = () => {
  clearTimeout(remTypingTimer);
  remTypingTimer = setTimeout(() => { remQuery = remSearch.value.trim(); loadReminders(); }, 250);
};

// Browser notifications are a nicety on top of the Windows ones; ask only when
// the user opens the Reminders page, never on load.
navReminders.addEventListener('click', () => {
  if (window.Notification && Notification.permission === 'default') {
    Notification.requestPermission().catch(() => {});
  }
}, {once: true});

pollReminders();
setInterval(pollReminders, 30000);

// ----------------------------------------------------------------- calendar

const HOUR_PX = 52;                       // must match .hr / .slot height in CSS
const DAY_MS = 86400000;
const calView = document.getElementById('calView');
const calGrid = document.getElementById('calGrid');
const calState = document.getElementById('calState');
const calLegend = document.getElementById('calLegend');
const chatBody = document.getElementById('log');
const composerZone = document.querySelector('.composer-zone');
const chatActions = document.getElementById('chatActions');
const calActions = document.getElementById('calActions');
const mailView = document.getElementById('mailView');
const mailActions = document.getElementById('mailActions');
const navInbox = document.getElementById('navInbox');
const navDashboard = document.getElementById('navDashboard');
const title = document.getElementById('title');
const crumbLeaf = document.getElementById('crumbLeaf');
const navCalendar = document.getElementById('navCalendar');
let calAnchor = startOfWeek(new Date());   // Monday of the week on screen
let calLoading = false;

function startOfWeek(d) {
  const x = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  x.setDate(x.getDate() - ((x.getDay() + 6) % 7));   // Monday
  return x;
}
const ymd = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
const sameDay = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();

// An all-day event carries a bare YYYY-MM-DD. Passing that to Date() parses it
// as UTC midnight, which lands on the previous day for anyone west of London.
function parseStamp(value, allDay) {
  if (!value) return null;
  if (allDay || value.length === 10) {
    const [y, m, d] = value.slice(0, 10).split('-').map(Number);
    return new Date(y, m - 1, d);
  }
  return new Date(value);
}

function tint(hex, alpha) {
  const h = (hex || '').replace('#', '');
  if (h.length !== 6) return 'rgba(75,75,216,' + alpha + ')';
  const n = parseInt(h, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

const timeLabel = d => d.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});

/* ------------------------------------------------------------- providers ---
   Two renderings of one state: the gate (#setup, shown when nothing is
   connected) and the settings view (#provView). Both read /api/providers,
   which never returns a key — only the masked tail the server chose to show.
   The key input is write-only: it is posted, then cleared, and nothing ever
   puts it back.                                                            */

const provView = document.getElementById('provView');
const navProviders = document.getElementById('navProviders');
const setup = document.getElementById('setup');
const setupList = document.getElementById('setupList');
const setupForm = document.getElementById('setupForm');
const setupKey = document.getElementById('setupKey');
const setupGo = document.getElementById('setupGo');
const setupMsg = document.getElementById('setupMsg');
const setupHint = document.getElementById('setupHint');
const setupStore = document.getElementById('setupStore');
const setupTitle = document.getElementById('setupTitle');
const setupLede = document.getElementById('setupLede');
const setupPeek = document.getElementById('setupPeek');
const setupUse = document.getElementById('setupUse');
const setupKeyWrap = document.getElementById('setupKeyWrap');
const setupReplace = document.getElementById('setupReplace');
const setupSkip = document.getElementById('setupSkip');
const provBadge = document.getElementById('provBadge');
const keylock = document.getElementById('keylock');
const keylockWhy = document.getElementById('keylockWhy');

let provState = null;
let setupPick = null;      // which provider the gate is collecting a key for
let setupSkipped = false;  // the user chose to look around without a key
let modelCache = {};

async function providerAction(payload) {
  const res = await api('/api/providers', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  if (!res) return null;
  const data = await res.json().catch(() => null);
  if (data && data.state) { provState = data.state; paintProviders(); }
  return data;
}

async function loadProviders() {
  const res = await api('/api/providers');
  if (!res) return;
  provState = await res.json().catch(() => null);
  paintProviders();
}

function providerRow(p, onClick) {
  const el = document.createElement('button');
  el.type = 'button';
  el.className = 'prov' + (p.connected ? ' on' : '');
  el.setAttribute('aria-current', setupPick === p.id ? 'true' : 'false');
  const status = p.connected
    ? (p.source === 'environment' ? 'Connected from ' + esc(p.env_var) : 'Connected')
    : 'Not connected';
  el.innerHTML =
    '<span class="dot">' + (p.connected ? '&#10003;' : '') + '</span>' +
    '<span class="body"><span class="name">' + esc(p.name) + '</span>' +
    '<span class="sub' + (p.connected ? ' key' : '') + '">' +
    (p.connected ? esc(p.masked_key) : status) + '</span></span>' +
    (p.active ? '<span class="tag">Active</span>' : '') +
    (p.connected && p.source === 'environment' ? '<span class="tag env">env</span>' : '');
  el.onclick = () => onClick(p);
  return el;
}

function storeNote(state) {
  const store = state.keystore || {};
  if (!store.available) return {text: store.detail || 'No secure credential store found.', warn: true};
  if (!store.secure) {
    return {text: 'Keys are stored in ' + store.name + ' — ' + store.detail +
                  '. Anyone who can read your files can read them.', warn: true};
  }
  return {text: 'Your keys are stored in ' + store.name + ' (' + store.detail +
                "). They are never written into this app's files and are sent " +
                'only to the provider they belong to.', warn: false};
}

/* --- the gate ------------------------------------------------------------ */

// Two different asks wear the same form. A provider with no key needs one
// pasted; a provider whose key is already on this machine — imported from an
// environment variable, or left behind by an earlier run — needs nothing but
// the word "yes", so that is the button it gets. Which account gets billed is
// still the user's decision, which is why it is never made for them.
function pickForSetup(p, mode) {
  setupPick = p.id;
  setupForm.classList.add('open');
  setupKey.value = '';
  setupKey.placeholder = p.key_hint || '';
  setupKey.type = 'password';
  setupPeek.textContent = 'Show';
  setupMsg.textContent = '';
  setupMsg.className = 'key-msg';
  document.getElementById('setupKeyLabel').textContent = p.name + ' API key';
  setupGo.textContent = p.connected ? 'Save this key' : 'Connect ' + p.name;
  setupHint.innerHTML =
    'Create a key at <a href="' + esc(p.console_url) + '" target="_blank" rel="noopener">' +
    esc(p.console_url) + '</a><br>' + esc(p.billing_note);

  const holdsKey = p.connected && mode !== 'replace';
  setupUse.hidden = !holdsKey;
  setupReplace.hidden = !holdsKey;
  setupKeyWrap.hidden = holdsKey;
  if (holdsKey) {
    setupUse.textContent = 'Use this ' + p.name + ' key';
    setupReplace.textContent = 'Paste a different ' + p.name + ' key';
  }
  paintProviders();
  if (holdsKey) setupUse.focus(); else setupKey.focus();
}

setupUse.onclick = async () => {
  if (!setupPick) return;
  setupUse.disabled = true;
  setupMsg.textContent = 'Switching over…';
  setupMsg.className = 'key-msg busy';
  const data = await providerAction({action: 'activate', provider: setupPick});
  setupUse.disabled = false;
  if (!data) { setupMsg.textContent = DEAD; setupMsg.className = 'key-msg bad'; return; }
  if (data.ok) { setupMsg.textContent = ''; return; }   // refreshLock closes the gate
  setupMsg.textContent = data.message || 'That key could not be used.';
  setupMsg.className = 'key-msg bad';
};

setupReplace.onclick = () => {
  const picked = (provState.providers || []).find(p => p.id === setupPick);
  if (picked) pickForSetup(picked, 'replace');
};

setupSkip.onclick = () => { setupSkipped = true; closeSetup(); };

async function submitKey(providerId, keyInput, message, button) {
  const key = keyInput.value.trim();
  if (!key) { message.textContent = 'Paste the key first.'; message.className = 'key-msg bad'; return false; }

  button.disabled = true;
  message.textContent = 'Checking with the provider…';
  message.className = 'key-msg busy';
  const data = await providerAction({action: 'add', provider: providerId, key});
  // Cleared whatever happened: a rejected key is still a key, and it has no
  // business sitting in the DOM afterwards.
  keyInput.value = '';
  button.disabled = false;

  if (!data) { message.textContent = DEAD; message.className = 'key-msg bad'; return false; }
  if (data.ok) {
    message.textContent = 'Connected.';
    message.className = 'key-msg good';
    return true;
  }
  message.textContent = data.message || 'That key was not accepted.';
  message.className = 'key-msg bad';
  return false;
}

setupPeek.onclick = () => {
  const shown = setupKey.type === 'text';
  setupKey.type = shown ? 'password' : 'text';
  setupPeek.textContent = shown ? 'Show' : 'Hide';
};
setupGo.onclick = async () => {
  if (!setupPick) return;
  await submitKey(setupPick, setupKey, setupMsg, setupGo);
};
setupKey.addEventListener('keydown', e => { if (e.key === 'Enter') setupGo.click(); });

/* --- the settings view --------------------------------------------------- */

function providerDetail(p) {
  const card = document.createElement('div');
  card.className = 'prov-detail';

  const head = document.createElement('div');
  head.className = 'head';
  head.innerHTML =
    '<span class="dot" style="width:20px;height:20px;border-radius:50%;display:grid;' +
    'place-items:center;font-size:11px;color:#fff;' +
    (p.connected ? 'background:#12855a;' : 'border:1.6px solid #d3d3d3;') + '">' +
    (p.connected ? '&#10003;' : '') + '</span>' +
    '<span class="name">' + esc(p.name) + '</span>' +
    (p.active ? '<span class="tag">Active</span>' : '');
  card.appendChild(head);

  const sub = document.createElement('div');
  sub.className = 'sub';
  sub.style.cssText = 'font-size:12.5px;color:var(--muted);margin-top:6px';
  sub.innerHTML = p.connected
    ? '<span style="font-family:ui-monospace,Menlo,monospace">' + esc(p.masked_key) + '</span>' +
      (p.source === 'environment'
        ? ' &middot; from <b>' + esc(p.env_var) + '</b> (development only)'
        : ' &middot; stored securely on this computer')
    : 'Not connected. ' + esc(p.billing_note);
  card.appendChild(sub);

  if (p.connected) {
    const select = document.createElement('select');
    const models = modelCache[p.id] || p.models;
    const ids = models.map(m => m.id);
    if (!ids.includes(p.model)) models.unshift({id: p.model, label: p.model});
    for (const m of models) {
      const option = document.createElement('option');
      option.value = m.id;
      option.textContent = m.label + (m.note ? ' — ' + m.note : '');
      option.selected = m.id === p.model;
      select.appendChild(option);
    }
    select.onchange = () => providerAction({action: 'model', provider: p.id, model: select.value});
    card.appendChild(select);
  }

  const actions = document.createElement('div');
  actions.className = 'prov-actions';
  const add = (label, cls, fn) => {
    const b = document.createElement('button');
    b.textContent = label;
    if (cls) b.className = cls;
    b.onclick = fn;
    actions.appendChild(b);
    return b;
  };

  if (p.connected && !p.active) {
    add('Use this key', 'primary', () => providerAction({action: 'activate', provider: p.id}));
  }
  // Asked for by name, so the gate opens straight onto the input — never onto
  // the "use the key you already have" shortcut it would otherwise offer.
  add(p.connected ? 'Replace key' : 'Add key', p.connected ? '' : 'primary', () => {
    setupPick = p.id;
    openSetup(p.connected ? 'Replace your ' + p.name + ' key' : 'Add your ' + p.name + ' key');
    pickForSetup(p, 'replace');
  });
  if (p.connected) {
    const test = add('Test', '', async () => {
      test.disabled = true;
      test.textContent = 'Testing…';
      const data = await providerAction({action: 'test', provider: p.id});
      test.disabled = false;
      test.textContent = 'Test';
      note.textContent = !data ? DEAD : (data.ok ? p.name + ' is working.' : data.message);
      note.style.color = data && data.ok ? '#12855a' : 'var(--err)';
    });
    add('Load models', '', async () => {
      note.textContent = 'Reading the model list…';
      note.style.color = 'var(--muted)';
      const data = await providerAction({action: 'models', provider: p.id, live: true});
      if (data && data.ok) {
        modelCache[p.id] = data.models;
        note.textContent = data.models.length + ' models available to this key.';
        paintProviders();
      } else {
        note.textContent = (data && data.message) || DEAD;
        note.style.color = 'var(--err)';
      }
    });
    add('Remove', 'danger', async () => {
      if (!confirm('Remove the ' + p.name + ' key from this computer?')) return;
      const data = await providerAction({action: 'remove', provider: p.id});
      if (data && data.locked) refreshLock();
    });
  }
  card.appendChild(actions);

  const note = document.createElement('div');
  note.style.cssText = 'font-size:12.5px;margin-top:9px;color:var(--muted)';
  card.appendChild(note);
  return card;
}

function paintProviders() {
  if (!provState) return;
  const providers = provState.providers || [];

  // --- the gate's list
  setupList.textContent = '';
  for (const p of providers) setupList.appendChild(providerRow(p, pickForSetup));
  const store = storeNote(provState);
  setupStore.textContent = store.text;
  setupStore.className = 'store-note' + (store.warn ? ' warn' : '');

  // --- the settings view
  if (!provView.classList.contains('on')) { refreshLock(); return; }
  provView.textContent = '';

  const connected = providers.filter(p => p.connected);
  const available = providers.filter(p => !p.connected);

  // Locked, and the user is standing on the screen that fixes it: say so at
  // the top, before the reading matter, with the button that does it.
  if (!provState.unlocked) {
    const alert = document.createElement('div');
    alert.className = 'needs-key';
    alert.innerHTML =
      '<div class="body"><div class="name">Lumen is off until you add an API key</div>' +
      '<div class="sub">' + esc(provState.lock_reason || '') + '</div></div>';
    const go = document.createElement('button');
    go.type = 'button';
    go.textContent = 'Add API key';
    go.onclick = () => openSetup();
    alert.appendChild(go);
    provView.appendChild(alert);
  }

  const banner = document.createElement('div');
  banner.className = 'byok';
  banner.style.margin = '4px 0 6px';
  banner.innerHTML =
    '<b>You bring your own API key.</b> API usage is billed directly by the ' +
    "provider you choose, according to that provider's pricing and policies. " +
    'This app adds nothing to that and takes no cut.';
  provView.appendChild(banner);

  const section = label => {
    const h = document.createElement('h2');
    h.textContent = label;
    provView.appendChild(h);
  };

  if (connected.length) {
    section('Your API keys');
    for (const p of connected) provView.appendChild(providerDetail(p));
  }
  if (available.length) {
    section(connected.length ? 'Add another provider' : 'Choose a provider');
    for (const p of available) provView.appendChild(providerDetail(p));
  }

  section('Key in use');
  const active = connected.find(p => p.active);
  const activeCard = document.createElement('div');
  activeCard.className = 'prov-detail';
  activeCard.innerHTML = active
    ? '<div class="head"><span class="name">' + esc(active.name) + '</span></div>' +
      '<div style="font-size:12.5px;color:var(--muted);margin-top:5px">' +
      esc(active.model) + ' &middot; every request goes from this computer straight to ' +
      esc(active.name) + '</div>'
    : '<div style="font-size:13px;color:var(--warn)">' + esc(provState.lock_reason || '') + '</div>';
  provView.appendChild(activeCard);

  section('Automatic fallback');
  const toggle = document.createElement('label');
  toggle.className = 'prov-toggle';
  const box = document.createElement('input');
  box.type = 'checkbox';
  box.checked = !!provState.fallback_enabled;
  box.onchange = () => providerAction({action: 'fallback', enabled: box.checked});
  const body = document.createElement('div');
  body.className = 'body';
  body.innerHTML =
    '<div class="name">Try another provider when the active one is unavailable</div>' +
    '<div class="sub">Off by default. When on, a request that fails because your ' +
    'provider is down, rate-limited or unreachable is retried with another ' +
    'provider you have connected — <b>which spends money on that account</b>. ' +
    'A rejected API key never triggers a fallback.</div>';
  toggle.appendChild(box);
  toggle.appendChild(body);
  provView.appendChild(toggle);

  section('Where your keys are kept');
  const where = document.createElement('div');
  where.className = 'store-note' + (store.warn ? ' warn' : '');
  where.style.borderTop = '0';
  where.style.paddingTop = '0';
  where.textContent = store.text;
  provView.appendChild(where);

  refreshLock();
}

/* --- the lock ------------------------------------------------------------ */

function openSetup(title) {
  setupSkipped = false;
  setup.hidden = false;
  setupTitle.textContent = title || 'Add your API key';
  setupLede.textContent = provState && provState.lock_reason
    ? provState.lock_reason
    : 'Lumen needs an API key before it can answer anything.';
  // Always a way out, and it says which one it is: nothing is lost by leaving
  // a key alone, and with no key at all there is still an app behind this.
  setupSkip.textContent = provState && provState.unlocked
    ? 'Cancel'
    : 'Not now — look around without the AI';
  if (!setupPick) { setupForm.classList.remove('open'); }
}

function closeSetup() {
  setup.hidden = true;
  setupPick = null;
  setupKey.value = '';
  setupForm.classList.remove('open');
  setupMsg.textContent = '';
}

// Everything that changes when a key is or is not connected, in one place:
// the rail badge, the composer, and the gate. Called after every read or write
// of the provider state, so the three can never disagree.
function paintLock() {
  if (!provState) return;
  const locked = !provState.unlocked;
  provBadge.hidden = !locked;
  keylock.hidden = !locked;
  composer.hidden = locked;
  if (locked) {
    keylockWhy.textContent = provState.lock_reason || 'No AI provider is connected yet.';
    stopDictation();
  }
}

function refreshLock() {
  if (!provState) return;
  paintLock();
  if (provState.unlocked) {
    setupSkipped = false;
    if (!setup.hidden) closeSetup();
  } else if (!setupSkipped && setup.hidden) {
    openSetup();
  }
}

document.getElementById('keylockGo').onclick = () => { showView('providers'); openSetup(); };

// Escape dismisses the gate. With no key that leaves the chat behind the bar in
// the composer rather than a dead box, which is a fair place to leave someone
// who wants to look at their calendar first.
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !setup.hidden) { setupSkipped = true; closeSetup(); }
});

const VIEWS = {
  chat:      {title: 'Ask Lumen'},
  calendar:  {title: 'Multi-calendar'},
  inbox:     {title: 'Inbox'},
  reminders: {title: 'Reminders'},
  providers: {title: 'API Keys'},
};

function showView(name) {
  const view = VIEWS[name] ? name : 'chat';
  if (view !== 'chat') stopDictation();   // the composer is about to be hidden
  calView.classList.toggle('on', view === 'calendar');
  mailView.classList.toggle('on', view === 'inbox');
  remView.classList.toggle('on', view === 'reminders');
  provView.classList.toggle('on', view === 'providers');
  chatBody.hidden = view !== 'chat';
  composerZone.hidden = view !== 'chat';
  chatActions.hidden = view !== 'chat';
  calActions.hidden = view !== 'calendar';
  mailActions.hidden = view !== 'inbox';
  remActions.hidden = view !== 'reminders';
  title.textContent = VIEWS[view].title;
  crumbLeaf.textContent = VIEWS[view].title;
  // Dashboard is home, so it carries the highlight whenever the chat is up —
  // exactly one item in the rail is ever marked active.
  navDashboard.classList.toggle('active', view === 'chat');
  navCalendar.classList.toggle('active', view === 'calendar');
  navInbox.classList.toggle('active', view === 'inbox');
  navReminders.classList.toggle('active', view === 'reminders');
  navProviders.classList.toggle('active', view === 'providers');
  if (view === 'calendar') loadCalendar();
  else if (view === 'inbox') loadInbox();
  else if (view === 'reminders') loadReminders({schedule: true});
  else if (view === 'providers') loadProviders();
  else input.focus();
}

function bindNav(el, view) {
  el.onclick = () => showView(view);
  el.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); showView(view); } };
}
bindNav(navDashboard, 'chat');
bindNav(navCalendar, 'calendar');
bindNav(navInbox, 'inbox');
bindNav(navReminders, 'reminders');
bindNav(navProviders, 'providers');
document.getElementById('back').onclick = () => showView('chat');
showView('chat');   // settles the rail highlight on first paint
document.getElementById('calPrev').onclick = () => { calAnchor = new Date(calAnchor.getTime() - 7 * DAY_MS); loadCalendar(); };
document.getElementById('calNext').onclick = () => { calAnchor = new Date(calAnchor.getTime() + 7 * DAY_MS); loadCalendar(); };
document.getElementById('calToday').onclick = () => { calAnchor = startOfWeek(new Date()); loadCalendar(); };
document.getElementById('calRefresh').onclick = () => loadCalendar();

function calMessage(html) {
  calState.innerHTML = html;
  calState.hidden = false;
  calGrid.hidden = true;
  calLegend.innerHTML = '';
}

async function loadCalendar() {
  if (calLoading) return;
  calLoading = true;
  const days = [...Array(7)].map((_, i) => new Date(calAnchor.getTime() + i * DAY_MS));
  const span = days[0].toLocaleDateString([], {day: 'numeric', month: 'short'}) + ' – ' +
               days[6].toLocaleDateString([], {day: 'numeric', month: 'short', year: 'numeric'});
  // The heading belongs to showView alone. Setting it here too let a slow
  // week load stamp "Multi-calendar" over a view the user had already left.
  calMessage(`Loading ${esc(span)}…`);
  try {
    const res = await api('/api/calendar?start=' + ymd(calAnchor) + '&days=7');
    if (!res) { calMessage(esc(DEAD)); return; }
    if (!res.ok) { calMessage('Could not read the calendar (HTTP ' + res.status + ').'); return; }
    const data = await res.json();
    if (!data.signed_in) {
      calMessage('<b>Not signed in to Google.</b><br>Use the account button at the bottom of the sidebar to sign in, then come back.');
      return;
    }
    if (data.error) { calMessage('<b>Google returned an error.</b><br>' + esc(data.error)); return; }
    renderWeek(days, data, span);
  } catch (err) {
    calMessage(esc(err.message || String(err)));
  } finally {
    calLoading = false;
  }
}

function renderWeek(days, data, span) {
  calState.hidden = true;
  calGrid.hidden = false;

  calLegend.innerHTML = (data.calendars || []).map(c =>
    `<span><i style="background:${esc(c.color)}"></i>${esc(c.summary || c.calendar_id)}</span>`
  ).join('') + `<span style="margin-left:auto;color:var(--faint)">${esc(span)} · ${esc(data.email || '')}</span>`;

  const head = document.getElementById('calHead');
  const today = new Date();
  head.innerHTML = '<div class="cell"></div>' + days.map(d =>
    `<div class="cell${sameDay(d, today) ? ' today' : ''}">
       <div class="dow">${d.toLocaleDateString([], {weekday: 'short'})}</div>
       <div class="dom">${d.getDate()}</div>
     </div>`).join('');

  // Split events into all-day rows and timed blocks, bucketed per day column.
  const timed = days.map(() => []);
  const allDay = days.map(() => []);
  for (const ev of data.events || []) {
    const start = parseStamp(ev.start, ev.all_day);
    const end = parseStamp(ev.end, ev.all_day) || start;
    if (!start) continue;
    for (let i = 0; i < 7; i++) {
      const dayStart = days[i];
      const dayEnd = new Date(dayStart.getTime() + DAY_MS);
      if (ev.all_day) {
        // Google's all-day end date is exclusive.
        if (start < dayEnd && end > dayStart) allDay[i].push(ev);
      } else if (start < dayEnd && end > dayStart) {
        timed[i].push({ev, start, end, dayStart, dayEnd});
      }
    }
  }

  const alldayRow = document.getElementById('calAllDay');
  alldayRow.innerHTML = '<div class="rowlabel">all day</div>' + days.map((d, i) =>
    '<div class="daycol">' + allDay[i].map(ev =>
      `<div class="evt" style="--evt:${esc(ev.color)};--evt-bg:${tint(ev.color, .14)}" title="${esc(ev.summary)}">
         <span class="t">${esc(ev.summary)}</span></div>`).join('') + '</div>').join('');

  const body = document.getElementById('calBody');
  const hours = '<div class="hours">' + [...Array(24)].map((_, h) =>
    `<div class="hr">${h === 0 ? '' : String(h).padStart(2, '0') + ':00'}</div>`).join('') + '</div>';

  body.innerHTML = hours + days.map((d, i) => {
    const slots = [...Array(24)].map(() => '<div class="slot"></div>').join('');
    const weekend = d.getDay() === 0 || d.getDay() === 6 ? ' weekend' : '';
    return `<div class="daycol${weekend}" data-day="${i}">${slots}</div>`;
  }).join('');

  const cols = body.querySelectorAll('.daycol');
  days.forEach((d, i) => {
    const col = cols[i];
    const laid = layout(timed[i]);
    for (const item of laid) {
      const from = Math.max(item.start, item.dayStart);
      const to = Math.min(item.end, item.dayEnd);
      const top = ((from - item.dayStart) / 3600000) * HOUR_PX;
      const height = Math.max(((to - from) / 3600000) * HOUR_PX, 18);
      const width = 100 / item.lanes;
      const el = document.createElement('div');
      el.className = 'evt' + (height < 34 ? ' short' : '');
      el.style.cssText = `top:${top}px;height:${height - 2}px;` +
        `left:calc(${item.lane * width}% + 3px);width:calc(${width}% - 6px);` +
        `--evt:${item.ev.color};--evt-bg:${tint(item.ev.color, .14)}`;
      el.title = item.ev.summary + ' · ' + timeLabel(item.start) + '–' + timeLabel(item.end) +
                 (item.ev.location ? ' · ' + item.ev.location : '');
      el.innerHTML = `<span class="t">${esc(item.ev.summary)}</span>` +
                     `<span class="when">${esc(timeLabel(item.start))}</span>`;
      col.appendChild(el);
    }
    if (sameDay(d, new Date())) {
      const now = new Date();
      const mark = document.createElement('div');
      mark.className = 'nowline';
      mark.style.top = ((now.getHours() + now.getMinutes() / 60) * HOUR_PX) + 'px';
      col.appendChild(mark);
    }
  });

  // Open on the working day rather than at midnight, unless something starts
  // earlier than that.
  const earliest = timed.flat().reduce((min, i) =>
    Math.min(min, i.start.getHours() + i.start.getMinutes() / 60), 8);
  calGrid.scrollTop = Math.max(0, (Math.floor(earliest) - 0.5) * HOUR_PX);
}

// Side-by-side placement for events that overlap in time: walk them in start
// order, put each in the first lane whose last event has already finished.
function layout(items) {
  const sorted = [...items].sort((a, b) => a.start - b.start || b.end - a.end);
  const out = [];
  let cluster = [], clusterEnd = null;

  const flush = () => {
    const lanes = cluster.reduce((n, i) => Math.max(n, i.lane + 1), 0);
    for (const i of cluster) { i.lanes = lanes; out.push(i); }
    cluster = []; clusterEnd = null;
  };

  for (const item of sorted) {
    if (clusterEnd !== null && item.start >= clusterEnd) flush();
    const laneEnds = [];
    for (const other of cluster) laneEnds[other.lane] = Math.max(laneEnds[other.lane] || 0, other.end);
    let lane = 0;
    while (laneEnds[lane] !== undefined && laneEnds[lane] > item.start) lane++;
    item.lane = lane;
    cluster.push(item);
    clusterEnd = Math.max(clusterEnd || 0, item.end);
  }
  flush();
  return out;
}

// ------------------------------------------------------------------ account

const acct = document.getElementById('acct');
const avatar = document.getElementById('avatar');
const switchBtn = document.getElementById('switch');
const signoutBtn = document.getElementById('signout');
const accountBtn = document.getElementById('account');
const acctMenu = document.getElementById('acctMenu');
let polling = null;

function closeMenu() { acctMenu.hidden = true; accountBtn.setAttribute('aria-expanded', 'false'); }
accountBtn.onclick = e => {
  e.stopPropagation();
  acctMenu.hidden = !acctMenu.hidden;
  accountBtn.setAttribute('aria-expanded', String(!acctMenu.hidden));
};
document.addEventListener('click', e => { if (!acctMenu.contains(e.target)) closeMenu(); });

function paintAccount(state) {
  const label = state.signing_in ? 'waiting for Google…' : (state.email || 'not signed in');
  acct.textContent = label;
  accountBtn.title = label;
  avatar.textContent = (state.email || 'L').trim().charAt(0).toUpperCase();
  switchBtn.disabled = state.signing_in;
  signoutBtn.disabled = state.signing_in || !state.signed_in;
  switchBtn.textContent = state.signed_in ? 'Switch account' : 'Sign in';
}

async function refreshAccount() {
  const res = await api('/api/account');
  if (!res || !res.ok) return null;
  try {
    const state = await res.json();
    paintAccount(state);
    return state;
  } catch { return null; }
}

const POLL_MS = 1500;
const POLL_GIVE_UP_MS = 10 * 60 * 1000;  // the Google tab can sit open a while
const POLL_MISSES = 5;                   // ~7s of no answer at all

function stopPolling() { clearInterval(polling); polling = null; }

function watchSignIn(previous) {
  stopPolling();
  const startedAt = Date.now();
  let misses = 0;
  polling = setInterval(async () => {
    const state = await refreshAccount();
    if (!state) {
      // Bounded: if the agent is stopped mid sign-in, an unbounded poll would
      // fire a failed request at the dead port every tick, forever.
      if (++misses >= POLL_MISSES) { stopPolling(); line('notice', DEAD); }
      return;
    }
    misses = 0;
    if (state.signing_in) {
      if (Date.now() - startedAt > POLL_GIVE_UP_MS) {
        stopPolling();
        line('notice', 'Still waiting on the Google tab. Reload this page once you have finished there.');
      }
      return;
    }
    stopPolling();
    if (state.error) {
      line('notice', 'Sign-in failed: ' + esc(state.error));
    } else if (state.email && state.email !== previous) {
      // The server clears history on a real account change, so the transcript
      // on screen no longer matches what the agent knows.
      wrap.innerHTML = '';
      wrap.appendChild(hero);
      line('notice', 'Signed in as ' + esc(state.email) + '. Started a fresh conversation.');
    }
  }, POLL_MS);
}

switchBtn.onclick = async () => {
  closeMenu();
  if (busy) { line('notice', 'Wait for the current answer to finish first.'); return; }
  const before = (await refreshAccount())?.email || null;
  const res = await api('/api/signin', {method: 'POST'});
  if (!res) { line('notice', DEAD); return; }
  if (res.status === 409) {
    const body = await res.json().catch(() => ({}));
    line('notice', body.error || 'Busy — try again in a moment.');
    return;
  }
  line('notice', 'A Google sign-in tab is opening. Pick the account you want, then come back here.');
  await refreshAccount();
  watchSignIn(before);
};

signoutBtn.onclick = async () => {
  closeMenu();
  if (busy) { line('notice', 'Wait for the current answer to finish first.'); return; }
  const res = await api('/api/signout', {method: 'POST'});
  if (!res) { line('notice', DEAD); return; }
  if (res.status === 409) {
    const body = await res.json().catch(() => ({}));
    line('notice', body.error || 'Busy — try again in a moment.');
    return;
  }
  wrap.innerHTML = '';
  wrap.appendChild(hero);
  line('notice', 'Signed out on this computer. Use Sign in to connect a Google account. ' +
                 'To revoke access at Google too, visit myaccount.google.com/permissions');
  await refreshAccount();
};

refreshAccount();
// Before anything else the page can do: this decides whether the gate is up.
loadProviders();

document.getElementById('quit').onclick = async () => {
  closeMenu();
  if (busy && !confirm('Lumen OS is still working. Quit anyway?')) return;
  stopPolling();  // nothing left to poll; the port is about to stop answering
  await api('/api/quit', {method: 'POST'});  // the socket closing is the expected outcome
  const gone = document.createElement('div');
  gone.id = 'gone';
  gone.innerHTML = '<h2>Lumen OS stopped</h2>' +
    '<p>Nothing is running on your PC now. You can close this tab.</p>' +
    '<p>Double-click <b>Workspace Agent</b> on your desktop to start it again.</p>';
  document.body.appendChild(gone);
};
</script>
</body>
</html>
"""
