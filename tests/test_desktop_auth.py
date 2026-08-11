"""The desktop half: the session that holds the tokens, and the loopback routes.

Everything else in this directory tests the server, which decides who may see
what. Nothing here is load-bearing for that -- if the chat page were bypassed
entirely the server would still refuse. What these tests protect is the other
promise, the one this side owns alone: **the page never holds a token**, and the
refresh token lives in the OS credential store rather than anywhere a browser
can reach.

So the assertions come in two kinds. The lifecycle ones (restore, refresh,
sign-out) check that a credential is kept exactly as long as it is good and
dropped the moment it is not. The rest check what crosses the boundary into the
page -- which is to say, that no token does.

Supabase is not involved: the API is a `MockTransport` and the credential store
is a dictionary, because the behaviour under test is this client's, not theirs.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from agent.auth import routes
from agent.auth import session as session_module
from agent.auth.session import ENDPOINT_ACCOUNT, REFRESH_ACCOUNT, AuthError, Session

API = "http://127.0.0.1:8000"

USER = {"id": "user-1", "email": "alex@example.com", "email_verified": True}
SESSION_PAYLOAD = {
    "access_token": "access-token-value",
    "refresh_token": "refresh-token-value",
    "expires_in": 3600,
    "user": USER,
}
PROFILE_PAYLOAD = {"user": USER, "profile": {"display_name": "Alex"}, "onboarded": True}


# ----------------------------------------------------------------- the doubles


class FakeStore:
    """The OS credential store, as a dictionary that counts its writes."""

    def __init__(self, **items: str) -> None:
        self.items: dict[str, str] = dict(items)
        self.deleted: list[str] = []

    def get(self, account: str) -> str | None:
        return self.items.get(account)

    def set(self, account: str, secret: str) -> None:
        self.items[account] = secret

    def delete(self, account: str) -> bool:
        self.deleted.append(account)
        return self.items.pop(account, None) is not None


class FakeApi:
    """A scripted API server. Unrouted paths 404 rather than being invented."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], tuple[int, dict]] = {}
        self.offline = False

    def on(self, method: str, path: str, status: int = 200, body: dict | None = None):
        self.routes[(method, path)] = (status, body or {})
        return self

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.offline:
            raise httpx.ConnectError("nothing is listening", request=request)
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(
                404, json={"error": {"code": "not_found", "message": "Not found."}}
            )
        status, body = route
        return httpx.Response(status, json=body)


@pytest.fixture
def api(monkeypatch) -> FakeApi:
    """Point every request the session makes at a scripted server."""
    server = FakeApi()
    real_client = httpx.Client

    def build(**kwargs):
        return real_client(transport=httpx.MockTransport(server.handle), **kwargs)

    monkeypatch.setattr(session_module.httpx, "Client", build)
    return server


@pytest.fixture
def store(monkeypatch) -> FakeStore:
    """An empty credential store, unless a test puts something in it."""
    fake = FakeStore()
    monkeypatch.setattr(session_module, "keystore", lambda: fake)
    return fake


def signed_in(api: FakeApi, store: FakeStore) -> Session:
    """A session that has been through a login, as the page would leave it."""
    api.on("POST", "/api/auth/login", 200, SESSION_PAYLOAD)
    api.on("GET", "/api/auth/session", 200, PROFILE_PAYLOAD)
    session = Session(API)
    session.sign_in(email="alex@example.com", password="correct-horse-Battery9")
    return session


# ------------------------------------------------------------------ restoring


def test_a_stored_refresh_token_signs_the_user_back_in(api, store):
    """The reason the token is stored at all.

    Closing the app must not cost a password. Nothing else calls `restore`, so
    without this the credential would be written, kept safely, and never read.
    """
    store.items[REFRESH_ACCOUNT] = "refresh-token-value"
    store.items[ENDPOINT_ACCOUNT] = API
    api.on("POST", "/api/auth/refresh", 200, SESSION_PAYLOAD)
    api.on("GET", "/api/auth/session", 200, PROFILE_PAYLOAD)

    session = Session(API)
    assert session.restore() is True
    assert session.is_signed_in()
    assert session.account.display_name == "Alex"


