"""Fixtures.

Two kinds of test live in this directory and they have different needs:

* **Unit and route tests** run with no infrastructure at all. Supabase is
  replaced by `FakeGoTrue` and Postgres by `FakeConnection`, so the auth logic
  -- what is revealed, what is counted, what is refused -- is tested for its own
  behaviour rather than for the upstream's.

* **Isolation tests** (`test_isolation.py`) need a real Postgres, because the
  thing under test *is* Postgres: row-level security is not something a fake can
  demonstrate. They skip when `TEST_DATABASE_URL` is unset, and the skip message
  says how to get one.

The environment is populated before any `server.*` import, because settings are
read at import and cached.
"""

from __future__ import annotations

import base64
import os
import time
import uuid

import pytest

# --- environment, before server imports --------------------------------------

TEST_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()

os.environ.setdefault("LUMEN_ENV", "test")
os.environ.setdefault("SUPABASE_URL", "https://project.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-key-for-tests")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "service-key-for-tests")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-hs256-secret-not-a-real-one")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost:5432/nonexistent")
os.environ.setdefault("LUMEN_TOKEN_ENCRYPTION_KEY", TEST_KEY)
os.environ.setdefault("LUMEN_APP_URL", "http://127.0.0.1:8765")
os.environ.setdefault("LUMEN_PUBLIC_URL", "http://127.0.0.1:8000")
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")

import jwt  # noqa: E402

from server import db as server_db  # noqa: E402
from server.security.tokens import EXPECTED_AUDIENCE  # noqa: E402
from server.settings import settings  # noqa: E402

# --- fake Postgres ------------------------------------------------------------


class FakeConnection:
    """Just enough asyncpg surface for the routes under test.

    Deliberately dumb: it records the statements it was given and answers from a
    scripted table. A test that needs real SQL semantics belongs in the
    Postgres-backed suite instead of being simulated here badly.
    """

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple]] = []
        self.rate_limit_hits: dict[str, int] = {}
        self.rows: dict[str, object] = {}

    def _record(self, query: str, args: tuple) -> None:
        self.statements.append((" ".join(query.split()), args))

    async def fetchval(self, query: str, *args):
        self._record(query, args)
        if "bump_rate_limit" in query:
            bucket = args[0]
            self.rate_limit_hits[bucket] = self.rate_limit_hits.get(bucket, 0) + 1
            return self.rate_limit_hits[bucket]
        if "select 1" in query:
            return 1
        return self.rows.get("fetchval")

    async def fetchrow(self, query: str, *args):
        self._record(query, args)
        return self.rows.get("fetchrow")

    async def fetch(self, query: str, *args):
        self._record(query, args)
        return self.rows.get("fetch", [])

    async def execute(self, query: str, *args):
        self._record(query, args)
        return "OK"

    def saw(self, fragment: str) -> bool:
        return any(fragment in statement for statement, _ in self.statements)


# --- fake GoTrue --------------------------------------------------------------


