"""The provider manager: credentials, the active choice, and the lock.

Every path that could reach an AI provider goes through here. There is exactly
one way to obtain a working provider — :meth:`ProviderManager.require_active` —
and it raises :class:`ProviderNotConfigured` unless the user has connected one
of their own. The UI shows a setup screen because of that exception; it does
not decide the policy. A new entry point added to this app in future gets the
lock for free as long as it asks the manager for its provider.

There is no key belonging to whoever built this app, and no code path that
could fall back to one. The only credentials this class can ever see are:

1. what the user stored in their own OS keystore, and
2. in a *development checkout only*, an environment variable — which never
   overrides (1), and is reported as coming from the environment wherever a
   provider's status is shown.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

from ..config import is_frozen
from . import catalog, keystore
from . import settings as settings_store
from .base import (
    FALLBACK_CODES,
    AIProvider,
    ModelInfo,
    ProviderError,
    ProviderNotConfigured,
    ValidationResult,
    mask,
)
from .settings import ProviderSettings

# Set to 1 to let a packaged build read keys from the environment. Off by
# default so a user can never inherit a key from a developer's shell or a
# stray .env that shipped alongside the app.
ENV_OPT_IN = "AGENT_ALLOW_ENV_KEYS"

SOURCE_KEYSTORE = "keystore"
SOURCE_ENVIRONMENT = "environment"


@dataclass(frozen=True)
class Credential:
    provider_id: str
    key: str
    source: str

    @property
    def masked(self) -> str:
        return mask(self.key)

    def __repr__(self) -> str:  # never let a key reach a traceback or a log
        return f"<Credential {self.provider_id} {self.source} {self.masked}>"

    __str__ = __repr__


class ProviderManager:
    """Owns credentials and the active-provider choice.

    Safe to construct before anything is configured: that is the whole point,
    since the app has to boot far enough to show a setup screen.
    """

    def __init__(
        self,
        store: keystore.Keystore | None = None,
        settings: ProviderSettings | None = None,
        *,
        settings_path=None,
        allow_env: bool | None = None,
    ):
        self._store = store
        self._settings_path = settings_path
        self._settings = settings if settings is not None else settings_store.load(settings_path)
        self._lock = threading.RLock()
        # A dev checkout may use environment variables; a shipped build must
        # opt in explicitly. See the module docstring.
        if allow_env is None:
            allow_env = (
                not is_frozen()
                or (os.getenv(ENV_OPT_IN) or "").strip().lower() in {"1", "true", "yes", "on"}
            )
        self.allow_env = allow_env

    # ---------------------------------------------------------------- stores

    def keystore(self) -> keystore.Keystore:
        if self._store is None:
            self._store = keystore.keystore()
        return self._store

    def keystore_status(self) -> dict:
        if self._store is not None:
            return {
                "available": True,
                "secure": self._store.secure,
                "name": self._store.name,
                "detail": self._store.detail,
            }
        return keystore.describe()

    def _save(self) -> None:
        settings_store.save(self._settings, self._settings_path)

    @property
    def settings(self) -> ProviderSettings:
        return self._settings

    # ----------------------------------------------------------- credentials

    def credential(self, provider_id: str) -> Credential | None:
        """The key for one provider, and where it came from.

        Precedence, top wins:

            user-configured secure credential
                    ↓
            developer environment variable (development only)
                    ↓
            no credential

        A key the user stored is never shadowed by an environment variable —
        that is what stops a developer's shell leaking into a real session.
        """
        provider = catalog.provider_class(provider_id)
        if provider is None:
            return None

        try:
            stored = self.keystore().get(provider_id)
        except keystore.KeystoreError:
            stored = None
        if stored:
            return Credential(provider_id, stored, SOURCE_KEYSTORE)

        if self.allow_env and provider.env_var:
            from_env = (os.getenv(provider.env_var) or "").strip()
            if from_env:
                return Credential(provider_id, from_env, SOURCE_ENVIRONMENT)
        return None

    def configured_ids(self) -> list[str]:
        return [pid for pid in catalog.provider_ids() if self.credential(pid) is not None]

    def is_configured(self, provider_id: str) -> bool:
        return self.credential(provider_id) is not None

    # ---------------------------------------------------------------- models

    def model_for(self, provider_id: str) -> str:
        provider = catalog.provider_class(provider_id)
        if provider is None:
            return ""
        return self._settings.models.get(provider_id) or provider.default_model()

    def set_model(self, provider_id: str, model: str) -> None:
        if catalog.provider_class(provider_id) is None:
            raise ValueError(f"unknown provider: {provider_id}")
        if not model.strip():
            raise ValueError("a model id is required")
        with self._lock:
            self._settings.models[provider_id] = model.strip()
            self._save()

    def models_for(self, provider_id: str, live: bool = False) -> list[ModelInfo]:
        """The model list for a picker. `live` asks the provider's API."""
        provider_class = catalog.provider_class(provider_id)
        if provider_class is None:
            return []
        if not live:
            return provider_class.catalog()
        try:
            return self.build(provider_id).live_models()
        except (ProviderNotConfigured, ProviderError):
            return provider_class.catalog()

    # ------------------------------------------------------------ management

    def build(self, provider_id: str | None = None) -> AIProvider:
        """Instantiate a provider with its stored key. Raises if there is none."""
        target = provider_id or self._settings.active
        if not target:
            raise ProviderNotConfigured("No AI provider is selected.")
        provider_class = catalog.provider_class(target)
        if provider_class is None:
            raise ProviderNotConfigured(f"Unknown provider: {target}")
        credential = self.credential(target)
        if credential is None:
            raise ProviderNotConfigured(
                f"No API key is configured for {provider_class.name}."
            )
        return provider_class(credential.key, self.model_for(target))

    def validate(self, provider_id: str, key: str) -> ValidationResult:
        """Check a key without storing it. Format first, then the provider."""
        provider_class = catalog.provider_class(provider_id)
        if provider_class is None:
            return ValidationResult(False, f"Unknown provider: {provider_id}", "INVALID_REQUEST")

        key = (key or "").strip()
        problem = provider_class.check_format(key)
        if problem:
            return ValidationResult(False, problem, "INVALID_API_KEY")

        provider = provider_class(key, self.model_for(provider_id))
        try:
            return provider.validate_key()
        except ProviderError as exc:
            return ValidationResult(False, exc.message, exc.code)

    def add(self, provider_id: str, key: str, model: str | None = None) -> ValidationResult:
        """Validate a key, and store it only if the provider accepted it.

        This is also the replace path: an existing key is overwritten only
        after the new one has proved itself, so a failed attempt leaves the
        working key exactly as it was.
        """
        result = self.validate(provider_id, key)
        if not result.ok:
            return result

        with self._lock:
            self.keystore().set(provider_id, key.strip())
            if model:
                self._settings.models[provider_id] = model.strip()
            # First provider connected becomes the active one — there is
            # nothing to choose between, and the alternative is a locked app
            # with a working key in it.
            if not self._settings.active:
                self._settings.active = provider_id
            self._save()
        return result

    def remove(self, provider_id: str) -> dict:
        """Forget a provider's key and tidy up after it.

        Returns what happened to the active provider, so the caller can tell
        the user whether the agent is still usable.
        """
        with self._lock:
            try:
                removed = self.keystore().delete(provider_id)
            except keystore.KeystoreError:
                removed = False
            self._settings.models.pop(provider_id, None)
            self._settings.fallback_order = [
                pid for pid in self._settings.fallback_order if pid != provider_id
            ]

            outcome = {"removed": removed, "active": self._settings.active, "switched": False, "locked": False}
            if self._settings.active != provider_id:
                self._save()
                outcome["locked"] = not self.configured_ids()
                return outcome

            remaining = [pid for pid in self.configured_ids() if pid != provider_id]
            if len(remaining) == 1:
                # One candidate is not a choice; switching is the only sensible
                # move and the user is told which one it landed on.
                self._settings.active = remaining[0]
                outcome.update(active=remaining[0], switched=True)
            else:
                # Either nothing is left (locked) or several are, in which case
                # the user picks — never this code, silently, with their money.
                self._settings.active = None
                outcome.update(active=None, locked=not remaining)
            self._save()
            return outcome

    def test(self, provider_id: str) -> ValidationResult:
        """Re-check a stored key against the provider, right now."""
        credential = self.credential(provider_id)
        if credential is None:
            return ValidationResult(False, "No API key is stored for this provider.", "INVALID_API_KEY")
        return self.validate(provider_id, credential.key)

    # ------------------------------------------------------- active provider

    def active_id(self) -> str | None:
        return self._settings.active

    def set_active(self, provider_id: str) -> None:
        if catalog.provider_class(provider_id) is None:
            raise ValueError(f"unknown provider: {provider_id}")
        if not self.is_configured(provider_id):
            raise ProviderNotConfigured(
                f"{catalog.provider_class(provider_id).name} has no API key yet."
            )
        with self._lock:
            self._settings.active = provider_id
            self._save()

    def set_fallback(self, enabled: bool, order: list[str] | None = None) -> None:
        with self._lock:
            self._settings.fallback_enabled = bool(enabled)
            if order is not None:
                self._settings.fallback_order = [
                    pid for pid in order if catalog.provider_class(pid) is not None
                ]
            self._save()

    def fallback_chain(self) -> list[str]:
        """Providers to try after the active one fails, in order.

        Empty unless the user turned fallback on. Spending someone's credits
        with a provider they did not pick is opt-in, always.
        """
        if not self._settings.fallback_enabled:
            return []
        active = self._settings.active
        configured = [pid for pid in self.configured_ids() if pid != active]
        chosen = [pid for pid in self._settings.fallback_order if pid in configured]
        return chosen or configured

    def should_fall_back(self, error: ProviderError) -> bool:
        return self._settings.fallback_enabled and error.code in FALLBACK_CODES

    # ------------------------------------------------------------- the lock

    def is_unlocked(self) -> bool:
        """True when an AI request could actually be made."""
        active = self._settings.active
        return bool(active and self.is_configured(active))

    def lock_reason(self) -> str:
        """Why the agent is unusable, phrased for the person reading it."""
        configured = self.configured_ids()
        if not configured:
            return (
                "This agent needs an AI provider before it can do anything. "
                "Connect one with your own API key to continue."
            )
        active = self._settings.active
        if not active:
            names = ", ".join(catalog.provider_class(pid).name for pid in configured)
            return f"Choose which provider to use. Connected: {names}."
        provider_class = catalog.provider_class(active)
        name = provider_class.name if provider_class else active
        return f"{name} is selected but has no API key. Add one, or pick another provider."

    def require_active(self) -> AIProvider:
        """The only supported way to get a provider. Enforces the lock.

        Every AI request in the app passes through this, which is what makes
        the requirement a property of the service layer rather than of the UI.
        """
        if not self.is_unlocked():
            raise ProviderNotConfigured(self.lock_reason())
        return self.build(self._settings.active)

    # ------------------------------------------------------------------- UI

    def status(self) -> dict:
        """Everything the settings screen renders, with no key in it.

        The only credential-derived value here is the masked tail, which is
        four characters and exists so a user can tell two keys apart.
        """
        providers = []
        for provider_id in catalog.provider_ids():
            described = catalog.describe(provider_id)
            credential = self.credential(provider_id)
            described.update(
                connected=credential is not None,
                masked_key=credential.masked if credential else "",
                source=credential.source if credential else "",
                active=self._settings.active == provider_id,
                model=self.model_for(provider_id),
            )
            providers.append(described)

        return {
            "providers": providers,
            "active": self._settings.active,
            "unlocked": self.is_unlocked(),
            "lock_reason": "" if self.is_unlocked() else self.lock_reason(),
            "fallback_enabled": self._settings.fallback_enabled,
            "fallback_order": list(self._settings.fallback_order),
            "keystore": self.keystore_status(),
            "env_keys_allowed": self.allow_env,
        }


_shared: ProviderManager | None = None


def shared() -> ProviderManager:
    """The process-wide manager. One keystore, one active choice."""
    global _shared  # noqa: PLW0603 - one manager per process, by design
    if _shared is None:
        _shared = ProviderManager()
    return _shared


def use(manager: ProviderManager | None) -> None:
    """Install a manager. Test seam — the app never calls this."""
    global _shared  # noqa: PLW0603 - see shared()
    _shared = manager
