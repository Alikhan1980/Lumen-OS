"""Rate limiting for the authentication surface.

Counters live in Postgres, not in this process. An in-memory limiter is worse
than none on a service that will ever run more than one instance: the attacker
does not have to defeat it, only to retry until the load balancer picks a
different worker. `public.bump_rate_limit` does the increment and the read in a
single statement, so two concurrent attempts cannot both observe the same count.

Three axes, because they stop different attacks:

* **per IP** -- one host spraying many accounts (credential stuffing).
* **per account** -- many hosts against one account (targeted brute force).
  This is the one a botnet defeats if you only limit by IP.
* **per account, for email sends** -- reset and verification mail, which costs
  money and can be turned into a way of harassing somebody's inbox.

The account axis is keyed by a *hash* of the address rather than the address.
The limiter table is then not a list of everyone who has ever tried to sign in,
which matters because that table is easier to reach than the auth database.

Failures are fail-closed for authentication: if the database is unreachable, an
auth attempt is refused rather than let through unmetered.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import time
from dataclasses import dataclass

from ..settings import settings

# Salts the account key so the stored bucket cannot be reversed with a
# dictionary of likely addresses. Derived from the token key, which is already
# required and already secret, so this adds no new configuration.
_KEY_SALT = b"lumen.ratelimit.v1"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining: int
    retry_after_seconds: int


def _account_key(value: str) -> str:
    from .crypto import keyring

    ring = keyring()
    secret = ring.keys[ring.current_version]
    digest = hmac.new(secret, _KEY_SALT + value.strip().lower().encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:32]


def client_ip(forwarded_for: str | None, direct: str | None) -> str:
    """The caller's address, as far as it can be trusted.

    `X-Forwarded-For` is only meaningful behind a proxy that sets it, and is
    forgeable by anyone talking to the server directly. Deployment note in
    SETUP-AUTH.md: terminate at a proxy that *overwrites* this header rather
    than appending to a client-supplied one.
    """
    if forwarded_for:
        first = forwarded_for.split(",")[0].strip()
        if first:
            return first
    return (direct or "unknown").strip()


def ip_prefix(address: str) -> str:
    """An address narrowed to its network, for logging and for bucketing.

    /24 for v4 and /48 for v6: enough to recognise a run of attempts from one
    place, not precise enough to be a location record of a particular person.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return "unknown"
    if parsed.version == 4:
        return str(ipaddress.ip_network(f"{parsed}/24", strict=False).network_address) + "/24"
    return str(ipaddress.ip_network(f"{parsed}/48", strict=False).network_address) + "/48"


def _window_start(window_seconds: int) -> int:
    now = int(time.time())
    return now - (now % window_seconds)


async def check(
    connection,
    *,
    action: str,
    ip: str | None = None,
    account: str | None = None,
    limit: int,
    window_seconds: int | None = None,
) -> Decision:
    """Count one attempt and say whether it is allowed.

    `connection` is a service-role connection -- the rate_limits table is not
    reachable by a user's own role, and the caller is usually unauthenticated
    anyway.
    """
    config = settings()
    window = window_seconds or config.rate_window_seconds

    if account:
        bucket = f"{action}:acct:{_account_key(account)}"
    elif ip:
        bucket = f"{action}:ip:{ip_prefix(ip)}"
    else:
        # Nothing to key on. Refuse rather than silently not limiting.
        return Decision(allowed=False, remaining=0, retry_after_seconds=window)

    start = _window_start(window)
    hits = await connection.fetchval(
        "select public.bump_rate_limit($1, to_timestamp($2))", bucket, start
    )
    hits = int(hits or 0)
    remaining = max(0, limit - hits)
    retry_after = max(1, (start + window) - int(time.time()))
    return Decision(
        allowed=hits <= limit,
        remaining=remaining,
        retry_after_seconds=retry_after,
    )


async def check_all(connection, checks: list[dict]) -> Decision:
    """Run several limits and return the first refusal.

    Every check is counted even when an earlier one already refused, so a
    blocked IP still registers against the account it is targeting -- otherwise
    an attacker could keep one axis quiet by keeping another one hot.
    """
    refusal: Decision | None = None
    tightest: Decision | None = None

    for spec in checks:
        decision = await check(connection, **spec)
        if not decision.allowed and refusal is None:
            refusal = decision
        if tightest is None or decision.remaining < tightest.remaining:
            tightest = decision

    return refusal or tightest or Decision(allowed=True, remaining=0, retry_after_seconds=0)
