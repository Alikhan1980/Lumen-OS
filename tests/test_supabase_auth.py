"""Accounts backed by Supabase directly, with no `server/` in the picture.

This backend exists for one reason: a downloaded copy of the app has no .env
and cannot reach a FastAPI server on the user's own machine, so the gate would
switch itself off and everyone would be ungated. These tests hold that line --
that the bundled project turns accounts *on*, and that the GoTrue request and
response shapes are translated correctly in both directions.

Supabase itself is a `MockTransport`. What is under test is this client's
translation, not theirs.
"""

from __future__ import annotations

import httpx
import pytest

from agent.auth import session as session_module
from agent.auth import supabase as supabase_module
from agent.auth.session import AuthError
from agent.auth.supabase import SupabaseSession

URL = "https://project.supabase.co"
ANON = "anon-key-value"

GOTRUE_USER = {
    "id": "user-1",
    "email": "alex@example.com",
    "email_confirmed_at": "2026-01-01T00:00:00Z",
    "user_metadata": {"name": "Alex"},
}
GOTRUE_SESSION = {
    "access_token": "access-token-value",
    "refresh_token": "refresh-token-value",
    "expires_in": 3600,
    "user": GOTRUE_USER,
}


class FakeStore:
    def __init__(self, **items: str) -> None:
        self.items: dict[str, str] = dict(items)

    def get(self, account: str) -> str | None:
        return self.items.get(account)

    def set(self, account: str, secret: str) -> None:
        self.items[account] = secret

    def delete(self, account: str) -> bool:
        return self.items.pop(account, None) is not None


