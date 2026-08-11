"""Anthropic, through the official SDK.

The one provider that uses a vendor SDK rather than raw HTTP: it is already a
dependency, and it carries the pieces that are awkward to hand-roll — prompt
cache breakpoints, extended thinking with its signatures, and the streaming
helper. An assistant turn keeps the SDK's own content blocks in `Turn.raw`, so
replaying history to Anthropic is byte-identical to what it produced, thinking
signatures included.
"""

from __future__ import annotations

from typing import Any

import anthropic

from .base import (
    BILLING_ERROR,
    CONTEXT_TOO_LONG,
    INVALID_API_KEY,
    INVALID_REQUEST,
    MODEL_UNAVAILABLE,
    NETWORK_ERROR,
    PERMISSION_DENIED,
    PROVIDER_UNAVAILABLE,
    RATE_LIMITED,
    UNKNOWN_PROVIDER_ERROR,
    AIProvider,
    Capabilities,
    Message,
    ModelInfo,
    ProviderError,
    Sink,
    Text,
    Thinking,
    ToolResult,
    ToolUse,
    Turn,
    TurnRequest,
    Usage,
    ValidationResult,
    redact,
)

# A single model response can run for minutes with extended thinking, so the
# read timeout has to be generous; it matches the SDK's own default.
TIMEOUT = 600.0

# Models that accept a mid-conversation {"role": "system"} message. The rest
# take the same note folded into the user turn instead; sending one to a model
# that does not support it is a 400.
MID_CONVERSATION_SYSTEM = frozenset(
    {"claude-opus-5", "claude-opus-4-8", "claude-fable-5", "claude-mythos-5"}
)


