"""Password policy.

No hashing is tested here because none is implemented -- GoTrue owns that, with
bcrypt. What is tested is what we refuse to send it in the first place, and that
the refusal messages are useful enough that somebody can act on them.
"""

from __future__ import annotations

import pytest

from server.security.passwords import MIN_LENGTH, PasswordRejected, validate


def test_accepts_a_reasonable_password():
    assert validate("correct-horse-Battery9") == "correct-horse-Battery9"


def test_accepts_a_long_passphrase():
    assert validate("Seven Bridges over the quiet River 1988")


@pytest.mark.parametrize(
    "candidate",
    ["", "short", "Sh0rt", "aB1" * 3],  # all under the minimum
)
def test_rejects_short(candidate):
    with pytest.raises(PasswordRejected):
        validate(candidate)


def test_minimum_is_twelve():
    # Deliberately not alphabetical, so this tests the length boundary rather
    # than tripping the sequential-run rule on the way past it.
    assert MIN_LENGTH == 12
    with pytest.raises(PasswordRejected):
        validate("Xk9-mnop-Q2")  # 11
    assert validate("Xk9-mnop-Qr2")  # 12


def test_rejects_missing_character_classes():
    for candidate in ("alllowercase123", "ALLUPPERCASE123", "NoDigitsInHere"):
        with pytest.raises(PasswordRejected):
            validate(candidate)


def test_rejects_overlong():
    with pytest.raises(PasswordRejected):
        validate("Aa1" + "x" * 200)


def test_rejects_common_passwords():
    with pytest.raises(PasswordRejected):
        validate("Password1234".lower().capitalize() if False else "password123")


def test_rejects_repeated_runs():
    """Twelve characters of nothing is still nothing."""
    with pytest.raises(PasswordRejected):
        validate("Aaaaaaaaaaaa1")


def test_rejects_sequential_runs():
    with pytest.raises(PasswordRejected):
        validate("Abcdefghijk1")


def test_rejects_the_users_own_email():
    with pytest.raises(PasswordRejected) as caught:
        validate("alexander-Smith99", email="alexander@example.com")
    assert "email" in str(caught.value).lower()


def test_rejects_the_users_own_name():
    with pytest.raises(PasswordRejected):
        validate("Montgomery-Wins7", name="Montgomery")


def test_short_names_do_not_trigger_the_name_rule():
    """A three-letter name would otherwise ban half of all passwords."""
    assert validate("Ann-loves-Tigers7", name="Ann")


def test_rejects_control_characters():
    with pytest.raises(PasswordRejected):
        validate("Abcdefgh1\x00234")


def test_normalises_unicode():
    """The same phrase typed two ways must be the same password.

    Composed vs decomposed accents look identical and compare unequal as bytes,
    which would otherwise lock somebody out of their own account depending on
    which machine they typed it on.
    """
    composed = "Café-passphrase9"
    decomposed = "Café-passphrase9"
    assert validate(composed) == validate(decomposed)


def test_messages_are_actionable_and_safe():
    """A user has to be able to fix it, without being told anything useful to
    an attacker."""
    # Distinctive strings, so "is the password echoed back?" is not confused by
    # an ordinary English word that happens to appear in the advice.
    for candidate in ("zqx4", "alllowercase123", "Qqqqqqqqqqqq1"):
        with pytest.raises(PasswordRejected) as caught:
            validate(candidate)
        message = str(caught.value)
        assert message.endswith(".")
        assert candidate not in message