def test_the_page_asking_for_state_is_what_restores_the_session(api, store):
    """The wiring, not just the method: a page load resumes the session."""
    store.items[REFRESH_ACCOUNT] = "refresh-token-value"
    store.items[ENDPOINT_ACCOUNT] = API
    api.on("POST", "/api/auth/refresh", 200, SESSION_PAYLOAD)
    api.on("GET", "/api/auth/session", 200, PROFILE_PAYLOAD)
    api.on("GET", "/api/auth/config", 200, {"providers": {"password": True}})

    status, payload = routes.handle(Session(API), "/api/auth/state", "GET", {})

    assert status == 200
    assert payload["signed_in"] is True
    assert payload["account"]["email"] == "alex@example.com"


def test_nothing_is_restored_without_a_stored_token(api, store):
    session = Session(API)
    assert session.restore() is False
    assert api.requests == []  # not even an attempt: there is nothing to try


def test_a_token_from_another_deployment_is_not_offered(api, store):
    """Switching `LUMEN_API_URL` must not produce a confusing 'session expired'.

    A token minted elsewhere cannot verify here, so trying it would fail on the
    first launch against a new deployment and look like a broken account.
    """
    store.items[REFRESH_ACCOUNT] = "refresh-token-value"
    store.items[ENDPOINT_ACCOUNT] = "https://lumen.example.com"

    assert Session(API).restore() is False
    assert api.requests == []


def test_restore_is_attempted_once_per_process(api, store):
    """Otherwise every state call waits out a connect timeout while offline."""
    store.items[REFRESH_ACCOUNT] = "refresh-token-value"
    store.items[ENDPOINT_ACCOUNT] = API
    api.offline = True

    session = Session(API)
    assert session.restore() is False
    assert session.restore() is False
    assert len(api.requests) == 1


def test_restoring_an_already_live_session_costs_nothing(api, store):
    session = signed_in(api, store)
    before = len(api.requests)

    assert session.restore() is True
    assert len(api.requests) == before


# ------------------------------------------------------- keeping the credential


def test_a_rejected_refresh_token_is_discarded(api, store):
    """Spent, revoked, or from another project: retrying it every launch is
    a slow way of never signing in."""
    store.items[REFRESH_ACCOUNT] = "refresh-token-value"
    store.items[ENDPOINT_ACCOUNT] = API
    api.on("POST", "/api/auth/refresh", 401, {"error": {"code": "unauthenticated"}})

    assert Session(API).restore() is False
    assert REFRESH_ACCOUNT not in store.items
    assert ENDPOINT_ACCOUNT not in store.items


def test_an_unreachable_server_keeps_the_credential(api, store):
    """A flaky network is not a reason to sign somebody out."""
    store.items[REFRESH_ACCOUNT] = "refresh-token-value"
    store.items[ENDPOINT_ACCOUNT] = API
    api.offline = True

    assert Session(API).restore() is False
    assert store.items[REFRESH_ACCOUNT] == "refresh-token-value"


def test_signing_in_files_the_refresh_token_and_the_endpoint(api, store):
    signed_in(api, store)

    assert store.items[REFRESH_ACCOUNT] == "refresh-token-value"
    assert store.items[ENDPOINT_ACCOUNT] == API


def test_signing_out_clears_the_credential_even_if_the_server_is_gone(api, store):
    """The one machine the user can see must end up signed out regardless."""
    session = signed_in(api, store)
    api.offline = True

    session.sign_out()

    assert not session.is_signed_in()
    assert REFRESH_ACCOUNT not in store.items