class FakeGoTrue:
    """Routes on (method, path, grant_type) -- the grant lives in the query."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str, str], tuple[int, dict]] = {}

    def on(self, method: str, path: str, status: int = 200, body: dict | None = None,
           grant: str = ""):
        self.routes[(method, path, grant)] = (status, body or {})
        return self

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        grant = request.url.params.get("grant_type", "")
        route = self.routes.get((request.method, request.url.path, grant))
        if route is None:
            return httpx.Response(404, json={"message": "Not found."})
        status, body = route
        return httpx.Response(status, json=body)


@pytest.fixture
def gotrue(monkeypatch) -> FakeGoTrue:
    server = FakeGoTrue()
    real_client = httpx.Client

    def build(**kwargs):
        return real_client(transport=httpx.MockTransport(server.handle), **kwargs)

    monkeypatch.setattr(supabase_module.httpx, "Client", build)
    return server


@pytest.fixture
def store(monkeypatch) -> FakeStore:
    fake = FakeStore()
    monkeypatch.setattr(session_module, "keystore", lambda: fake)
    return fake


@pytest.fixture
def session(store) -> SupabaseSession:
    return SupabaseSession(URL, ANON)


# ------------------------------------------------------------------ the gate


def test_bundled_supabase_turns_accounts_on(monkeypatch):
    """The whole point: a build with a project in it is gated."""
    monkeypatch.delenv("LUMEN_API_URL", raising=False)
    monkeypatch.setattr("agent.config.supabase_config", lambda: (URL, ANON))
    assert session_module.accounts_enabled() is True


def test_no_backend_at_all_leaves_the_app_ungated(monkeypatch):
    monkeypatch.delenv("LUMEN_API_URL", raising=False)
    monkeypatch.setattr("agent.config.supabase_config", lambda: None)
    assert session_module.accounts_enabled() is False


def test_explicit_server_url_wins_over_the_bundled_project(monkeypatch):
    """A developer pointing at their own server/ must not silently get Supabase."""
    monkeypatch.setenv("LUMEN_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr("agent.config.supabase_config", lambda: (URL, ANON))
    session_module.reset_shared()
    try:
        assert type(session_module.shared()) is session_module.Session
    finally:
        session_module.reset_shared()


def test_shared_picks_supabase_when_no_server_is_configured(monkeypatch):
    monkeypatch.delenv("LUMEN_API_URL", raising=False)
    monkeypatch.setattr("agent.config.supabase_config", lambda: (URL, ANON))
    session_module.reset_shared()
    try:
        assert isinstance(session_module.shared(), SupabaseSession)
    finally:
        session_module.reset_shared()


# ------------------------------------------------------------- request shapes


def test_sign_in_posts_the_password_grant(session, gotrue):
    gotrue.on("POST", "/auth/v1/token", body=GOTRUE_SESSION, grant="password")

    session.sign_in(email="alex@example.com", password="hunter2hunter2")

    assert session.is_signed_in()
    request = gotrue.requests[0]
    assert request.url.params["grant_type"] == "password"
    assert request.headers["apikey"] == ANON


def test_sign_in_maps_gotrue_fields_onto_the_account(session, gotrue):
    gotrue.on("POST", "/auth/v1/token", body=GOTRUE_SESSION, grant="password")

    session.sign_in(email="alex@example.com", password="hunter2hunter2")

    account = session.account
    assert account is not None
    # email_confirmed_at is a timestamp, and the app wants a boolean.
    assert account.email_verified is True
    assert account.display_name == "Alex"


def test_signup_carries_the_name_as_user_metadata(session, gotrue):
    gotrue.on("POST", "/auth/v1/signup", body=GOTRUE_SESSION)

    session.sign_up(name="Alex", email="alex@example.com", password="hunter2hunter2")

    assert gotrue.requests[0].url.path == "/auth/v1/signup"
    assert session.is_signed_in()


def test_signup_awaiting_confirmation_does_not_claim_a_session(session, gotrue):
    """No token means the user must click the link -- do not report signed in."""
    gotrue.on("POST", "/auth/v1/signup", body={"user": GOTRUE_USER})

    result = session.sign_up(
        name="Alex", email="alex@example.com", password="hunter2hunter2"
    )

    assert result["status"] == "pending_verification"
    assert session.is_signed_in() is False


# ------------------------------------------------------------ what comes back


def test_the_refresh_token_goes_to_the_credential_store(session, gotrue, store):
    gotrue.on("POST", "/auth/v1/token", body=GOTRUE_SESSION, grant="password")

    session.sign_in(email="alex@example.com", password="hunter2hunter2")

    assert store.get(session_module.REFRESH_ACCOUNT) == "refresh-token-value"


def test_state_never_carries_a_token(session, gotrue):
    """The page reads this. A token in it would be a token in the browser."""
    gotrue.on("POST", "/auth/v1/token", body=GOTRUE_SESSION, grant="password")

    state = session.sign_in(email="alex@example.com", password="hunter2hunter2")

    assert "access_token" not in state
    assert "refresh_token" not in state
    assert "access-token-value" not in str(state)


def test_a_rejected_password_surfaces_gotrue_s_message(session, gotrue):
    gotrue.on(
        "POST",
        "/auth/v1/token",
        status=400,
        body={"error_code": "invalid_credentials", "message": "Invalid login credentials"},
        grant="password",
    )

    with pytest.raises(AuthError) as caught:
        session.sign_in(email="alex@example.com", password="wrong-password")

    assert caught.value.code == "invalid_credentials"
    assert "Invalid login credentials" in str(caught.value)


def test_an_older_msg_shaped_error_still_reads(session, gotrue):
    """GoTrue has used msg, message and error_description across versions."""
    gotrue.on(
        "POST", "/auth/v1/recover", status=400, body={"msg": "Email rate limit exceeded"}
    )

    with pytest.raises(AuthError) as caught:
        session.forgot_password("alex@example.com")

    assert "rate limit" in str(caught.value)


def test_being_offline_says_so_rather_than_failing_obscurely(session, monkeypatch):
    def refuse(**kwargs):
        raise httpx.ConnectError("nothing is listening")

    monkeypatch.setattr(supabase_module.httpx, "Client", refuse)

    with pytest.raises(AuthError) as caught:
        session.sign_in(email="alex@example.com", password="hunter2hunter2")

    assert caught.value.code == "unreachable"


def test_changing_a_password_checks_the_current_one_first(session, gotrue):
    """GoTrue's update-user takes only the new password and trusts the bearer
    token, so without this an unlocked machine is a password change."""
    gotrue.on("POST", "/auth/v1/token", body=GOTRUE_SESSION, grant="password")
    gotrue.on("PUT", "/auth/v1/user", body=GOTRUE_USER)
    session.sign_in(email="alex@example.com", password="hunter2hunter2")
    gotrue.requests.clear()

    session.change_password(current="hunter2hunter2", new="new-password-here")

    verified_first = gotrue.requests[0]
    assert verified_first.url.params["grant_type"] == "password"
    assert gotrue.requests[1].method == "PUT"


def test_a_wrong_current_password_blocks_the_change(session, gotrue):
    gotrue.on("POST", "/auth/v1/token", body=GOTRUE_SESSION, grant="password")
    session.sign_in(email="alex@example.com", password="hunter2hunter2")
    gotrue.requests.clear()
    # The re-check now fails; PUT /user is deliberately not routed.
    gotrue.on(
        "POST",
        "/auth/v1/token",
        status=400,
        body={"error_code": "invalid_credentials", "message": "Invalid login credentials"},
        grant="password",
    )

    with pytest.raises(AuthError):
        session.change_password(current="not-the-password", new="new-password-here")

    assert all(request.method != "PUT" for request in gotrue.requests)


def test_sign_out_clears_the_stored_token(session, gotrue, store):
    gotrue.on("POST", "/auth/v1/token", body=GOTRUE_SESSION, grant="password")
    gotrue.on("POST", "/auth/v1/logout", status=204)
    session.sign_in(email="alex@example.com", password="hunter2hunter2")

    session.sign_out()

    assert session.is_signed_in() is False
    assert store.get(session_module.REFRESH_ACCOUNT) is None


# ------------------------------------------------------------ bundled config


def test_a_bom_written_config_still_loads(tmp_path, monkeypatch):
    """build.ps1 generates this file with PowerShell, which writes a BOM.

    Reading it as plain utf-8 raises, which `supabase_config` treats as "no
    project configured" -- and a build whose gate is silently off is exactly
    the failure this whole backend exists to prevent.
    """
    from agent import config as config_module

    written = tmp_path / "supabase.json"
    written.write_text(
        '{"url": "https://project.supabase.co", "anon_key": "anon-key-value"}',
        encoding="utf-8-sig",
    )
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    monkeypatch.setattr(config_module, "CREDENTIALS_DIR", tmp_path)
    monkeypatch.setattr(config_module, "bundled_dir", lambda: tmp_path)

    assert config_module.supabase_config() == (
        "https://project.supabase.co",
        "anon-key-value",
    )
