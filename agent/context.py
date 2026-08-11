"""Who the agent is acting for, for the duration of one turn.

The desktop build has never needed this: one process, one signed-in Google
account, one `token.json`. The server does. A single process handles turns for
many users at once, and the thing that must never happen is a tool call made
during User A's turn reaching User B's mailbox.

The mechanism is a `ContextVar`. It is bound by the server immediately after a
request's token has been verified, and read by `agent/tools/google_auth.py` when
it builds an API client. Three properties make it the right tool:

* **Task-local.** `contextvars` follow an `await`, and each request runs in its
  own task, so two concurrent turns cannot see each other's binding. A module
  global -- which is what the desktop build effectively uses -- would be shared
  by every request in the process.
* **Not reachable from a tool argument.** Tools receive the arguments the model
  produced. The context is not one of them; a tool asks for it, it is never
  passed in. So there is no string the model can emit that changes whose
  credentials get used. That is what "the AI cannot specify another user's ID"
  means in practice, and it is a property of the plumbing rather than of a
  validation rule somebody has to remember to write.
* **Absent by default.** With nothing bound, `current()` returns None and the
  Google helper falls back to the local token file -- so the desktop app behaves
  exactly as it did before this file existed.

Nothing here holds a refresh token. The server hands in a callable that returns
a short-lived access token, and calls it again when it expires.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


class NoUserBound(RuntimeError):
    """A tool needed a user and there was none.

    Raised rather than falling back to anything. On the server, "I could not
    tell whose request this is" must fail the call -- guessing is how one user's
    turn ends up touching another user's account.
    """


@dataclass(frozen=True)
class AgentContext:
    """The identity and capabilities in force for one turn."""

    # The `sub` of a verified JWT. Never a value from a request body or a tool
    # argument.
    user_id: str

    # Returns a `google.oauth2.credentials.Credentials` for this user, refreshing
    # if needed. A callable rather than a value because a turn can outlive an
    # access token, and because it keeps the refresh token on the server side of
    # the boundary -- this object never holds one.
    google_credentials: Callable[[], Any] | None = None

    # Tool names this user's grants actually cover. Empty means "no restriction"
    # (the desktop case); a populated set is enforced when the tool list is
    # built, so an ungranted tool is not offered to the model at all.
    allowed_tools: frozenset[str] = field(default_factory=frozenset)

    # For the audit log.
    session_id: str | None = None

    def may_use(self, tool_name: str) -> bool:
        return not self.allowed_tools or tool_name in self.allowed_tools


_current: ContextVar[AgentContext | None] = ContextVar("lumen_agent_context", default=None)


@contextmanager
def bind(context: AgentContext) -> Iterator[AgentContext]:
    """Make `context` the active one for this task, and restore on exit.

    The token/reset pair matters: `reset` puts back whatever was there before
    rather than clearing, so nesting is safe and a failed turn cannot leave a
    stale identity bound to a pooled worker.
    """
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)


def current() -> AgentContext | None:
    """The bound context, or None when running as the local desktop user."""
    return _current.get()


def require() -> AgentContext:
    """The bound context, or raise. For code paths that must never guess."""
    context = _current.get()
    if context is None:
        raise NoUserBound(
            "No authenticated user is bound to this turn. A tool tried to reach "
            "user data outside a request context."
        )
    return context


def user_id() -> str | None:
    context = _current.get()
    return context.user_id if context else None
