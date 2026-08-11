"""Google Gemini, over the Generative Language REST API with plain httpx.

The provider that least resembles the other two, and the reason the abstraction
has to be a translation layer rather than a thin wrapper:

* the key goes in an ``x-goog-api-key`` header — never in the query string,
  where it would end up in server logs and browser history;
* roles are ``user`` and ``model``, and the system prompt is a separate
  ``systemInstruction`` field rather than a message;
* function calls have **no ids**. A response is matched to its call by name, so
  ids are synthesised locally and stripped again on the way back out;
* tool schemas are an OpenAPI subset, not full JSON Schema — anything Gemini
  rejects is filtered out rather than passed through and 400'd on;
* reasoning is a token *budget*, not a level.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from .base import (
    BILLING_ERROR,
    CONTENT_REFUSED,
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
    split_system,
)

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
TIMEOUT = httpx.Timeout(600.0, connect=10.0)

# Our effort scale onto a thinking-token budget. -1 asks Gemini to decide.
_THINKING_BUDGET = {"low": 1024, "medium": 4096, "high": 12288, "xhigh": 24576, "max": -1}

# JSON Schema keywords the OpenAPI subset does not accept. Passing them through
# is a 400 on every request, so they are dropped from tool schemas here.
_SCHEMA_DROP = frozenset(
    {"$schema", "additionalProperties", "examples", "const", "definitions",
     "$defs", "$ref", "patternProperties", "exclusiveMinimum", "exclusiveMaximum",
     "if", "then", "else", "not", "allOf", "oneOf"}
)


class GeminiProvider(AIProvider):
    id = "gemini"
    name = "Google Gemini"
    console_url = "https://aistudio.google.com/apikey"
    key_hint = "AIza…"
    env_var = "GEMINI_API_KEY"
    billing_note = (
        "Billed by Google against your own Gemini API account. There is a free "
        "tier with low rate limits; paid usage needs billing enabled in Google "
        "AI Studio."
    )

    @classmethod
    def catalog(cls) -> list[ModelInfo]:
        return [
            ModelInfo("gemini-2.5-pro", "Gemini 2.5 Pro", 1_048_576, 65_536, 1.25, 10.00,
                      "Most capable; reasoning model."),
            ModelInfo("gemini-2.5-flash", "Gemini 2.5 Flash", 1_048_576, 65_536, 0.30, 2.50,
                      "Fast and inexpensive."),
            ModelInfo("gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite", 1_048_576, 65_536, 0.10, 0.40),
            ModelInfo("gemini-2.0-flash", "Gemini 2.0 Flash", 1_048_576, 8_192, 0.10, 0.40),
        ]

    def capabilities(self, model: str | None = None) -> Capabilities:
        wanted = model or self.model
        info = self.model_info(wanted)
        thinking = wanted.startswith("gemini-2.5")
        return Capabilities(
            streaming=True,
            tools=True,
            vision=True,
            thinking=thinking,
            system_prompt=True,
            mid_conversation_system=False,
            prompt_caching=False,  # explicit cached-content handles only
            effort=thinking,
            parallel_tool_calls=True,
            context_window=info.context_window if info else 1_048_576,
            max_output_tokens=info.max_output_tokens if info else 8_192,
        )

    @classmethod
    def check_format(cls, key: str) -> str | None:
        problem = super().check_format(key)
        if problem:
            return problem
        if not key.startswith("AIza"):
            return "A Gemini API key starts with AIza. That looks like a key for something else."
        if len(key) < 30:
            return "That key is too short to be a Gemini API key."
        return None

    # ------------------------------------------------------------------ wire

    def _headers(self) -> dict[str, str]:
        """The only place the key is used. Header, never the URL."""
        return {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=BASE_URL, timeout=TIMEOUT)

    def _fail(self, response: httpx.Response) -> ProviderError:
        status = response.status_code
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        error = payload.get("error") if isinstance(payload, dict) else {}
        error = error if isinstance(error, dict) else {}
        reason = str(error.get("status") or "")
        message = redact(error.get("message") or response.text or "")
        # The specific reason lives one level down, in the error details.
        detail_reason = ""
        for item in error.get("details") or []:
            if isinstance(item, dict) and item.get("reason"):
                detail_reason = str(item["reason"])
                break

        if status == 400 and detail_reason == "API_KEY_INVALID":
            return ProviderError(
                INVALID_API_KEY, "Google rejected that API key.",
                provider=self.id, status=status, detail=detail_reason,
            )
        if status == 401:
            return ProviderError(INVALID_API_KEY, provider=self.id, status=status, detail=reason)
        if status == 403:
            if "SERVICE_DISABLED" in (detail_reason or "") or "has not been used" in message:
                return ProviderError(
                    PERMISSION_DENIED,
                    "The Generative Language API is not enabled on the Google "
                    "Cloud project this key belongs to.",
                    provider=self.id, status=status, detail=detail_reason or reason,
                )
            return ProviderError(
                PERMISSION_DENIED,
                "That key is valid but is not permitted to use this model. Check "
                "the key's restrictions in Google AI Studio.",
                provider=self.id, status=status, detail=detail_reason or reason,
            )
        if status == 429:
            return ProviderError(
                RATE_LIMITED,
                "Gemini's rate limit was reached — the free tier is limited per "
                "minute and per day. Wait a moment, or enable billing.",
                provider=self.id, status=status, detail=detail_reason or reason,
            )
        if status == 404:
            return ProviderError(
                MODEL_UNAVAILABLE,
                f"Gemini does not offer {self.model!r} to this key.",
                provider=self.id, status=status, detail=reason,
            )
        if status in {500, 502, 503, 504}:
            return ProviderError(
                PROVIDER_UNAVAILABLE, "Gemini is having trouble right now. Try again shortly.",
                provider=self.id, status=status, detail=reason,
            )
        if status == 402 or "billing" in message.lower():
            return ProviderError(
                BILLING_ERROR,
                "Google reports a billing problem on the project behind this key.",
                provider=self.id, status=status, detail=reason,
            )
        if status == 400:
            if "token count" in message.lower() or "too long" in message.lower():
                return ProviderError(CONTEXT_TOO_LONG, provider=self.id, status=status, detail=reason)
            return ProviderError(
                INVALID_REQUEST, f"Gemini rejected the request: {message}",
                provider=self.id, status=status, detail=reason,
            )
        return ProviderError(
            UNKNOWN_PROVIDER_ERROR, f"Gemini returned {status}: {message}",
            provider=self.id, status=status, detail=reason,
        )

    def _network_error(self, exc: Exception) -> ProviderError:
        return ProviderError(
            NETWORK_ERROR, "Could not reach Google. Check your internet connection.",
            provider=self.id, detail=type(exc).__name__,
        )

    # ------------------------------------------------------------ validation

    def validate_key(self) -> ValidationResult:
        try:
            with self._client() as client:
                response = client.get(
                    "/models", headers=self._headers(), params={"pageSize": 200}, timeout=30.0
                )
        except httpx.HTTPError as exc:
            error = self._network_error(exc)
            return ValidationResult(False, error.message, error.code)

        if response.status_code != 200:
            error = self._fail(response)
            return ValidationResult(False, error.message, error.code)

        try:
            listed = response.json().get("models") or []
        except ValueError:
            listed = []
        known = {info.id: info for info in self.catalog()}
        models = []
        for item in listed:
            if not isinstance(item, dict):
                continue
            # Embedding and image models cannot answer a chat turn.
            if "generateContent" not in (item.get("supportedGenerationMethods") or []):
                continue
            name = str(item.get("name") or "").removeprefix("models/")
            if not name:
                continue
            models.append(
                known.get(name)
                or ModelInfo(
                    name,
                    str(item.get("displayName") or name),
                    int(item.get("inputTokenLimit") or 32_768),
                    int(item.get("outputTokenLimit") or 8_192),
                )
            )
        return ValidationResult(True, "Google accepted the key.", models=models)

    def live_models(self) -> list[ModelInfo]:
        result = self.validate_key()
        return result.models or self.catalog()

    # --------------------------------------------------------- serialisation

    def _contents(self, request: TurnRequest) -> list[dict]:
        out: list[dict] = []
        for message in split_system(request.messages):
            parts: list[dict] = []
            for block in message.blocks:
                if isinstance(block, Text):
                    if block.text:
                        parts.append({"text": block.text})
                elif isinstance(block, ToolUse):
                    parts.append({"functionCall": {"name": block.name, "args": block.input}})
                elif isinstance(block, ToolResult):
                    # Gemini wants a JSON object back, and pairs it to the call
                    # by name. An error is reported inside that object rather
                    # than with a flag, because there is no flag to set.
                    parts.append(
                        {
                            "functionResponse": {
                                "name": block.name or block.tool_use_id,
                                "response": (
                                    {"error": block.content} if block.is_error
                                    else {"result": block.content}
                                ),
                            }
                        }
                    )
            if not parts:
                continue
            out.append({"role": "model" if message.role == "assistant" else "user", "parts": parts})
        return out

    def _body(self, request: TurnRequest) -> dict:
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": self._contents(request),
            "generationConfig": {"maxOutputTokens": request.max_tokens},
        }
        if request.tools:
            declarations = [
                declaration
                for declaration in (_declaration(t) for t in request.tools)
                if declaration
            ]
            if declarations:
                body["tools"] = [{"functionDeclarations": declarations}]
        if self.capabilities(request.model).effort:
            config: dict[str, Any] = {"includeThoughts": bool(request.want_thinking)}
            if request.effort:
                config["thinkingBudget"] = _THINKING_BUDGET.get(request.effort, -1)
            body["generationConfig"]["thinkingConfig"] = config
        return body

    # ------------------------------------------------------------------ turn

    def stream(self, request: TurnRequest, sink: Sink) -> Turn:
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_uses: list[ToolUse] = []
        finish = ""
        usage = Usage()
        # Gemini has no call ids; the agent loop needs one to pair a result to
        # its call, so they are minted here and dropped again on the way back.
        counter = 0

        try:
            with self._client() as client, client.stream(
                "POST",
                f"/models/{request.model}:streamGenerateContent",
                params={"alt": "sse"},
                headers=self._headers(),
                json=self._body(request),
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise self._fail(response)
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if not chunk:
                        continue
                    try:
                        event = json.loads(chunk)
                    except ValueError:
                        continue

                    metadata = event.get("usageMetadata")
                    if isinstance(metadata, dict):
                        usage = _usage(metadata)

                    for candidate in event.get("candidates") or []:
                        finish = candidate.get("finishReason") or finish
                        for part in (candidate.get("content") or {}).get("parts") or []:
                            if "functionCall" in part:
                                call = part["functionCall"] or {}
                                counter += 1
                                arguments = call.get("args")
                                tool_uses.append(
                                    ToolUse(
                                        f"gemini_call_{counter}",
                                        str(call.get("name") or ""),
                                        arguments if isinstance(arguments, dict) else {},
                                    )
                                )
                                continue
                            piece = part.get("text")
                            if not piece:
                                continue
                            if part.get("thought"):
                                thinking_parts.append(piece)
                                sink.thinking(piece)
                            else:
                                text_parts.append(piece)
                                sink.text(piece)
        except ProviderError:
            raise
        except httpx.HTTPError as exc:
            raise self._network_error(exc) from exc

        blocks: list[Any] = []
        if thinking_parts:
            blocks.append(Thinking("".join(thinking_parts)))
        if text_parts:
            blocks.append(Text("".join(text_parts)))
        blocks.extend(tool_uses)

        stop = "end_turn"
        if tool_uses:
            stop = "tool_use"
        elif finish == "MAX_TOKENS":
            stop = "max_tokens"
        elif finish in {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}:
            stop = "refusal"

        if stop == "refusal" and not text_parts:
            raise ProviderError(
                CONTENT_REFUSED,
                "Gemini blocked this request under its safety filters.",
                provider=self.id, detail=finish,
            )

        return Turn(
            blocks=blocks,
            stop_reason=stop,
            usage=usage,
            provider=self.id,
            raw=None,
            refusal_detail=finish if stop == "refusal" else "",
        )


# ------------------------------------------------------------------- helpers


def _usage(raw: dict) -> Usage:
    cached = int(raw.get("cachedContentTokenCount") or 0)
    # Thinking tokens are billed as output but reported separately.
    output = int(raw.get("candidatesTokenCount") or 0) + int(raw.get("thoughtsTokenCount") or 0)
    return Usage(
        input_tokens=max(int(raw.get("promptTokenCount") or 0) - cached, 0),
        output_tokens=output,
        cache_read_tokens=cached,
    )


def _clean_schema(schema: Any) -> Any:
    """A JSON Schema reduced to the OpenAPI subset Gemini accepts.

    Unknown keywords are dropped rather than translated: a tool that loses
    `additionalProperties: false` still works, whereas one that sends it fails
    the whole request.
    """
    if isinstance(schema, list):
        return [_clean_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _SCHEMA_DROP:
            continue
        if key == "type" and isinstance(value, str):
            out["type"] = value.upper()
            continue
        if key == "type" and isinstance(value, list):
            # ["string", "null"] — take the first real type; Gemini has no unions.
            concrete = [t for t in value if isinstance(t, str) and t != "null"]
            out["type"] = (concrete[0] if concrete else "string").upper()
            out["nullable"] = "null" in value
            continue
        if key == "properties" and isinstance(value, dict):
            # A property *name* is not a schema keyword — recurse into the
            # values, not the mapping, or nested types are never converted.
            out[key] = {name: _clean_schema(sub) for name, sub in value.items()}
            continue
        if key == "items":
            out[key] = _clean_schema(value)
            continue
        out[key] = value
    return out


def _declaration(tool: Any) -> dict | None:
    """One tool as a Gemini functionDeclaration.

    A parameterless tool must omit `parameters` entirely — an object schema
    with no properties is rejected.
    """
    schema = _clean_schema(tool.input_schema)
    declaration: dict[str, Any] = {
        "name": tool.name,
        "description": tool.description,
    }
    if isinstance(schema, dict) and schema.get("properties"):
        if not schema.get("required"):
            schema.pop("required", None)
        declaration["parameters"] = schema
    return declaration