class AnthropicProvider(AIProvider):
    id = "anthropic"
    name = "Anthropic"
    console_url = "https://console.anthropic.com/settings/keys"
    key_hint = "sk-ant-…"
    env_var = "ANTHROPIC_API_KEY"
    billing_note = (
        "Billed per token by Anthropic against your own account. Separate from "
        "a Claude.ai subscription — the account needs credit."
    )

    @classmethod
    def catalog(cls) -> list[ModelInfo]:
        return [
            ModelInfo("claude-sonnet-5", "Claude Sonnet 5", 200_000, 64_000, 3.00, 15.00,
                      "Best balance for tool-driven work."),
            ModelInfo("claude-opus-5", "Claude Opus 5", 200_000, 64_000, 5.00, 25.00,
                      "Most capable; slower and dearer."),
            ModelInfo("claude-opus-4-8", "Claude Opus 4.8", 200_000, 64_000, 5.00, 25.00),
            ModelInfo("claude-sonnet-4-6", "Claude Sonnet 4.6", 200_000, 64_000, 3.00, 15.00),
            ModelInfo("claude-haiku-4-5-20251001", "Claude Haiku 4.5", 200_000, 32_000, 1.00, 5.00,
                      "Fastest and cheapest."),
        ]

    def capabilities(self, model: str | None = None) -> Capabilities:
        wanted = model or self.model
        info = self.model_info(wanted)
        return Capabilities(
            streaming=True,
            tools=True,
            vision=True,
            thinking=True,
            system_prompt=True,
            mid_conversation_system=wanted in MID_CONVERSATION_SYSTEM,
            prompt_caching=True,
            effort=True,
            parallel_tool_calls=True,
            context_window=info.context_window if info else 200_000,
            max_output_tokens=info.max_output_tokens if info else 32_000,
        )

    @classmethod
    def check_format(cls, key: str) -> str | None:
        problem = super().check_format(key)
        if problem:
            return problem
        if not key.startswith("sk-ant-"):
            return "An Anthropic key starts with sk-ant-. That looks like a key for something else."
        if len(key) < 30:
            return "That key is too short to be an Anthropic key."
        return None

    # ------------------------------------------------------------------ wire

    def _client(self) -> anthropic.Anthropic:
        return anthropic.Anthropic(api_key=self._api_key, timeout=TIMEOUT)

    def _fail(self, exc: Exception) -> ProviderError:
        """Map an SDK exception onto a normalised code.

        Every branch here is a different thing to tell the user; collapsing
        them into "invalid API key" is the failure mode this exists to avoid.
        """
        detail = ""
        status = getattr(exc, "status_code", None)
        raw = redact(getattr(exc, "message", "") or exc)

        if isinstance(exc, anthropic.AuthenticationError):
            return ProviderError(INVALID_API_KEY, provider=self.id, status=status, detail=raw)
        if isinstance(exc, anthropic.PermissionDeniedError):
            return ProviderError(
                PERMISSION_DENIED,
                "Anthropic accepted the key but refused this request. Check the "
                "key's permissions and that the account is in good standing.",
                provider=self.id, status=status, detail=raw,
            )
        if isinstance(exc, anthropic.RateLimitError):
            return ProviderError(
                RATE_LIMITED,
                "Anthropic's rate limit was reached. Wait a moment and try again.",
                provider=self.id, status=status, detail=raw,
            )
        if isinstance(exc, anthropic.NotFoundError):
            return ProviderError(
                MODEL_UNAVAILABLE,
                f"Anthropic does not offer {self.model!r} to this account.",
                provider=self.id, status=status, detail=raw,
            )
        if isinstance(exc, anthropic.APIConnectionError):
            return ProviderError(
                NETWORK_ERROR, "Could not reach Anthropic. Check your internet connection.",
                provider=self.id, detail=type(exc).__name__,
            )
        if isinstance(exc, anthropic.APIStatusError):
            if status == 402:
                return ProviderError(
                    BILLING_ERROR,
                    "Anthropic reports a billing problem — the account is likely out of credit.",
                    provider=self.id, status=status, detail=raw,
                )
            if status == 400 and "credit balance" in raw.lower():
                return ProviderError(
                    BILLING_ERROR,
                    "Your Anthropic account is out of credit. Top it up at "
                    "console.anthropic.com/settings/billing.",
                    provider=self.id, status=status, detail=raw,
                )
            if status == 400 and ("too long" in raw.lower() or "max_tokens" in raw.lower()):
                return ProviderError(CONTEXT_TOO_LONG, provider=self.id, status=status, detail=raw)
            if status and status >= 500:
                return ProviderError(
                    PROVIDER_UNAVAILABLE,
                    "Anthropic is having trouble right now. Try again shortly.",
                    provider=self.id, status=status, detail=raw,
                )
            if status == 400:
                return ProviderError(
                    INVALID_REQUEST, f"Anthropic rejected the request: {raw}",
                    provider=self.id, status=status,
                )
            detail = raw
        return ProviderError(
            UNKNOWN_PROVIDER_ERROR, f"Anthropic returned an unexpected error: {redact(exc, 160)}",
            provider=self.id, status=status, detail=detail,
        )

    # ------------------------------------------------------------ validation

    def validate_key(self) -> ValidationResult:
        """Cheapest round trip that proves a key works: list one model."""
        try:
            page = self._client().models.list(limit=20)
        except Exception as exc:
            error = self._fail(exc)
            return ValidationResult(False, error.message, error.code)

        known = {info.id: info for info in self.catalog()}
        models = [
            known.get(item.id) or ModelInfo(item.id, getattr(item, "display_name", item.id), 200_000, 32_000)
            for item in getattr(page, "data", [])
        ]
        return ValidationResult(True, "Anthropic accepted the key.", models=models)

    def live_models(self) -> list[ModelInfo]:
        result = self.validate_key()
        return result.models or self.catalog()

    # --------------------------------------------------------- serialisation

    def _system_blocks(self, system: str) -> list[dict]:
        # One cache breakpoint, on the system prompt, which renders after the
        # tool definitions — so a long tool-calling turn re-reads its own
        # prefix instead of paying for it again.
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    def _serialise(self, messages: list[Message], supports_system: bool) -> list[dict]:
        from .base import split_system

        source = messages if supports_system else split_system(messages)
        out: list[dict] = []
        for message in source:
            if message.role == "system":
                out.append({"role": "system", "content": message.text()})
                continue
            # Our own previous turn, replayed exactly — thinking signatures and
            # all. Anything from another provider is rebuilt from blocks below.
            if message.role == "assistant" and message.provider == self.id and message.raw is not None:
                out.append({"role": "assistant", "content": message.raw})
                continue
            content = [block for block in (_encode(b) for b in message.blocks) if block]
            if not content:
                continue
            out.append({"role": message.role, "content": content})
        return out

    def _params(self, request: TurnRequest) -> dict:
        supports_system = request.model in MID_CONVERSATION_SYSTEM
        params: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "system": self._system_blocks(request.system),
            "messages": self._serialise(request.messages, supports_system),
            "thinking": {"type": "adaptive", "display": "summarized"},
            # Auto-caches the last cacheable block, so history is re-read rather
            # than re-billed as it grows.
            "cache_control": {"type": "ephemeral"},
        }
        if request.tools:
            params["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in request.tools
            ]
        if request.effort:
            params["output_config"] = {"effort": request.effort}
        return params

    # ------------------------------------------------------------------ turn

    def stream(self, request: TurnRequest, sink: Sink) -> Turn:
        client = self._client()
        try:
            with client.messages.stream(**self._params(request)) as stream:
                for event in stream:
                    if event.type != "content_block_delta":
                        continue
                    if event.delta.type == "text_delta":
                        sink.text(event.delta.text)
                    elif event.delta.type == "thinking_delta":
                        sink.thinking(event.delta.thinking)
                message = stream.get_final_message()
        except anthropic.APIError as exc:
            raise self._fail(exc) from exc
        except ProviderError:
            raise
        except Exception as exc:  # transport-level failures the SDK lets through
            raise ProviderError(
                NETWORK_ERROR, "Could not reach Anthropic. Check your internet connection.",
                provider=self.id, detail=type(exc).__name__,
            ) from exc

        stop = {
            "tool_use": "tool_use",
            "max_tokens": "max_tokens",
            "refusal": "refusal",
            "pause_turn": "pause",
        }.get(message.stop_reason or "", "end_turn")

        blocks = []
        for block in message.content:
            kind = getattr(block, "type", "")
            if kind == "text" and block.text:
                blocks.append(Text(block.text))
            elif kind == "thinking":
                blocks.append(Thinking(getattr(block, "thinking", "")))
            elif kind == "tool_use":
                blocks.append(ToolUse(block.id, block.name, block.input if isinstance(block.input, dict) else {}))

        raw_usage = message.usage
        usage = Usage(
            input_tokens=getattr(raw_usage, "input_tokens", 0) or 0,
            output_tokens=getattr(raw_usage, "output_tokens", 0) or 0,
            cache_write_tokens=getattr(raw_usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(raw_usage, "cache_read_input_tokens", 0) or 0,
        )

        details = getattr(message, "stop_details", None)
        return Turn(
            blocks=blocks,
            stop_reason=stop,
            usage=usage,
            provider=self.id,
            raw=message.content,
            refusal_detail=str(getattr(details, "category", "") or "") if details else "",
        )


def _encode(block: Any) -> dict | None:
    if isinstance(block, Text):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUse):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResult):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
            "is_error": block.is_error,
        }
    # Thinking is never replayed from normalised blocks: without its original
    # signature Anthropic rejects it, and a summary is not valid input.
    return None
