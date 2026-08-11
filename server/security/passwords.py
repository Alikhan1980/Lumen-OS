"""Password rules.

There is no hashing in this file, and there must never be. Supabase's GoTrue
hashes with bcrypt on its side of the wire; a password reaches this server only
in transit, is forwarded once over TLS, and is never written anywhere -- not to
a log, not to a database column, not to an error message.

What is here is the policy: what we refuse to accept in the first place. Length
does most of the work, so the minimum is 12 rather than the traditional 8. The
character-class rules are deliberately mild, because pushing people into
`Password1!` produces worse passwords than a long phrase does.

The blocklist is a short list of the passwords that actually show up at the top
of every credential-stuffing corpus, plus anything derived from the user's own
email. It is not a substitute for a breach-corpus check -- see the note at the
bottom for wiring one in.
"""

from __future__ import annotations

import re
import unicodedata

MIN_LENGTH = 12
# Long enough for a passphrase, short enough that a pathological input cannot
# be used to make bcrypt burn CPU. (bcrypt also truncates past 72 bytes, so
# anything beyond this is security theatre in any case.)
MAX_LENGTH = 128

# The perennial top of every leaked-password list, normalised to lowercase.
# Short on purpose: a long list belongs in a breach corpus, not in source.
_COMMON = frozenset(
    {
        "password", "password1", "password123", "passw0rd", "qwertyuiop",
        "123456789012", "1234567890", "letmein12345", "iloveyou123",
        "admin1234567", "welcome12345", "monkey123456", "abc123456789",
        "qwerty123456", "dragon123456", "sunshine1234", "princess1234",
        "football1234", "baseball1234", "trustno1trust", "changeme1234",
    }
)


class PasswordRejected(ValueError):
    """The password does not meet policy. The message is safe to show a user."""


def _normalise(raw: str) -> str:
    # NFKC so a password typed with composed and decomposed accents on two
    # different machines is the same password. GoTrue compares bytes.
    return unicodedata.normalize("NFKC", raw)


def _sequence_run(text: str, length: int = 5) -> bool:
    """True if the text is mostly one repeated or sequential run.

    Catches `aaaaaaaaaaaa` and `123456789012`, which pass a naive length check
    while carrying almost no entropy.
    """
    lowered = text.lower()
    repeats = 1
    ascending = 1
    for index in range(1, len(lowered)):
        previous, current = lowered[index - 1], lowered[index]
        repeats = repeats + 1 if current == previous else 1
        ascending = ascending + 1 if ord(current) - ord(previous) == 1 else 1
        if repeats >= length or ascending >= length:
            return True
    return False


def validate(password: str, *, email: str | None = None, name: str | None = None) -> str:
    """Check a password against policy. Returns the normalised form, or raises.

    The returned value is what should be sent onward to GoTrue, so that the
    string checked and the string stored are the same one.
    """
    if not isinstance(password, str) or not password:
        raise PasswordRejected("Enter a password.")

    candidate = _normalise(password)

    if len(candidate) < MIN_LENGTH:
        raise PasswordRejected(
            f"Use at least {MIN_LENGTH} characters. A short phrase you will "
            "remember beats a short password you will not."
        )
    if len(candidate) > MAX_LENGTH:
        raise PasswordRejected(f"Keep it under {MAX_LENGTH} characters.")

    # No control characters, and nothing that is only whitespace.
    if any(unicodedata.category(char).startswith("C") for char in candidate):
        raise PasswordRejected("Remove any control characters.")
    if not candidate.strip():
        raise PasswordRejected("Enter a password.")

    checks = (
        (re.search(r"[a-z]", candidate), "a lowercase letter"),
        (re.search(r"[A-Z]", candidate), "an uppercase letter"),
        (re.search(r"\d", candidate), "a number"),
    )
    missing = [label for found, label in checks if not found]
    if missing:
        raise PasswordRejected("Add " + ", ".join(missing) + ".")

    if candidate.lower() in _COMMON:
        raise PasswordRejected("That password is too common. Pick something else.")

    if _sequence_run(candidate):
        raise PasswordRejected(
            "That is mostly a repeated or sequential run. Mix it up a little."
        )

    # Anything built out of the address or the name is the first thing an
    # attacker tries against a known account.
    for source in (email, name):
        if not source:
            continue
        stem = re.split(r"[@\s]", source.strip().lower(), maxsplit=1)[0]
        if len(stem) >= 4 and stem in candidate.lower():
            raise PasswordRejected("Do not use your name or email in your password.")

    return candidate


# A breach-corpus check is the single highest-value addition here, and it is
# deliberately not wired in by default because it puts a third party in the
# signup path. To add it: call Have I Been Pwned's range API with the first five
# hex characters of the SHA-1 of `candidate`, and reject on a suffix match. Only
# those five characters leave the server (k-anonymity), never the password. Fail
# open on a network error -- an outage there must not stop people signing up.
