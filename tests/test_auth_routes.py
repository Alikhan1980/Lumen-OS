"""The signup, login and password flows, end to end through the app.

These go through the real FastAPI stack -- middleware, dependencies, exception
handlers -- with Supabase and Postgres faked underneath. So they test what the
service actually does with an upstream answer, which is where the security
decisions live.

A recurring shape below: asserting that two different situations produce
*identical* responses. That is not laziness, it is the requirement. An attacker
learns whether an address has an account by finding any difference at all
between the two -- status code, body, or wording.
"""

from __future__ import annotations

from .conftest import make_token

GOOD_PASSWORD = "correct-horse-Battery9"


# --------------------------------------------------------------------- signup


def test_signup_creates_an_account(client, fake_auth):
    response = client.post(
        "/api/auth/signup",
        json={"name": "Alex", "email": "alex@example.com", "password": GOOD_PASSWORD},
    )
    assert response.status_code == 202
    assert response.json()["status"] == "pending_verification"
    assert ("verify", "alex@example.com") in fake_auth.sent


def test_duplicate_signup_is_indistinguishable_from_a_new_one(client, fake_auth):
    """The whole point: an attacker cannot use signup to enumerate customers."""
    fake_auth.add_user("taken@example.com", GOOD_PASSWORD)

    fresh = client.post(
        "/api/auth/signup",
        json={"name": "Alex", "email": "brand-new@example.com", "password": GOOD_PASSWORD},
    )
    duplicate = client.post(
        "/api/auth/signup",
        json={"name": "Alex", "email": "taken@example.com", "password": GOOD_PASSWORD},
    )

    assert fresh.status_code == duplicate.status_code == 202
    assert fresh.json() == duplicate.json()