def test_a_401_costs_one_refresh_and_one_retry(api, store):
    """A token can age out between the check and the call, so one retry is
    worth it -- but a second would be a loop against a server saying no."""
    session = signed_in(api, store)
    api.on("GET", "/api/account", 401, {"error": {"code": "unauthenticated"}})
    api.on("POST", "/api/auth/refresh", 200, SESSION_PAYLOAD)
    api.requests.clear()

    with pytest.raises(AuthError):
        session.api("GET", "/api/account")

    assert api.paths().count("/api/account") == 2
    assert api.paths().count("/api/auth/refresh") == 1


def test_a_session_the_server_has_forgotten_ends_locally_too(api, store):
    """If the refresh token is dead there is nothing left to be signed in with,
    so the app must stop claiming to be."""
    session = signed_in(api, store)
    api.on("GET", "/api/account", 401, {"error": {"code": "unauthenticated"}})
    api.on("POST", "/api/auth/refresh", 401, {"error": {"code": "unauthenticated"}})

    with pytest.raises(AuthError) as raised:
        session.api("GET", "/api/account")

    assert raised.value.code == "unauthenticated"
    assert not session.is_signed_in()
    assert REFRESH_ACCOUNT not in store.items


# ------------------------------------------------------- what reaches the page


def test_no_token_ever_reaches_the_page(api, store):
    """The central claim of the desktop design, asserted against the bytes.

    Not "no field called token" -- the token's actual value, anywhere in the
    payload the page is handed.
    """
    session = signed_in(api, store)
    api.on("GET", "/api/auth/config", 200, {"providers": {"password": True}})

    for path in ("/api/auth/state", "/api/auth/login", "/api/auth/logout"):
        method = "GET" if path.endswith("state") else "POST"
        _, payload = routes.handle(session, path, method, {"email": "a@b.c", "password": "x"})
        rendered = routes.json_bytes(payload).decode()
        assert "access-token-value" not in rendered
        assert "refresh-token-value" not in rendered


def test_the_page_is_told_when_onboarding_is_unfinished(api, store):
    api.on("POST", "/api/auth/login", 200, SESSION_PAYLOAD)
    api.on(
        "GET",
        "/api/auth/session",
        200,
        {"user": USER, "profile": {"display_name": "Alex"}, "onboarded": False},
    )
    api.on("GET", "/api/auth/config", 200, {"providers": {"password": True}})

    session = Session(API)
    session.sign_in(email="alex@example.com", password="correct-horse-Battery9")
    _, payload = routes.handle(session, "/api/auth/state", "GET", {})

    assert payload["needs_onboarding"] is True


def test_sign_in_still_works_when_the_config_call_fails(api, store):
    """Federated buttons are a nicety; a password field is not."""
    session = signed_in(api, store)
    api.on("GET", "/api/auth/config", 503, {"error": {"code": "service_unavailable"}})

    _, payload = routes.handle(session, "/api/auth/state", "GET", {})

    assert payload["config"] == {"providers": {"password": True}}


# ------------------------------------------------------------------- the routes


def test_error_codes_become_the_matching_status(api, store):
    api.on(
        "POST",
        "/api/auth/login",
        403,
        {"error": {"code": "email_not_verified", "message": "Confirm your address."}},
    )

    status, payload = routes.handle(
        Session(API), "/api/auth/login", "POST", {"email": "a@b.c", "password": "x"}
    )

    assert status == 403
    assert payload["error"]["code"] == "email_not_verified"


def test_a_rate_limit_is_passed_through_as_429(api, store):
    api.on(
        "POST",
        "/api/auth/signup",
        429,
        {"error": {"code": "rate_limited", "message": "Too many attempts."}},
    )

    status, _ = routes.handle(
        Session(API),
        "/api/auth/signup",
        "POST",
        {"name": "Alex", "email": "a@b.c", "password": "x"},
    )

    assert status == 429


