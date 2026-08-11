"""OpenAI, over the Chat Completions API with plain httpx.

No vendor SDK: httpx is already here for the search backends, the wire format
is stable, and doing it directly keeps full control over error normalisation —
which matters because OpenAI signals a dead key, an exhausted quota and an
overloaded model with three different shapes that all look alike at a glance.

Where this differs from Anthropic, and why the abstraction cannot flatten it:

* tool calls carry their arguments as a JSON *string* that arrives in
  fragments across stream chunks, so they are reassembled before use;
* there is no mid-conversation system role, so the agent's context note is
  folded into the user turn;
* reasoning models take `max_completion_tokens` and `reasoning_effort` where
  older ones take `max_tokens` and neither.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

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
    ToolResult,
    ToolUse,
    Turn,
    TurnRequest,
    Usage,
    ValidationResult,
    redact,
    split_system,
)

BASE_URL = "https://api.openai.com/v1"
TIMEOUT = httpx.Timeout(600.0, connect=10.0)

# Families that take `max_completion_tokens` and a reasoning knob. Matched by
# prefix so a point release does not need a code change.
_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")

# Our five-step effort scale onto OpenAI's four. xhigh and max both saturate:
# saying so here is better than pretending the scales match.
_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


class OpenAIProvider(AIProvider):
    id = "openai"
    name = "OpenAI"
    console_url = "https://platform.openai.com/api-keys"
    key_hint = "sk-…"
    env_var = "OPENAI_API_KEY"
    billing_note = (
        "Billed per token by OpenAI against your own account. Needs a payment "
        "method or prepaid credit on the platform account — a ChatGPT Plus "
        "subscription does not cover API usage."
    )

    @classmethod
    def catalog(cls) -> list[ModelInfo]:
        # A starting point for the picker, not a limit: the setup screen reads
        # the account's real model list, and any id can be typed in by hand.
        return [
            ModelInfo("gpt-5.1", "GPT-5.1", 400_000, 128_000, 1.25, 10.00,
                      "Strong default for tool-driven work."),
            ModelInfo("gpt-5", "GPT-5", 400_000, 128_000, 1.25, 10.00),
            ModelInfo("gpt-5-mini", "GPT-5 mini", 400_000, 128_000, 0.25, 2.00,
                      "Cheaper and faster."),
            ModelInfo("gpt-4.1", "GPT-4.1", 1_047_576, 32_768, 2.00, 8.00),
            ModelInfo("gpt-4o", "GPT-4o", 128_000, 16_384, 2.50, 10.00),
            ModelInfo("o4-mini", "o4-mini", 200_000, 100_000, 1.10, 4.40,
                      "Reasoning model."),
        ]

    def capabilities(self, model: str | None = None) -> Capabilities:
        wanted = model or self.model
        info = self.model_info(wanted)
        reasoning = _is_reasoning(wanted)
        return Capabilities(
            streaming=True,
            tools=True,
            vision=True,
            # Reasoning tokens are billed but the text is not returned, so the
            # agent must not promise the user a thinking trace here.
            thinking=False,
            system_prompt=True,
            mid_conversation_system=False,
            prompt_caching=True,  # automatic, no breakpoints to place
            effort=reasoning,
            parallel_tool_calls=True,
            context_window=info.context_window if info else 128_000,
            max_output_tokens=info.max_output_tokens if info else 16_384,
        )

    @classmethod
    def check_format(cls, key: str) -> str | None:
        problem = super().check_format(key)
        if problem:
            return problem
        if not key.startswith("sk-"):
            return "An OpenAI key starts with sk-. That looks like a key for something else."
        if key.startswith("sk-ant-"):
            return "That is an Anthropic key. Add it under Anthropic instead."
        if len(key) < 20:
            return "That key is too short to be an OpenAI key."
        return None

    # ------------------------------------------------------------------ wire

    def _headers(self) -> dict[str, str]:
        """The only place the key is used. Never logged, never echoed."""
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=BASE_URL, timeout=TIMEOUT)

    def _fail(self, response: httpx.Response) -> ProviderError:
        """Map an HTTP failure onto a normalised code.

        OpenAI puts the useful discriminator in `error.code` rather than in the
        status, so a 429 that means "out of credit" is told apart from a 429
        that means "slow down".
        """
        status = response.status_code
        try:
            payload = response.json().get("error") or {}
        except ValueError:
            payload = {}
        code = str(payload.get("code") or "")
        kind = str(payload.get("type") or "")
        message = redact(payload.get("message") or response.text or "")

        if status == 401:
            return ProviderError(
                INVALID_API_KEY, "OpenAI rejected that key.",
                provider=self.id, status=status, detail=code or kind,
            )
        if status == 403:
            return ProviderError(
                PERMISSION_DENIED,
                "That key is valid but is not permitted to use this model or "
                "endpoint. Check the key's project and scopes.",
                provider=self.id, status=status, detail=code or kind,
            )
        if status == 429 or code in {"insufficient_quota", "billing_hard_limit_reached"}:
            if code in {"insufficient_quota", "billing_hard_limit_reached"}:
                return ProviderError(
                    BILLING_ERROR,
                    "Your OpenAI account has no credit left for API use. Add a "
                    "payment method or top up at platform.openai.com/billing.",
                    provider=self.id, status=status, detail=code,
                )
            return ProviderError(
                RATE_LIMITED,
                "OpenAI's rate limit was reached. Wait a moment and try again.",
                provider=self.id, status=status, detail=code or kind,
            )
        if status == 402:
            return ProviderError(BILLING_ERROR, provider=self.id, status=status, detail=code)
        if status == 404 or code == "model_not_found":
            return ProviderError(
                MODEL_UNAVAILABLE,
                f"OpenAI does not offer {self.model!r} to this account. Some "
                "models need a verified organisation.",
                provider=self.id, status=status, detail=code,
            )
        if status in {500, 502, 503, 504}:
            return ProviderError(
                PROVIDER_UNAVAILABLE, "OpenAI is having trouble right now. Try again shortly.",
                provider=self.id, status=status, detail=code or kind,
            )
        if code == "context_length_exceeded":
            return ProviderError(CONTEXT_TOO_LONG, provider=self.id, status=status, detail=code)
        if status == 400:
            return ProviderError(
                INVALID_REQUEST, f"OpenAI rejected the request: {message}",
                provider=self.id, status=status, detail=code or kind,
            )
        return ProviderError(
            UNKNOWN_PROVIDER_ERROR, f"OpenAI returned {status}: {message}",
            provider=self.id, status=status, detail=code or kind,
        )

    def _network_error(self, exc: Exception) -> ProviderError:
        return ProviderError(
            NETWORK_ERROR, "Could not reach OpenAI. Check your internet connection.",
            provider=self.id, detail=type(exc).__name__,
        )

    # ------------------------------------------------------------ validation

    def validate_key(self) -> ValidationResult:
        try:
            with self._client() as client:
                response = client.get("/models", headers=self._headers(), timeout=30.0)
        except httpx.HTTPError as exc:
            error = self._network_error(exc)
            return ValidationResult(False, error.message, error.code)

        if response.status_code != 200:
            error = self._fail(response)
            return ValidationResult(False, error.message, error.code)

        try:
            data = response.json().get("data") or []
        except ValueError:
            data = []
        known = {info.id: info for info in self.catalog()}
        # Fine-tunes, embeddings, audio and image models are not chat models;
        # showing them in the picker would just be a way to pick a broken one.
        ids = sorted(
            str(item.get("id"))
            for item in data
            if isinstance(item, dict) and _looks_like_chat_model(str(item.get("id") or ""))
        )
        models = [known.get(name) or ModelInfo(name, name, 128_000, 16_384) for name in ids]
        return ValidationResult(True, "OpenAI accepted the key.", models=models)

    def live_models(self) -> list[ModelInfo]:
        result = self.validate_key()
        return result.models or self.catalog()

    # --------------------------------------------------------- serialisation

    def _serialise(self, request: TurnRequest) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": request.system}]
        for message in split_system(request.messages):
            if message.role == "assistant":
                out.extend(_assistant_payload(message, self.id))
                continue
            # Tool results are their own top-level messages here, not blocks
            # inside a user turn, so a mixed turn has to be taken apart.
            text_parts: list[str] = []
            for block in message.blocks:
                if isinstance(block, ToolResult):
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.tool_use_id,
                            "content": block.content,
                        }
                    )
                elif isinstance(block, Text):
                    text_parts.append(block.text)
            if text_parts:
                out.append({"role": "user", "content": "\n".join(text_parts)})
        return out

    def _body(self, request: TurnRequest) -> dict:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": self._serialise(request),
            "stream": True,
            # Without this the final chunk carries no usage and /cost would
            # silently under-report every turn.
            "stream_options": {"include_usage": True},
        }
        if _is_reasoning(request.model):
            body["max_completion_tokens"] = request.max_tokens
            if request.effort:
                body["reasoning_effort"] = _EFFORT.get(request.effort, "medium")
        else:
            body["max_tokens"] = request.max_tokens
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in request.tools
            ]
        return body

    # ------------------------------------------------------------------ turn

    def stream(self, request: TurnRequest, sink: Sink) -> Turn:
        text_parts: list[str] = []
        # Keyed by the `index` OpenAI assigns each call in the turn; id, name
        # and arguments all arrive separately and in pieces.
        calls: dict[int, dict] = {}
        finish = ""
        usage = Usage()

        try:
            with self._client() as client, client.stream(
                "POST", "/chat/completions", headers=self._headers(), json=self._body(request)
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise self._fail(response)
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if not chunk or chunk == "[DONE]":
                        continue
                    try:
                        event = json.loads(chunk)
                    except ValueError:
                        continue

                    if isinstance(event.get("usage"), dict):
                        usage = _usage(event["usage"])

                    for choice in event.get("choices") or []:
                        finish = choice.get("finish_reason") or finish
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            text_parts.append(piece)
                            sink.text(piece)
                        for call in delta.get("tool_calls") or []:
                            _accumulate(calls, call)
        except ProviderError:
            raise
        except httpx.HTTPError as exc:
            raise self._network_error(exc) from exc

        blocks: list[Any] = []
        if text_parts:
            blocks.append(Text("".join(text_parts)))
        for index in sorted(calls):
            entry = calls[index]
            try:
                arguments = json.loads(entry["arguments"] or "{}")
            except ValueError:
                # A truncated or malformed argument string is the model's
                # mistake to see, not a crash: hand it back as an empty call
                # and let the tool layer report what was wrong.
                arguments = {}
            blocks.append(
                ToolUse(entry["id"] or f"call_{index}", entry["name"], arguments if isinstance(arguments, dict) else {})
            )

        stop = {
            "tool_calls": "tool_use",
            "length": "max_tokens",
            "content_filter": "refusal",
        }.get(finish, "end_turn")
        if any(isinstance(b, ToolUse) for b in blocks):
            stop = "tool_use"

        return Turn(
            blocks=blocks,
            stop_reason=stop,
            usage=usage,
            provider=self.id,
            # Rebuilt from the blocks on replay; the wire format holds nothing
            # the normalised form loses.
            raw=None,
            refusal_detail="content_filter" if finish == "content_filter" else "",
        )


# ------------------------------------------------------------------- helpers


def _is_reasoning(model: str) -> bool:
    return any(model.startswith(prefix) for prefix in _REASONING_PREFIXES)


def _looks_like_chat_model(model_id: str) -> bool:
    if not model_id:
        return False
    excluded = ("embedding", "whisper", "tts", "dall-e", "moderation", "audio",
                "realtime", "image", "transcribe", "search", "codex")
    if any(word in model_id for word in excluded):
        return False
    return model_id.startswith(("gpt-", "o1", "o3", "o4", "chatgpt-"))


def _usage(raw: dict) -> Usage:
    details = raw.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0)
    return Usage(
        # Cached input is reported inside prompt_tokens, so it is subtracted
        # out to stop the estimate charging for it at the full rate twice.
        input_tokens=max(int(raw.get("prompt_tokens") or 0) - cached, 0),
        output_tokens=int(raw.get("completion_tokens") or 0),
        cache_read_tokens=cached,
    )


def _accumulate(calls: dict[int, dict], fragment: dict) -> None:
    index = int(fragment.get("index") or 0)
    entry = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
    if fragment.get("id"):
        entry["id"] = fragment["id"]
    function = fragment.get("function") or {}
    if function.get("name"):
        entry["name"] = function["name"]
    if function.get("arguments"):
        entry["arguments"] += function["arguments"]


def _assistant_payload(message: Message, provider_id: str) -> list[dict]:
    """One assistant turn as OpenAI wants it back.

    Content and tool calls travel in the same message, which is the opposite of
    the tool-result case, so this cannot be folded into the loop above.
    """
    text = "".join(b.text for b in message.blocks if isinstance(b, Text))
    tool_calls = [
        {
            "id": b.id,
            "type": "function",
            "function": {"name": b.name, "arguments": json.dumps(b.input, ensure_ascii=False)},
        }
        for b in message.blocks
        if isinstance(b, ToolUse)
    ]
    if not text and not tool_calls:
        return []
    payload: dict[str, Any] = {"role": "assistant", "content": text or None}
    if tool_calls:
        payload["tool_calls"] = tool_calls
    return [payload]