class FakeGoTrue:
    """A stand-in for Supabase Auth with a scripted user table.

    Behaves the way GoTrue does in the cases the routes branch on: unknown user
    and wrong password are indistinguishable, an unconfirmed account refuses
    login with its own code, and a duplicate signup raises `user_already_exists`.
    """

    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self.sent: list[tuple[str, str]] = []
        self.signed_out: list[tuple[str, str]] = []
        self.fail_next: Exception | None = None

    # helpers for tests
    def add_user(self, email: str, password: str, *, confirmed: bool = True) -> str:
        user_id = str(uuid.uuid4())
        self.users[email.lower()] = {
            "id": user_id,
            "password": password,
            "confirmed": confirmed,
        }
        return user_id

    def _session(self, email: str):
        from server.services.gotrue import Session

        record = self.users[email.lower()]
        return Session(
            access_token=make_token(record["id"], email=email, verified=record["confirmed"]),
            refresh_token=f"refresh-{record['id']}",
            expires_in=3600,
            user_id=record["id"],
            email=email,
            email_verified=record["confirmed"],
        )

    # the GoTrue interface the routes use
    async def sign_up(self, *, email: str, password: str, display_name: str, redirect_to: str):
        from server.services.gotrue import GoTrueError

        if self.fail_next:
            raise self.fail_next
        if email.lower() in self.users:
            raise GoTrueError(422, "user_already_exists", "already registered")
        self.add_user(email, password, confirmed=False)
        self.sent.append(("verify", email))
        return None  # confirmation required, as configured

    async def sign_in(self, *, email: str, password: str):
        from server.services.gotrue import GoTrueError

        record = self.users.get(email.lower())
        if record is None or record["password"] != password:
            raise GoTrueError(400, "invalid_credentials", "bad login")
        if not record["confirmed"]:
            raise GoTrueError(400, "email_not_confirmed", "confirm first")
        return self._session(email)

    async def refresh(self, *, refresh_token: str):
        from server.services.gotrue import GoTrueError

        for email, record in self.users.items():
            if refresh_token == f"refresh-{record['id']}":
                return self._session(email)
        raise GoTrueError(401, "invalid_grant", "no")

    async def sign_out(self, *, access_token: str, scope: str = "local"):
        self.signed_out.append((access_token, scope))

    async def send_reset(self, *, email: str, redirect_to: str):
        from server.services.gotrue import GoTrueError

        if email.lower() not in self.users:
            raise GoTrueError(400, "user_not_found", "no such user")
        self.sent.append(("reset", email))

    async def resend_verification(self, *, email: str, redirect_to: str):
        from server.services.gotrue import GoTrueError

        if email.lower() not in self.users:
            raise GoTrueError(400, "user_not_found", "no such user")
        self.sent.append(("verify", email))

    async def verify_otp(self, *, email: str, token: str, kind: str):
        from server.services.gotrue import GoTrueError

        if token != "good-token" or email.lower() not in self.users:
            raise GoTrueError(401, "invalid_token", "no")
        self.users[email.lower()]["confirmed"] = True
        return self._session(email)

    async def update_password(self, *, access_token: str, password: str):
        for record in self.users.values():
            if access_token.startswith("eyJ") or True:  # any live session
                record["password"] = password
                return

    def authorize_url(self, *, provider: str, redirect_to: str) -> str:
        return f"https://auth.example/authorize?provider={provider}"


# --- tokens -------------------------------------------------------------------


def make_token(
    user_id: str,
    *,
    email: str = "user@example.com",
    verified: bool = True,
    expires_in: int = 3600,
    audience: str = EXPECTED_AUDIENCE,
    issuer: str | None = None,
    role: str = EXPECTED_AUDIENCE,
    secret: str | None = None,
    algorithm: str = "HS256",
) -> str:
    """Mint a token the server will accept, or a deliberately broken one.

    Every deviation a test wants to make -- wrong audience, wrong issuer, past
    expiry, wrong role -- is a keyword here, so the security tests read as a
    list of the things that must be rejected.
    """
    now = int(time.time())
    claims = {
        "sub": user_id,
        "aud": audience,
        "iss": issuer if issuer is not None else settings().jwt_issuer,
        "role": role,
        "email": email,
        "email_verified": verified,
        "session_id": "session-1",
        "iat": now,
        "exp": now + expires_in,
    }
    return jwt.encode(
        claims, secret or settings().supabase_jwt_secret, algorithm=algorithm
    )


# --- fixtures -----------------------------------------------------------------


@pytest.fixture
def fake_db(monkeypatch) -> FakeConnection:
    """Replace the pool so nothing reaches a real Postgres."""
    connection = FakeConnection()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _service(*, reason: str):
        yield connection

    @asynccontextmanager
    async def _service_tx(*, reason: str):
        yield connection

    @asynccontextmanager
    async def _user(principal):
        yield connection

    monkeypatch.setattr(server_db, "service_connection", _service)
    monkeypatch.setattr(server_db, "service_transaction", _service_tx)
    monkeypatch.setattr(server_db, "user_connection", _user)
    monkeypatch.setattr(server_db, "connect", _noop_connect)
    monkeypatch.setattr(server_db, "disconnect", _noop_disconnect)
    return connection


async def _noop_connect():
    return None


async def _noop_disconnect():
    return None


@pytest.fixture
def fake_auth(monkeypatch) -> FakeGoTrue:
    fake = FakeGoTrue()
    from server.services import gotrue as gotrue_module

    monkeypatch.setattr(gotrue_module, "anon", lambda: fake)
    monkeypatch.setattr(gotrue_module, "admin", lambda: fake)
    return fake


@pytest.fixture
def client(fake_db, fake_auth, monkeypatch):
    """A TestClient with startup checks stubbed out.

    The lifespan is replaced rather than skipped so the app is exercised the way
    it is served -- middleware, exception handlers and all -- without needing a
    database or a Supabase project to exist.
    """
    from fastapi.testclient import TestClient

    from server import main

    monkeypatch.setattr(main.agent_runtime, "assert_no_identity_parameters", lambda: None)

    app = main.create_app()
    app.router.lifespan_context = _test_lifespan

    with TestClient(app, base_url="http://testserver") as test_client:
        yield test_client


from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def _test_lifespan(app):
    yield