def test_signup_rejects_a_weak_password(client):
    response = client.post(
        "/api/auth/signup",
        json={"name": "Alex", "email": "alex@example.com", "password": "short"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "weak_password"


def test_signup_rejects_an_invalid_email(client):
    response = client.post(
        "/api/auth/signup",
        json={"name": "Alex", "email": "not-an-email", "password": GOOD_PASSWORD},
    )
    assert response.status_code == 422


def test_signup_rejects_missing_fields(client):
    response = client.post("/api/auth/signup", json={"email": "alex@example.com"})
    assert response.status_code == 422


def test_signup_rejects_unknown_fields(client):
    """Mass assignment: a client must not be able to set fields we did not offer."""
    response = client.post(
        "/api/auth/signup",
        json={
            "name": "Alex",
            "email": "alex@example.com",
            "password": GOOD_PASSWORD,
            "role": "admin",
        },
    )
    assert response.status_code == 422


def test_signup_response_never_echoes_the_password(client):
    """Pydantic's default validation error includes the offending input.

    For a signup form that means the password lands in the response body, and
    from there in anything that logs responses. errors.py replaces that handler;
    this is the test that it stayed replaced.
    """
    # Distinctive, and rejected by policy rather than by the schema.
    probe = "qqqqqqqqqqqqqq"
    policy_failure = client.post(
        "/api/auth/signup",
        json={"name": "Alex", "email": "alex@example.com", "password": probe},
    )
    assert policy_failure.status_code == 422
    assert probe not in policy_failure.text

    # And the schema-rejection path, which is the one Pydantic would render.
    schema_failure = client.post(
        "/api/auth/signup",
        json={"name": "Alex", "email": "not-an-email", "password": probe},
    )
    assert schema_failure.status_code == 422
    assert probe not in schema_failure.text


# ---------------------------------------------------------------------- login


def test_login_succeeds_with_correct_credentials(client, fake_auth):
    fake_auth.add_user("alex@example.com", GOOD_PASSWORD, confirmed=True)
    response = client.post(
        "/api/auth/login", json={"email": "alex@example.com", "password": GOOD_PASSWORD}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "signed_in"
    assert body["access_token"]
    # The refresh token is in a cookie, not the body, for a browser client.
    assert "refresh_token" not in body


def test_login_puts_the_refresh_token_in_an_httponly_cookie(client, fake_auth):
    fake_auth.add_user("alex@example.com", GOOD_PASSWORD)
    response = client.post(
        "/api/auth/login", json={"email": "alex@example.com", "password": GOOD_PASSWORD}
    )
    cookie_header = response.headers.get("set-cookie", "")
    assert "lumen_refresh=" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "SameSite=strict" in cookie_header.replace("samesite=strict", "SameSite=strict")


def test_desktop_client_gets_the_refresh_token_in_the_body(client, fake_auth):
    """The packaged app has no cookie jar; it stores this in the OS keychain."""
    fake_auth.add_user("alex@example.com", GOOD_PASSWORD)
    response = client.post(
        "/api/auth/login",
        json={"email": "alex@example.com", "password": GOOD_PASSWORD},
        headers={"X-Lumen-Client": "desktop"},
    )
    assert response.json()["refresh_token"]


def test_wrong_password_and_unknown_account_are_identical(client, fake_auth):
    """Two failures, one answer. Anything else is an enumeration oracle."""
    fake_auth.add_user("real@example.com", GOOD_PASSWORD)

    wrong_password = client.post(
        "/api/auth/login", json={"email": "real@example.com", "password": "Wrong-Password-1"}
    )
    unknown_account = client.post(
        "/api/auth/login", json={"email": "ghost@example.com", "password": "Wrong-Password-1"}
    )

    assert wrong_password.status_code == unknown_account.status_code == 401
    assert wrong_password.json() == unknown_account.json()
    assert wrong_password.json()["error"]["message"] == "Incorrect email or password."


def test_unverified_account_is_told_so(client, fake_auth):
    """Only reachable with the correct password, so it discloses nothing new."""
    fake_auth.add_user("new@example.com", GOOD_PASSWORD, confirmed=False)
    response = client.post(
        "/api/auth/login", json={"email": "new@example.com", "password": GOOD_PASSWORD}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "email_not_verified"


def test_login_is_rate_limited_per_account(client, fake_auth, fake_db):
    """Many hosts against one account -- the case an IP limiter alone misses."""
    fake_auth.add_user("target@example.com", GOOD_PASSWORD)

    statuses = [
        client.post(
            "/api/auth/login",
            json={"email": "target@example.com", "password": "Wrong-Password-1"},
        ).status_code
        for _ in range(12)
    ]
    assert 429 in statuses, "account-level brute force was never refused"

    refused = next(
        code for code in statuses if code == 429
    )
    assert refused == 429


def test_rate_limit_response_carries_retry_after(client, fake_auth):
    fake_auth.add_user("target@example.com", GOOD_PASSWORD)
    last = None
    for _ in range(15):
        last = client.post(
            "/api/auth/login",
            json={"email": "target@example.com", "password": "Wrong-Password-1"},
        )
    assert last.status_code == 429
    assert int(last.headers["Retry-After"]) > 0


# ------------------------------------------------------------- password reset


def test_forgot_password_is_identical_for_known_and_unknown_addresses(client, fake_auth):
    fake_auth.add_user("real@example.com", GOOD_PASSWORD)

    known = client.post("/api/auth/password/forgot", json={"email": "real@example.com"})
    unknown = client.post("/api/auth/password/forgot", json={"email": "ghost@example.com"})

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()


def test_reset_completes_with_a_valid_token(client, fake_auth):
    fake_auth.add_user("alex@example.com", "Old-Password-123", confirmed=True)
    response = client.post(
        "/api/auth/password/reset",
        json={
            "email": "alex@example.com",
            "token": "good-token",
            "password": "brand-New-Password9",
        },
    )
    assert response.status_code == 200
    assert fake_auth.users["alex@example.com"]["password"] == "brand-New-Password9"


def test_reset_signs_other_sessions_out(client, fake_auth):
    """If the reset happened because somebody else knew the password, their
    session must not survive it."""
    fake_auth.add_user("alex@example.com", "Old-Password-123")
    client.post(
        "/api/auth/password/reset",
        json={
            "email": "alex@example.com",
            "token": "good-token",
            "password": "brand-New-Password9",
        },
    )
    assert any(scope == "others" for _, scope in fake_auth.signed_out)


def test_reset_rejects_a_bad_token(client, fake_auth):
    fake_auth.add_user("alex@example.com", GOOD_PASSWORD)
    response = client.post(
        "/api/auth/password/reset",
        json={
            "email": "alex@example.com",
            "token": "forged-token",
            "password": "brand-New-Password9",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_reset_token"


def test_reset_enforces_the_password_policy(client, fake_auth):
    fake_auth.add_user("alex@example.com", GOOD_PASSWORD)
    response = client.post(
        "/api/auth/password/reset",
        json={"email": "alex@example.com", "token": "good-token", "password": "weak"},
    )
    assert response.status_code == 422


def test_resend_verification_does_not_reveal_membership(client, fake_auth):
    fake_auth.add_user("real@example.com", GOOD_PASSWORD, confirmed=False)
    known = client.post("/api/auth/verify/resend", json={"email": "real@example.com"})
    unknown = client.post("/api/auth/verify/resend", json={"email": "ghost@example.com"})
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()


# ------------------------------------------------------------ protected routes


def test_protected_routes_refuse_without_a_token(client):
    """Everything that touches user data must be unreachable when signed out."""
    protected = [
        ("get", "/api/auth/session"),
        ("get", "/api/account"),
        ("patch", "/api/account/profile"),
        ("get", "/api/account/security"),
        ("get", "/api/account/export"),
        ("get", "/api/integrations"),
        ("post", "/api/integrations/google/connect"),
        ("delete", "/api/integrations/google"),
        ("get", "/api/agent/capabilities"),
        ("get", "/api/agent/conversations"),
    ]
    for method, path in protected:
        response = client.request(method.upper(), path, json={})
        assert response.status_code == 401, f"{method.upper()} {path} was reachable"


def test_protected_routes_refuse_a_forged_token(client):
    forged = make_token("attacker", secret="not-the-signing-key")
    response = client.get("/api/account", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


def test_protected_routes_refuse_an_expired_token(client):
    stale = make_token("user-1", expires_in=-60)
    response = client.get("/api/account", headers={"Authorization": f"Bearer {stale}"})
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Your session has expired. Please sign in again."


def test_unverified_user_cannot_reach_integrations(client):
    """Verification gates the surface where damage reaches other people."""
    token = make_token("user-1", verified=False)
    response = client.get("/api/integrations", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "email_not_verified"


# ------------------------------------------------------------------- responses


def test_errors_never_leak_internals(client, fake_auth):
    fake_auth.add_user("alex@example.com", GOOD_PASSWORD)
    response = client.post(
        "/api/auth/login", json={"email": "alex@example.com", "password": "Wrong-Password-1"}
    )
    body = response.text.lower()
    for leak in ("traceback", "gotrue", "supabase", "postgres", "asyncpg", "file \""):
        assert leak not in body


def test_security_headers_are_present(client):
    response = client.get("/api/auth/config")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "no-store" in response.headers["Cache-Control"]
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]


def test_public_config_exposes_no_secrets(client):
    response = client.get("/api/auth/config")
    body = response.text
    for secret in (
        "service-key-for-tests",
        "anon-key-for-tests",
        "test-client-secret",
        "test-hs256-secret-not-a-real-one",
    ):
        assert secret not in body


def test_refresh_from_cookie_requires_csrf(client, fake_auth):
    """The one cookie-authenticated endpoint, and the one that needs the check."""
    fake_auth.add_user("alex@example.com", GOOD_PASSWORD)
    client.post("/api/auth/login", json={"email": "alex@example.com", "password": GOOD_PASSWORD})

    # The client now holds both cookies. Refresh without echoing the CSRF token.
    client.cookies.delete("lumen_csrf")
    response = client.post("/api/auth/refresh", json={})
    assert response.status_code == 401
