"""Multi-provider AI access: bring your own key.

The app has no API key of its own and no way to acquire one. A user connects a
provider they hold an account with, the key goes into the operating system's
credential store, and every AI request in the app is made from this machine
straight to that provider.

    Agent
      ↓
    ProviderManager      ← the lock: no key, no request
      ↓
    AIProvider           ← the abstraction
      ↓
    OpenAI / Anthropic / Gemini / …

Start at :mod:`agent.providers.base` for the interface a new provider has to
implement, and at :mod:`agent.providers.manager` for the lock.
"""

from __future__ import annotations

from .base import (
    AIProvider,
    Capabilities,
    Message,
    ModelInfo,
    ProviderError,
    ProviderNotConfigured,
    Sink,
    Text,
    Thinking,
    ToolDef,
    ToolResult,
    ToolUse,
    Turn,
    TurnRequest,
    Usage,
    ValidationResult,
    mask,
    redact,
)
from .catalog import all_providers, describe, provider_class, provider_ids
from .manager import ProviderManager, shared

__all__ = [
    "AIProvider",
    "Capabilities",
    "Message",
    "ModelInfo",
    "ProviderError",
    "ProviderManager",
    "ProviderNotConfigured",
    "Sink",
    "Text",
    "Thinking",
    "ToolDef",
    "ToolResult",
    "ToolUse",
    "Turn",
    "TurnRequest",
    "Usage",
    "ValidationResult",
    "all_providers",
    "describe",
    "mask",
    "provider_class",
    "provider_ids",
    "redact",
    "shared",
]
