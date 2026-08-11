"""An AI assistant with live access to Google Workspace."""

from __future__ import annotations

import contextlib
import sys

__version__ = "1.2.0"


def _prepare_streams() -> None:
    """Make stdout/stderr usable however the app was launched.

    Two cases to survive:

    * **pythonw.exe** (the no-console launcher) leaves both as ``None``. Rich
      would crash on the first write, so they're pointed at a log file — which
      doubles as the only place a startup failure can be seen.
    * **A packaged or plain console build** inherits the console's ANSI code
      page (cp1252 here), which mangles the UI's box drawing and glyphs.

    Done at import time, before rich builds its Console and samples the encoding.
    """
    if sys.stdout is None or sys.stderr is None:
        try:
            from .config import DATA_DIR

            DATA_DIR.mkdir(parents=True, exist_ok=True)
            # Deliberately not a context manager: this handle becomes stdout for
            # the life of the process and is closed by interpreter shutdown.
            handle = open(  # noqa: SIM115
                DATA_DIR / "agent.log", "a", encoding="utf-8", buffering=1
            )
        except Exception:
            return  # nothing sensible left to do; let it run silently
        if sys.stdout is None:
            sys.stdout = handle
        if sys.stderr is None:
            sys.stderr = handle
        return

    if sys.platform == "win32":
        # Fails when output is redirected, or no console is attached.
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)

    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")


_prepare_streams()
