"""Logging that cannot leak a credential, and the audit trail.

The requirement is a list of things that must never be logged: passwords,
access and refresh tokens, API keys, personal data. A rule like that enforced by
everybody remembering it at every call site fails on the first tired afternoon.
So it is enforced twice here instead:

* **A filter on the log records themselves.** Every message is swept for the
  shapes credentials come in -- JWTs, Google refresh tokens, `Bearer` headers,
  `sk-`/`AIza` API keys, and any `password=`-style assignment -- and the match
  is replaced before the record reaches a handler. It catches the case nobody
  plans for: an exception whose *message* contains the request body.

* **`safe()` for structured fields.** Anything attached to a log line goes
  through an allowlist of keys, not a denylist. A field named
  `provider_refresh_token_v2` is not on the allowlist, so it does not print.

Email addresses are personal data and are logged masked. The user id is a
random UUID and is logged whole -- it is what makes an incident investigable,
and on its own it identifies nobody.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

# Ordered most specific first: a JWT would otherwise be partly eaten by the
# generic long-token rule and stop looking like a JWT.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # JWTs -- Supabase access tokens, Google id tokens.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]+"), "[jwt]"),
    # Google OAuth refresh tokens.
    (re.compile(r"\b1//[A-Za-z0-9_-]{20,}"), "[refresh-token]"),
    # Google API keys.
    (re.compile(r"\bAIza[A-Za-z0-9_-]{30,}"), "[api-key]"),
    # OpenAI / Anthropic style keys.
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"), "[api-key]"),
    # Authorization headers in any casing.
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [redacted]"),
    (re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/=]{8,}"), "Basic [redacted]"),
    # password=..., "secret": "...", token: '...' and friends.
    (
        re.compile(
            r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|"
            r"code_verifier|client_secret)"
            r"(\"?\s*[:=]\s*\"?)([^\s,;&\"'}]{4,})"
        ),
        r"\1\2[redacted]",
    ),
    # Our own envelope format, in case a ciphertext is ever printed.
    (re.compile(r"\bv\d+\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{16,}"), "[sealed]"),
)


def scrub(text: str) -> str:
    """Remove anything credential-shaped from a string."""
    if not text:
        return text
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    """Applies `scrub` to the formatted message and to every argument.

    Returning True always -- this filter edits, it does not drop.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = scrub(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: scrub(value) if isinstance(value, str) else value
                    for key, value in record.args.items()
                }
            else:
                record.args = tuple(
                    scrub(value) if isinstance(value, str) else value
                    for value in record.args
                )
        # Tracebacks carry local variables into the message on some formatters,
        # and an exception raised out of an HTTP client library routinely
        # stringifies the request it failed on.
        if record.exc_text:
            record.exc_text = scrub(record.exc_text)
        return True


# Keys allowed to appear in a structured log line. An allowlist, so a new field
# is invisible until somebody adds it here on purpose.
_SAFE_KEYS = frozenset(
    {
        "action", "attempt", "code", "connection_id", "conversation_id", "count",
        "duration_ms", "event", "ip_prefix", "limit", "method", "outcome", "path",
        "provider", "reason", "remaining", "request_id", "retry_after", "scope",
        "scopes", "status", "table", "tool", "user_id", "window",
    }
)


def mask_email(address: str | None) -> str:
    """`alex@example.com` -> `a***@example.com`.

    Enough to recognise an address you already know, not enough to harvest one.
    """
    if not address or "@" not in address:
        return "[email]"
    local, _, domain = address.partition("@")
    head = local[0] if local else ""
    return f"{head}***@{domain}"


def safe(**fields: Any) -> str:
    """Render structured fields for a log line, dropping anything not allowlisted."""
    kept = {}
    for key, value in fields.items():
        if key not in _SAFE_KEYS:
            continue
        kept[key] = scrub(value)[:200] if isinstance(value, str) else value
    return json.dumps(kept, default=str, sort_keys=True)


_configured = False


def configure(level: str = "INFO") -> None:
    """Set up the `lumen.server` logger tree. Idempotent.

    Its own tree, with propagate off, so an embedding application's handlers
    never receive these records -- those handlers have not been given the
    redacting filter, and a log line is only as safe as the least careful sink
    it reaches.
    """
    global _configured  # noqa: PLW0603 - logging is configured once per process
    if _configured:
        return

    root = logging.getLogger("lumen.server")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)-22s %(message)s")
    )
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)

    # Uvicorn's access log prints full request lines, including query strings --
    # which is where a token ends up if anyone ever puts one in a URL. Filter it
    # too rather than trusting that nobody ever will.
    for name in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).addFilter(RedactingFilter())

    _configured = True


def logger(name: str) -> logging.Logger:
    configure()
    return logging.getLogger("lumen.server").getChild(name)


# ----------------------------------------------------------------- audit trail


async def record_event(
    *,
    event: str,
    user_id: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    detail: dict | None = None,
) -> None:
    """Write a row to public.auth_events.

    Opens its own service connection rather than taking one. Two reasons: half
    these events happen before anybody is authenticated and there is no user
    connection to use, and `auth_events` deliberately grants a user's own role
    no insert -- so an audit write attempted on a scoped connection would fail
    permission checks and vanish into the warning below. Owning the connection
    choice here removes the chance of calling it with the wrong one.

    Best-effort: an audit write that fails must not fail the request that
    triggered it, or a full disk becomes an outage. It is logged instead.

    `detail` is passed through `safe()`'s allowlist, so a caller cannot smuggle
    a token into the audit table by putting it in a dict.
    """
    from .db import service_connection
    from .security.ratelimit import ip_prefix

    payload = json.loads(safe(**(detail or {}))) if detail else {}

    try:
        async with service_connection(reason="audit event") as connection:
            await connection.execute(
                """
                insert into public.auth_events (user_id, event, ip_prefix, user_agent, detail)
                values ($1, $2, $3, $4, $5::jsonb)
                """,
                user_id,
                event[:80],
                ip_prefix(ip) if ip else None,
                (user_agent or "")[:400] or None,
                json.dumps(payload),
            )
    except Exception as exc:
        logger("audit").warning("could not record %s: %s", event, type(exc).__name__)
