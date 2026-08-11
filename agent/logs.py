"""A file log of what the agent did outside the user's Google account.

The Google tools are auditable after the fact — a sent message is in Sent, a
shared file records the grant. Web searches and browser actions leave no such
trail, so they leave one here: which query went to which provider, which URL was
opened, what was clicked, what was downloaded and to where.

Written next to the rest of the per-user data, and rotated so it cannot grow
without bound. Never fatal: if the file cannot be opened the agent logs to
nowhere rather than refusing to run.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from .config import DATA_DIR

LOG_PATH = DATA_DIR / "activity.log"
MAX_BYTES = 1_000_000
BACKUPS = 2

def _level() -> int:
    name = (os.getenv("AGENT_LOG_LEVEL") or "INFO").strip().upper()
    return getattr(logging, name, logging.INFO) if name != "OFF" else logging.CRITICAL + 1


def _configure() -> logging.Logger:
    # Its own logger tree rather than the root logger: this must not pick up
    # handlers from an embedding application, or add any to it. The handler
    # list doubles as the "already configured" flag.
    root = logging.getLogger("lumen")
    if root.handlers:
        return root

    root.setLevel(_level())
    root.propagate = False
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            LOG_PATH, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    except OSError:
        handler = logging.NullHandler()
    root.addHandler(handler)
    return root


def logger(name: str) -> logging.Logger:
    """A logger writing to the activity log, e.g. logger("search")."""
    return _configure().getChild(name)
