"""Where a user's API keys live: the operating system's own credential store.

No key is ever written to the .env, to providers.json, to the reminders
database, or to any file this app formats itself. Each provider's key is one
entry in the platform keystore, owned by the logged-in user and encrypted by
the OS with credentials this process never sees:

* **Windows** — Credential Manager (``CredWriteW``/``CredReadW``), reached
  through ctypes so there is no extra dependency to bundle into the .exe.
* **macOS** — the login Keychain, via the ``security`` binary that ships with
  the OS.
* **Linux** — the Secret Service (GNOME Keyring, KWallet) via ``secret-tool``.
* **Anywhere** — the ``keyring`` package, if the user happens to have it, as a
  backstop for a desktop the three above do not cover.

If none of those is available there is no safe place to put a key, and the app
says so instead of quietly inventing one. A plaintext file is available only
when the user opts into it by setting AGENT_ALLOW_INSECURE_KEYSTORE=1, is
reported as insecure everywhere it is used, and is documented as such.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Protocol

from ..config import CREDENTIALS_DIR

# One namespace for every entry this app owns, so `account` only has to be the
# provider id and an uninstall knows exactly what to sweep up.
SERVICE = "WorkspaceAgent.AIProvider"

INSECURE_OPT_IN = "AGENT_ALLOW_INSECURE_KEYSTORE"


class KeystoreError(RuntimeError):
    """The store could not be read or written. Never carries the secret."""


class Keystore(Protocol):
    name: str
    detail: str
    secure: bool

    def get(self, account: str) -> str | None: ...
    def set(self, account: str, secret: str) -> None: ...
    def delete(self, account: str) -> bool: ...


# ------------------------------------------------------------------- windows


class WindowsCredentialStore:
    """Windows Credential Manager, generic credentials, current user only.

    CredWriteW with CRED_PERSIST_LOCAL_MACHINE keeps the entry on this machine
    for this Windows account. Another user signing in to the same PC cannot
    read it, which is the same boundary the Google token already relies on.
    """

    name = "Windows Credential Manager"
    detail = "encrypted per Windows user account"
    secure = True

    # advapi32 constants
    _GENERIC = 1
    _PERSIST_LOCAL_MACHINE = 2

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._advapi = ctypes.WinDLL("advapi32", use_last_error=True)

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        self._CREDENTIAL = CREDENTIAL
        self._advapi.CredReadW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(CREDENTIAL)),
        ]
        self._advapi.CredReadW.restype = wintypes.BOOL
        self._advapi.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
        self._advapi.CredWriteW.restype = wintypes.BOOL
        self._advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._advapi.CredDeleteW.restype = wintypes.BOOL
        self._advapi.CredFree.argtypes = [ctypes.c_void_p]
        self._advapi.CredFree.restype = None

    @staticmethod
    def available() -> bool:
        return sys.platform == "win32"

    def _target(self, account: str) -> str:
        return f"{SERVICE}:{account}"

    def get(self, account: str) -> str | None:
        ctypes = self._ctypes
        pointer = ctypes.POINTER(self._CREDENTIAL)()
        if not self._advapi.CredReadW(self._target(account), self._GENERIC, 0, ctypes.byref(pointer)):
            code = ctypes.get_last_error()
            if code == 1168:  # ERROR_NOT_FOUND — nothing stored, not a failure
                return None
            raise KeystoreError(f"Credential Manager read failed (error {code})")
        try:
            blob = pointer.contents
            size = blob.CredentialBlobSize
            if not size:
                return None
            raw = ctypes.string_at(blob.CredentialBlob, size)
            return raw.decode("utf-16-le")
        finally:
            self._advapi.CredFree(pointer)

    def set(self, account: str, secret: str) -> None:
        ctypes = self._ctypes
        blob = secret.encode("utf-16-le")
        credential = self._CREDENTIAL()
        credential.Flags = 0
        credential.Type = self._GENERIC
        credential.TargetName = self._target(account)
        # The comment is visible in the Credential Manager UI, so it says what
        # the entry is for and nothing about what is in it.
        credential.Comment = "AI provider API key stored by Lumen OS"
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(
            ctypes.create_string_buffer(blob, len(blob)), ctypes.POINTER(ctypes.c_char)
        )
        credential.Persist = self._PERSIST_LOCAL_MACHINE
        credential.AttributeCount = 0
        credential.Attributes = None
        credential.TargetAlias = None
        credential.UserName = account
        if not self._advapi.CredWriteW(ctypes.byref(credential), 0):
            raise KeystoreError(
                f"Credential Manager write failed (error {ctypes.get_last_error()})"
            )

    def delete(self, account: str) -> bool:
        if not self._advapi.CredDeleteW(self._target(account), self._GENERIC, 0):
            code = self._ctypes.get_last_error()
            if code == 1168:
                return False
            raise KeystoreError(f"Credential Manager delete failed (error {code})")
        return True


# --------------------------------------------------------------------- macOS


class MacKeychainStore:
    """The login Keychain, through the `security` tool.

    The secret goes over argv on write, which is visible to `ps` for the
    instant the call runs. That is how `security` is designed; the alternative
    is a compiled binding. Reads and deletes never carry it.
    """

    name = "macOS Keychain"
    detail = "login keychain"
    secure = True

    @staticmethod
    def available() -> bool:
        return sys.platform == "darwin" and bool(shutil.which("security"))

    def get(self, account: str) -> str | None:
        result = subprocess.run(  # noqa: S603
            ["security", "find-generic-password", "-a", account, "-s", SERVICE, "-w"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def set(self, account: str, secret: str) -> None:
        result = subprocess.run(  # noqa: S603
            ["security", "add-generic-password", "-a", account, "-s", SERVICE, "-w", secret, "-U"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            # stderr from `security` describes the keychain, not the secret,
            # but it is trimmed anyway rather than passed through whole.
            raise KeystoreError(f"Keychain write failed: {result.stderr.strip()[:120]}")

    def delete(self, account: str) -> bool:
        result = subprocess.run(  # noqa: S603
            ["security", "delete-generic-password", "-a", account, "-s", SERVICE],
            capture_output=True, text=True, check=False,
        )
        return result.returncode == 0


# --------------------------------------------------------------------- linux


class SecretToolStore:
    """Secret Service (GNOME Keyring, KWallet) through `secret-tool`.

    The secret goes in on stdin, never on the command line.
    """

    name = "Secret Service"
    detail = "libsecret / GNOME Keyring"
    secure = True

    @staticmethod
    def available() -> bool:
        return sys.platform.startswith("linux") and bool(shutil.which("secret-tool"))

    def get(self, account: str) -> str | None:
        result = subprocess.run(  # noqa: S603
            ["secret-tool", "lookup", "service", SERVICE, "account", account],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def set(self, account: str, secret: str) -> None:
        result = subprocess.run(  # noqa: S603
            ["secret-tool", "store", "--label", f"{SERVICE} {account}",
             "service", SERVICE, "account", account],
            input=secret, capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise KeystoreError(f"Secret Service write failed: {result.stderr.strip()[:120]}")

    def delete(self, account: str) -> bool:
        result = subprocess.run(  # noqa: S603
            ["secret-tool", "clear", "service", SERVICE, "account", account],
            capture_output=True, text=True, check=False,
        )
        return result.returncode == 0


# ------------------------------------------------------------------- keyring


class KeyringStore:
    """The `keyring` package, if it is installed. A backstop, not the plan."""

    name = "keyring"
    detail = ""
    secure = True

    def __init__(self) -> None:
        import keyring

        self._keyring = keyring
        self.detail = type(keyring.get_keyring()).__name__

    @staticmethod
    def available() -> bool:
        try:
            import keyring
            from keyring.backends.fail import Keyring as FailKeyring
        except ImportError:
            return False
        # An unconfigured keyring resolves to a backend that raises on every
        # call. Treat that as unavailable rather than as a working store.
        return not isinstance(keyring.get_keyring(), FailKeyring)

    def get(self, account: str) -> str | None:
        return self._keyring.get_password(SERVICE, account)

    def set(self, account: str, secret: str) -> None:
        self._keyring.set_password(SERVICE, account, secret)

    def delete(self, account: str) -> bool:
        try:
            self._keyring.delete_password(SERVICE, account)
        except Exception:
            return False
        return True


# ---------------------------------------------------------------- last resort


class InsecureFileStore:
    """A file on disk, only when the user has explicitly asked for it.

    Encoding is base64, which is not encryption and is not claimed to be: with
    no OS keystore there is nowhere to put an encryption key that an attacker
    who can read this file could not also read. It exists so a headless Linux
    box is usable at all, it is off unless AGENT_ALLOW_INSECURE_KEYSTORE=1, and
    every screen that shows it says the keys are not protected.
    """

    name = "unprotected file"
    detail = "no OS keystore available — keys are NOT encrypted"
    secure = False

    def __init__(self, path: Path | None = None):
        self.path = path or (CREDENTIALS_DIR / "provider-keys.json")

    @staticmethod
    def available() -> bool:
        return (os.getenv(INSECURE_OPT_IN) or "").strip().lower() in {"1", "true", "yes", "on"}

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        # Owner-only before it takes the real name, so there is no window where
        # the file exists world-readable. chmod is a no-op on Windows.
        with contextlib.suppress(OSError):
            os.chmod(temporary, 0o600)
        temporary.replace(self.path)

    def get(self, account: str) -> str | None:
        raw = self._read().get(account)
        if not isinstance(raw, str):
            return None
        try:
            return base64.b64decode(raw.encode("ascii")).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None

    def set(self, account: str, secret: str) -> None:
        data = self._read()
        data[account] = base64.b64encode(secret.encode("utf-8")).decode("ascii")
        self._write(data)

    def delete(self, account: str) -> bool:
        data = self._read()
        if account not in data:
            return False
        del data[account]
        self._write(data)
        return True


# --------------------------------------------------------------- the chooser

# Native stores first: no extra dependency, predictable inside a PyInstaller
# build, and each one is the store the platform's own users expect.
_BACKENDS = (WindowsCredentialStore, MacKeychainStore, SecretToolStore, KeyringStore, InsecureFileStore)

_cached: Keystore | None = None


def keystore(refresh: bool = False) -> Keystore:
    """The store this machine will use. Raises if there is no safe option."""
    global _cached  # noqa: PLW0603 - one keystore per process, chosen once
    if _cached is not None and not refresh:
        return _cached
    for backend in _BACKENDS:
        try:
            if backend.available():
                _cached = backend()
                return _cached
        except Exception:
            continue  # a backend that cannot initialise is simply not available
    raise KeystoreError(
        "No secure credential store is available on this system, so there is "
        "nowhere safe to keep an API key. Install a Secret Service keyring "
        "(GNOME Keyring or KWallet), or set "
        f"{INSECURE_OPT_IN}=1 to accept keys being stored unencrypted on disk."
    )


def describe() -> dict:
    """What the UI shows about where keys are kept."""
    try:
        store = keystore()
    except KeystoreError as exc:
        return {"available": False, "secure": False, "name": "none", "detail": str(exc)}
    return {
        "available": True,
        "secure": store.secure,
        "name": store.name,
        "detail": store.detail,
    }


def reset_cache() -> None:
    """Forget the chosen backend. Tests install their own; nothing else needs it."""
    global _cached  # noqa: PLW0603 - see keystore()
    _cached = None


def use(store: Keystore | None) -> None:
    """Install a specific store. Test seam — the app never calls this."""
    global _cached  # noqa: PLW0603 - see keystore()
    _cached = store