def test_an_internal_failure_does_not_leak_a_traceback(api, store, monkeypatch):
    monkeypatch.setattr(
        Session, "sign_in", lambda *args, **kwargs: 1 / 0, raising=True
    )

    status, payload = routes.handle(
        Session(API), "/api/auth/login", "POST", {"email": "a@b.c", "password": "x"}
    )

    assert status == 500
    assert payload["error"]["code"] == "internal_error"
    assert "ZeroDivisionError" not in routes.json_bytes(payload).decode()


def test_changing_a_password_keeps_the_server_wording(api, store):
    """The server is the side that knows other devices were signed out, so it
    is the side whose sentence the user should see."""
    session = signed_in(api, store)
    api.on(
        "POST",
        "/api/auth/password/change",
        200,
        {"status": "changed", "message": "Password updated. Other devices have been signed out."},
    )

    status, payload = routes.handle(
        session,
        "/api/auth/password",
        "POST",
        {"current_password": "old-one", "new_password": "correct-horse-Battery9"},
    )

    assert status == 200
    assert "Other devices" in payload["message"]


def test_a_wrong_current_password_is_a_401_with_a_usable_message(api, store):
    session = signed_in(api, store)
    api.on(
        "POST",
        "/api/auth/password/change",
        401,
        {"error": {"code": "invalid_credentials", "message": "That is not your current password."}},
    )

    status, payload = routes.handle(
        session,
        "/api/auth/password",
        "POST",
        {"current_password": "wrong", "new_password": "correct-horse-Battery9"},
    )

    assert status == 401
    assert payload["error"]["message"] == "That is not your current password."


def test_a_mistyped_password_is_not_mistaken_for_an_expired_session(api, store):
    """Both answer 401, and only one of them is about the token.

    Refreshing on the other spends a credential to learn nothing, and a refresh
    that then fails signs the user out -- telling somebody their session expired
    when what actually happened is that they typed their old password wrong.
    """
    session = signed_in(api, store)
    api.on(
        "POST",
        "/api/auth/password/change",
        401,
        {"error": {"code": "invalid_credentials", "message": "That is not your current password."}},
    )
    api.requests.clear()

    routes.handle(
        session,
        "/api/auth/password",
        "POST",
        {"current_password": "wrong", "new_password": "correct-horse-Battery9"},
    )

    assert "/api/auth/refresh" not in api.paths()
    assert session.is_signed_in()


def test_an_unknown_auth_path_is_a_404(api, store):
    status, _ = routes.handle(Session(API), "/api/auth/nonsense", "POST", {})
    assert status == 404


