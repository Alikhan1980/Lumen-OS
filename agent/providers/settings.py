"""Which provider is active, which model each one uses, and nothing secret.

This file is deliberately boring and deliberately readable: it lives at
``providers.json`` next to the rest of the per-user data and contains only
choices. API keys are in the OS keystore — see keystore.py. Anyone who opens
this file should find nothing worth having, and :func:`sanity_check` asserts
that in the test suite.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..config import DATA_DIR

SETTINGS_PATH = DATA_DIR / "providers.json"


@dataclass
class ProviderSettings:
    """The user's provider choices."""

    active: str | None = None
    # provider id -> model id. Absent means "the provider's default".
    models: dict[str, str] = field(default_factory=dict)
    # Off unless the user turns it on. When off, a provider that is down is an
    # error the user sees, never a silent switch that spends other credits.
    fallback_enabled: bool = False
    # Which providers to try, in order, when fallback is on. Empty means the
    # remaining configured providers in registry order.
    fallback_order: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "active": self.active,
            "models": dict(self.models),
            "fallback_enabled": self.fallback_enabled,
            "fallback_order": list(self.fallback_order),
        }


def load(path: Path | None = None) -> ProviderSettings:
    target = path or SETTINGS_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ProviderSettings()
    if not isinstance(raw, dict):
        return ProviderSettings()

    models = raw.get("models")
    order = raw.get("fallback_order")
    return ProviderSettings(
        active=str(raw["active"]) if raw.get("active") else None,
        models={str(k): str(v) for k, v in models.items()} if isinstance(models, dict) else {},
        fallback_enabled=bool(raw.get("fallback_enabled")),
        fallback_order=[str(x) for x in order] if isinstance(order, list) else [],
    )


def save(settings: ProviderSettings, path: Path | None = None) -> None:
    """Write atomically, so a crash mid-write cannot leave a truncated file."""
    target = path or SETTINGS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(settings.as_dict(), indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def sanity_check(path: Path | None = None) -> list[str]:
    """Anything in the settings file that looks like a credential.

    Called by the test suite after every provider operation. An empty list is
    the only acceptable answer.
    """
    target = path or SETTINGS_PATH
    try:
        text = target.read_text(encoding="utf-8-sig")
    except OSError:
        return []
    found = []
    for marker in ("sk-", "AIza", "Bearer ", "api_key", "apiKey", "secret"):
        if marker in text:
            found.append(marker)
    return found
