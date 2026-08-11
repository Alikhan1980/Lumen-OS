"""Security primitives for the API.

Each module here does one thing, and none of them implement cryptography:

* `tokens`   -- verifies Supabase-issued JWTs. The only source of a user id.
* `crypto`   -- AES-256-GCM envelope encryption for stored OAuth tokens.
* `passwords`-- password policy. No hashing; GoTrue owns that.
* `ratelimit`-- shared, Postgres-backed counters for the auth surface.
* `http`     -- response headers, HTTPS enforcement, cookies, CSRF.
"""

from . import crypto, http, passwords, ratelimit, tokens

__all__ = ["crypto", "http", "passwords", "ratelimit", "tokens"]