def test_an_unknown_oauth_provider_is_refused_without_opening_a_browser(api, store, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(routes.webbrowser, "open", opened.append)

    status, payload = routes.handle(
        Session(API), "/api/auth/oauth", "POST", {"provider": "myspace"}
    )

    assert status == 400
    assert payload["error"]["code"] == "unknown_provider"
    assert opened == []


def test_federated_sign_in_opens_the_system_browser(api, store, monkeypatch):
    """Not an embedded view: the user has to be able to see the address bar."""
    opened: list[str] = []
    monkeypatch.setattr(routes.webbrowser, "open", opened.append)
    api.on(
        "GET",
        "/api/auth/oauth/google/start",
        200,
        {"url": "https://accounts.google.com/o/oauth2/auth?x=1"},
    )

    status, _ = routes.handle(Session(API), "/api/auth/oauth", "POST", {"provider": "google"})

    assert status == 200
    assert opened == ["https://accounts.google.com/o/oauth2/auth?x=1"]


# ------------------------------------------------------- the process-wide one


def test_the_shared_session_is_one_per_process(monkeypatch):
    """One person at the keyboard. The server never touches this and derives
    identity per request instead."""
    session_module.reset_shared()
    monkeypatch.setenv("LUMEN_API_URL", "https://lumen.example.com/")
    try:
        first = session_module.shared()
        assert first is session_module.shared()
        assert first.api_url == "https://lumen.example.com"  # trailing slash dropped
    finally:
        session_module.reset_shared()


def test_the_shared_session_defaults_to_the_local_api(monkeypatch):
    session_module.reset_shared()
    monkeypatch.delenv("LUMEN_API_URL", raising=False)
    try:
        assert session_module.shared().api_url == "http://127.0.0.1:8000"
    finally:
        session_module.reset_shared()


# -------------------------------------------------------- the injected screens


def _page_ids() -> set[str]:
    """Every element id in the document the screens are injected into."""
    from agent import web
    from agent.auth import screens

    document = web.PAGE_TEMPLATE + screens.HTML + screens.MENU_ITEMS + screens.VERIFY_BAR
    return set(re.findall(r'id="([A-Za-z0-9_-]+)"', document))


class _BareAgent:
    """Enough of an Agent for the page to render: no key, no provider."""

    provider_id = None
    model = "a-model"


def _render() -> str:
    from agent.web import _page

    return _page(_BareAgent(), "someone@example.com")


def test_with_no_accounts_server_there_is_no_sign_in_screen(monkeypatch):
    """The desktop app on its own, which is how it ships.

    Without this the .exe opens on a sign-in card pointed at a server on the
    user's own machine that nobody has started, and there is no way past it.
    """
    monkeypatch.delenv("LUMEN_API_URL", raising=False)
    page = _render()

    assert 'id="auth"' not in page
    assert 'id="viewLogin"' not in page
    assert 'id="lumenSignout"' not in page
    assert "__AUTH" not in page  # the placeholders collapsed, not left in place
    assert 'id="acctMenu"' in page  # ...and the app itself is untouched


def test_configuring_an_accounts_server_puts_the_screens_in(monkeypatch):
    monkeypatch.setenv("LUMEN_API_URL", "http://127.0.0.1:8000")
    page = _render()

    assert 'id="viewLogin"' in page
    assert 'id="viewPassword"' in page
    assert 'id="lumenPassword"' in page
    assert 'id="verifyBar"' in page
    assert "__AUTH" not in page


def test_the_script_only_reaches_for_elements_that_exist(api, store):
    """The screens are three strings injected into somebody else's document.

    Nothing checks that at load: a `getElementById` for an id that was never
    added returns null, and the failure is a button that quietly does nothing.
    """
    from agent.auth import screens

    wanted = set(re.findall(r"getElementById\('([A-Za-z0-9_-]+)'\)", screens.JS))
    assert wanted <= _page_ids()


def test_every_loopback_route_is_reachable_from_a_screen(api, store):
    """The other direction, which is how `/api/auth/password` sat unreachable:
    the route worked, the session method worked, and no button called it."""
    from agent.auth import screens

    served = set(re.findall(r'path == "(/api/auth/[a-z]+)"', Path(routes.__file__).read_text()))
    called = set(re.findall(r"authCall\('(/api/auth/[a-z]+)'", screens.JS))

    assert served == called


# -------------------------------------------------------------- being offline


def test_a_missing_local_server_says_how_to_start_it(api, store):
    """"Check your connection" sends people to look at their wifi for a problem
    that is on their own machine."""
    api.offline = True

    with pytest.raises(AuthError) as raised:
        Session(API).sign_in(email="a@b.c", password="x")

    assert raised.value.code == "api_unreachable"
    assert "uvicorn" in raised.value.message
    assert "SETUP-AUTH.md" in raised.value.message


def test_a_missing_remote_server_gives_the_ordinary_advice(api, store):
    api.offline = True

    with pytest.raises(AuthError) as raised:
        Session("https://lumen.example.com").sign_in(email="a@b.c", password="x")

    assert "uvicorn" not in raised.value.message
    assert "connection" in raised.value.message


def test_being_offline_leaves_the_page_on_the_sign_in_screen(api, store):
    """Rather than an error screen with nothing to do on it."""
    api.offline = True

    status, payload = routes.handle(Session(API), "/api/auth/state", "GET", {})

    assert status == 200
    assert payload["signed_in"] is False
    assert payload["config"] == {"providers": {"password": True}}
