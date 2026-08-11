"""The provider registry: the one list of who exists.

Adding a provider is three lines here plus its module. Nothing else in the app
enumerates providers — setup, settings, the CLI, the web UI, the lock and the
tests all read this.

    from .mistral_provider import MistralProvider
    register(MistralProvider)

The order below is the order the user sees them in.
"""

from __future__ import annotations

from .anthropic_provider import AnthropicProvider
from .base import AIProvider
from .gemini_provider import GeminiProvider
from .openai_provider import OpenAIProvider

_PROVIDERS: dict[str, type[AIProvider]] = {}


def register(provider: type[AIProvider]) -> type[AIProvider]:
    if not provider.id:
        raise ValueError(f"{provider.__name__} has no id")
    if provider.id in _PROVIDERS:
        raise ValueError(f"duplicate provider id: {provider.id}")
    _PROVIDERS[provider.id] = provider
    return provider


register(OpenAIProvider)
register(AnthropicProvider)
register(GeminiProvider)


def provider_ids() -> list[str]:
    return list(_PROVIDERS)


def provider_class(provider_id: str) -> type[AIProvider] | None:
    return _PROVIDERS.get(provider_id)


def all_providers() -> list[type[AIProvider]]:
    return list(_PROVIDERS.values())


def describe(provider_id: str) -> dict:
    """Everything the UI needs about a provider before any key exists."""
    provider = _PROVIDERS[provider_id]
    return {
        "id": provider.id,
        "name": provider.name,
        "console_url": provider.console_url,
        "key_hint": provider.key_hint,
        "env_var": provider.env_var,
        "billing_note": provider.billing_note,
        "models": [model.as_dict() for model in provider.catalog()],
        "default_model": provider.default_model(),
    }
