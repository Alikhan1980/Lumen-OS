"""Envelope encryption for the OAuth tokens the server holds on a user's behalf.

AES-256-GCM from `cryptography`, which is a binding to OpenSSL. No cryptography
is implemented here -- this module chooses a well-established mode, generates
nonces correctly, and formats the result. That is the whole job, and the parts
that are easy to get wrong (the primitive, the tag handling, the constant-time
comparisons) are the library's.

Why encrypt at all when Postgres already restricts the table? Because the two
failures are different. RLS stops a signed-in user reaching a row. Encryption
stops a *database* leak -- a stray dump, a snapshot restored to the wrong place,
a read replica someone forgot about -- from yielding a working Google credential.
The key lives in the server's environment, so an attacker needs both.

Format of a stored value:

    v<version>.<base64url nonce>.<base64url ciphertext+tag>

The version is in the string rather than only in the row, so a value that moves
between columns still says how to read itself. Rotation adds a new key, keeps
the old one listed for decryption only, and a background pass re-wraps rows.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..settings import ConfigError, settings

# AES-GCM's standard nonce size. 96 bits is what the mode is specified around;
# other lengths are legal but go through an extra derivation and buy nothing.
NONCE_BYTES = 12
KEY_BYTES = 32


class DecryptionError(RuntimeError):
    """A stored secret could not be read back.

    Wrong key, rotated-away key, or a tampered row. Deliberately says which of
    those it is nowhere near a user -- see server/errors.py.
    """


def _decode_key(raw: str, label: str) -> bytes:
    try:
        key = base64.b64decode(raw, validate=True)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{label} is not valid base64.") from exc
    if len(key) != KEY_BYTES:
        raise ConfigError(
            f"{label} must decode to {KEY_BYTES} bytes for AES-256 "
            f"(got {len(key)}). Generate one with:\n"
            "    python -c \"import base64,os;print(base64.b64encode(os.urandom(32)).decode())\""
        )
    return key


@dataclass(frozen=True)
class Keyring:
    """The key used for new writes, plus every key still needed for reads."""

    current_version: int
    keys: dict[int, bytes]

    def cipher(self, version: int) -> AESGCM:
        key = self.keys.get(version)
        if key is None:
            raise DecryptionError(f"no key for version {version}")
        return AESGCM(key)


@lru_cache(maxsize=1)
def keyring() -> Keyring:
    config = settings()
    keys = {config.token_key_version: _decode_key(config.token_key, "LUMEN_TOKEN_ENCRYPTION_KEY")}

    # "2:<b64>,1:<b64>" -- superseded keys, kept so rows written before a
    # rotation still open. Never used for new writes.
    for raw_entry in config.token_keys_previous.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        version_text, _, key_text = entry.partition(":")
        if not key_text:
            raise ConfigError(
                "LUMEN_TOKEN_KEYS_PREVIOUS entries must look like '1:<base64 key>'."
            )
        try:
            version = int(version_text)
        except ValueError as exc:
            raise ConfigError(
                f"LUMEN_TOKEN_KEYS_PREVIOUS has a non-numeric version: {version_text!r}"
            ) from exc
        if version == config.token_key_version:
            raise ConfigError(
                f"LUMEN_TOKEN_KEYS_PREVIOUS lists version {version}, which is also "
                "the current LUMEN_TOKEN_KEY_VERSION. Bump the current version."
            )
        keys[version] = _decode_key(key_text.strip(), f"LUMEN_TOKEN_KEYS_PREVIOUS[{version}]")

    return Keyring(current_version=config.token_key_version, keys=keys)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def encrypt(plaintext: str, *, context: str) -> str:
    """Wrap a secret for storage.

    `context` is bound in as additional authenticated data -- it is not secret,
    but it is authenticated, so a ciphertext lifted out of one row cannot be
    replayed into another. Pass something that identifies the slot, such as
    ``f"integration_secret:{connection_id}:refresh_token"``. Decryption has to
    supply the identical string.
    """
    ring = keyring()
    nonce = os.urandom(NONCE_BYTES)
    sealed = ring.cipher(ring.current_version).encrypt(
        nonce, plaintext.encode("utf-8"), context.encode("utf-8")
    )
    return f"v{ring.current_version}.{_b64(nonce)}.{_b64(sealed)}"


def decrypt(stored: str, *, context: str) -> str:
    """Unwrap a value written by `encrypt`. Raises DecryptionError on any fault."""
    ring = keyring()
    try:
        version_part, nonce_part, body_part = stored.split(".", 2)
        version = int(version_part.removeprefix("v"))
        nonce = _unb64(nonce_part)
        sealed = _unb64(body_part)
    except (ValueError, TypeError) as exc:
        raise DecryptionError("stored secret is malformed") from exc

    try:
        opened = ring.cipher(version).decrypt(nonce, sealed, context.encode("utf-8"))
    except InvalidTag as exc:
        # Wrong key, wrong context, or the row was edited. All three mean the
        # same thing to a caller: this credential is not usable.
        raise DecryptionError("stored secret failed authentication") from exc
    return opened.decode("utf-8")


def needs_rewrap(stored: str) -> bool:
    """True when a value was written under a superseded key.

    Lets a read path opportunistically re-encrypt, so a rotation completes as
    rows are touched instead of needing a migration window.
    """
    try:
        version = int(stored.split(".", 1)[0].removeprefix("v"))
    except (ValueError, IndexError):
        return False
    return version != keyring().current_version
