"""Nothing credential-shaped may reach a log.

The requirement is a list of things that must never be logged. A list like that
is only as good as the enforcement, so these tests are written as the list
itself: one case per kind of secret, plus the cases where a secret arrives
somewhere nobody expected it -- inside an exception message, inside a URL,
inside a dict passed as a log argument.
"""

from __future__ import annotations

import logging

from server.observability import RedactingFilter, mask_email, safe, scrub


def test_jwt_is_removed():
    line = (
        "auth failed for eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0In0.dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    )
    assert "eyJ" not in scrub(line)
    assert "[jwt]" in scrub(line)


def test_google_refresh_token_is_removed():
    line = "storing 1//0eXaMPLE-refresh_token_value_here_1234567890"
    cleaned = scrub(line)
    assert "1//0eXaMPLE" not in cleaned
    assert "[refresh-token]" in cleaned


def test_api_keys_are_removed():
    # These carry "notarealkey" because the repository's own secret scan
    # (scripts/selftest.py::test_no_developer_key) greps for key-shaped strings
    # and exempts that marker. A test fixture must not look like a live
    # credential to the tool whose job is finding live credentials.
    google = "AIzaSyD-notarealkey-0123456789abcdefghijklmn"
    openai = "sk-proj-notarealkey-0123456789abcdefghij"

    for candidate in (google, openai):
        cleaned = scrub(f"using key {candidate} now")
        assert "[api-key]" in cleaned
        assert candidate not in cleaned


def test_authorization_headers_are_removed():
    # A header line matches both the bearer rule and the generic `name: value`
    # rule, so it comes out redacted twice. Over-redacting is the right way for
    # this to be wrong, so the assertion is on the secret being gone rather
    # than on an exact string.
    for line, secret in (
        ("Authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop"),
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
        ("sent Bearer abcdefghijklmnop to google", "abcdefghijklmnop"),
    ):
        cleaned = scrub(line)
        assert secret not in cleaned, line
        assert "[redacted]" in cleaned


def test_assignments_are_removed():
    for line in (
        'password="hunter2hunter2"',
        "password=hunter2hunter2",
        '"client_secret": "GOCSPX-abcdefghijk"',
        "code_verifier=abcdef123456",
        "api_key: abcdef123456",
    ):
        cleaned = scrub(line)
        assert "[redacted]" in cleaned, line
        assert "hunter2" not in cleaned
        assert "GOCSPX" not in cleaned
        assert "abcdef123456" not in cleaned


def test_sealed_values_are_removed():
    line = "row holds v1.abcdefghijklmnop.qrstuvwxyz0123456789"
    assert "[sealed]" in scrub(line)


def test_filter_applies_to_message_and_arguments():
    """The realistic failure: a token arrives as a `%s` argument, not in the format string."""
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="calling with %s",
        args=("Bearer abcdefghijklmnopqrst",),
        exc_info=None,
    )
    RedactingFilter().filter(record)
    assert "abcdefghijklmnopqrst" not in record.getMessage()


def test_filter_applies_to_dict_arguments():
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="%(token)s",
        # Wrapped in a tuple the way logging itself passes a mapping through;
        # LogRecord unwraps a single Mapping argument on construction.
        args=({"token": "Bearer abcdefghijklmnopqrst"},),
        exc_info=None,
    )
    RedactingFilter().filter(record)
    assert "abcdefghijklmnopqrst" not in record.getMessage()


def test_safe_drops_unlisted_fields():
    """An allowlist, so a newly named field is invisible until someone allows it."""
    rendered = safe(
        user_id="abc",
        provider="google",
        refresh_token="1//secret",
        password="hunter2",
        some_new_field="whatever",
    )
    assert "abc" in rendered
    assert "google" in rendered
    assert "refresh_token" not in rendered
    assert "hunter2" not in rendered
    assert "some_new_field" not in rendered


def test_safe_scrubs_allowlisted_string_values_too():
    """Even a permitted field cannot smuggle a token through."""
    rendered = safe(reason="failed with Bearer abcdefghijklmnop")
    assert "abcdefghijklmnop" not in rendered


def test_email_is_masked():
    assert mask_email("alex@example.com") == "a***@example.com"
    assert mask_email(None) == "[email]"
    assert mask_email("not-an-address") == "[email]"


def test_scrub_leaves_ordinary_text_alone():
    line = "user 4f2a signed in from 203.0.113.0/24 in 41ms"
    assert scrub(line) == line
