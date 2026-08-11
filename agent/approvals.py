"""Approval a tool asks for *while* it runs.

The registry's `confirm` flag decides before the handler starts, from the tool
name and its arguments alone. That is enough for `gmail_send_email`, where the
arguments are the whole action — but not for a browser click, where the
consequence lives in the page rather than the call: `browser_click(target="14")`
could be a navigation link or a *Place order* button, and only the handler,
after it has resolved the element, can tell which.

So a handler may ask for itself. The agent loop binds the current front-end's
confirmer around every tool call; a handler that asks outside one — a script, a
test — is denied, the same default as `Callbacks.confirm`.

The binding is per thread, so ask() must be called on the thread running the
tool. Work handed to another thread (the browser session's worker, say) should
come back with what it found and let the handler ask from here.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

# (action, details) -> approved. `action` is shown as the title, `details` as
# the rows beneath it, so both front-ends can render this exactly like the
# registry-driven prompt.
Confirmer = Callable[[str, dict], bool]

_local = threading.local()


@contextmanager
def bind(confirmer: Confirmer | None) -> Iterator[None]:
    """Make `confirmer` the one a running tool reaches, for this thread."""
    previous = getattr(_local, "confirmer", None)
    _local.confirmer = confirmer
    try:
        yield
    finally:
        _local.confirmer = previous


def available() -> bool:
    """True when someone is listening — i.e. asking would reach a human."""
    return getattr(_local, "confirmer", None) is not None


def ask(action: str, details: dict) -> bool:
    """Ask the user to approve something the running tool is about to do."""
    confirmer: Confirmer | None = getattr(_local, "confirmer", None)
    if confirmer is None:
        return False
    return bool(confirmer(action, details))
