"""Encryption of the OAuth tokens held at rest.

The properties worth testing are not "does AES work" -- that is OpenSSL's
problem -- but the ones this module adds on top: that a ciphertext is bound to
the row it belongs to, that tampering is detected rather than tolerated, and
that a key rotation leaves old rows readable.
"""

from __future__ import annotations

import base64

import pytest

from server.security import crypto
from server.settings import ConfigError


def test_round_trip():
    sealed = crypto.encrypt("1//refresh-token-value", context="row:1:refresh")
    assert crypto.decrypt(sealed, context="row:1:refresh") == "1//refresh-token-value"


def test_ciphertext_does_not_contain_the_plaintext():
    sealed = crypto.encrypt("1//super-secret", context="row:1:refresh")
    assert "super-secret" not in sealed
    assert "1//" not in sealed


def test_nonce_differs_every_time():
    """Two encryptions of the same value must not produce the same ciphertext.

    Otherwise a database dump reveals which users hold identical values, and
    GCM's security collapses entirely if a nonce is ever reused with one key.
    """
    a = crypto.encrypt("same", context="row:1:refresh")
    b = crypto.encrypt("same", context="row:1:refresh")
    assert a != b
    assert crypto.decrypt(a, context="row:1:refresh") == crypto.decrypt(b, context="row:1:refresh")


def test_context_is_authenticated():
    """A ciphertext lifted into another row must not decrypt there.

    This is what stops "copy user B's refresh_token_enc into user A's row" from
    being a working attack for anyone with write access to the table.
    """
    sealed = crypto.encrypt("token", context="integration_secret:AAA:refresh_token")
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(sealed, context="integration_secret:BBB:refresh_token")


def test_field_is_authenticated_too():
    sealed = crypto.encrypt("token", context="integration_secret:AAA:refresh_token")
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(sealed, context="integration_secret:AAA:access_token")


def test_tampering_is_rejected():
    sealed = crypto.encrypt("token", context="row:1:refresh")
    version, nonce, body = sealed.split(".", 2)
    flipped = body[:-4] + ("AAAA" if not body.endswith("AAAA") else "BBBB")
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(f"{version}.{nonce}.{flipped}", context="row:1:refresh")


def test_malformed_input_is_rejected_cleanly():
    for candidate in ("", "nonsense", "v1.only-two-parts", "v9.abc.def"):
        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt(candidate, context="row:1:refresh")


def test_rotation_keeps_old_rows_readable(monkeypatch):
    """A new key must not lock everyone out of their existing connections."""
    old_key = base64.b64encode(b"A" * 32).decode()
    new_key = base64.b64encode(b"B" * 32).decode()

    monkeypatch.setattr(
        crypto, "keyring", lambda: crypto.Keyring(1, {1: base64.b64decode(old_key)})
    )
    sealed_under_v1 = crypto.encrypt("old-token", context="row:1:refresh")

    monkeypatch.setattr(
        crypto,
        "keyring",
        lambda: crypto.Keyring(
            2, {2: base64.b64decode(new_key), 1: base64.b64decode(old_key)}
        ),
    )
    assert crypto.decrypt(sealed_under_v1, context="row:1:refresh") == "old-token"
    assert crypto.needs_rewrap(sealed_under_v1) is True

    fresh = crypto.encrypt("new-token", context="row:1:refresh")
    assert fresh.startswith("v2.")
    assert crypto.needs_rewrap(fresh) is False


def test_dropped_key_version_fails_closed(monkeypatch):
    """A row encrypted with a key that is gone must error, never return junk."""
    monkeypatch.setattr(
        crypto, "keyring", lambda: crypto.Keyring(1, {1: base64.b64decode(base64.b64encode(b"A" * 32))})
    )
    sealed = crypto.encrypt("token", context="row:1:refresh")

    monkeypatch.setattr(
        crypto, "keyring", lambda: crypto.Keyring(2, {2: base64.b64decode(base64.b64encode(b"B" * 32))})
    )
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(sealed, context="row:1:refresh")


def test_bad_key_configuration_is_a_startup_error(monkeypatch):
    """A short or non-base64 key must fail loudly at boot, not at first use."""
    from server import settings as settings_module

    crypto.keyring.cache_clear()
    monkeypatch.setenv("LUMEN_TOKEN_ENCRYPTION_KEY", "not-base64!!")
    settings_module.settings.cache_clear()
    with pytest.raises(ConfigError):
        crypto.keyring()

    crypto.keyring.cache_clear()
    monkeypatch.setenv("LUMEN_TOKEN_ENCRYPTION_KEY", base64.b64encode(b"tooshort").decode())
    settings_module.settings.cache_clear()
    with pytest.raises(ConfigError):
        crypto.keyring()

    # Leave the caches clean for whatever runs next.
    crypto.keyring.cache_clear()
    settings_module.settings.cache_clear()
