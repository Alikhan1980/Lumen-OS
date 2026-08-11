"""The provider abstraction: one shape for a turn, three very different APIs.

Everything above this line — the agent loop, the tools, the UI — speaks the
vocabulary defined here and never imports a vendor SDK. Everything below it is
one module per provider that translates in both directions.

The translation is deliberately *not* a lowest-common-denominator API. Two
things keep provider-specific behaviour intact:

* :class:`Capabilities` — the agent asks what a provider can do rather than
  assuming. Mid-conversation system messages, prompt caching and reasoning
  effort all exist on some providers and not others.
* :attr:`Message.raw` — an assistant turn keeps the exact bytes its own
  provider produced. Sent back to that provider it round-trips losslessly,
  carrying thinking signatures and reasoning items the normalised blocks cannot
  express. Sent to a *different* provider (after a switch, or a fallback) it is
  rebuilt from the normalised blocks instead, which is lossy but always valid.

Credentials never appear in anything defined here: not in ``repr``, not in an
error, not in a log record. A provider holds its key in one private attribute
and puts it in exactly one place — the ``Authorization``-equivalent header of a
request to that provider's own API.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

# --------------------------------------------------------------------- errors

# Normalised failure codes. Providers map their own error shapes onto these so
# the rest of the app can react to a rate limit without knowing whose it is.
INVALID_API_KEY = "INVALID_API_KEY"
PERMISSION_DENIED = "PERMISSION_DENIED"
RATE_LIMITED = "RATE_LIMITED"
BILLING_ERROR = "BILLING_ERROR"
NETWORK_ERROR = "NETWORK_ERROR"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
INVALID_REQUEST = "INVALID_REQUEST"
CONTEXT_TOO_LONG = "CONTEXT_TOO_LONG"
CONTENT_REFUSED = "CONTENT_REFUSED"
UNKNOWN_PROVIDER_ERROR = "UNKNOWN_PROVIDER_ERROR"

# Codes worth trying a different provider for, when the user has explicitly
# turned automatic fallback on. A bad key is not among them: another provider
# would not fix it, and spending someone else's credits to paper over a
# fixable configuration mistake is exactly what fallback must not do.
FALLBACK_CODES = frozenset({PROVIDER_UNAVAILABLE, RATE_LIMITED, NETWORK_ERROR})

# What each code means to someone who is not holding the API docs. Providers
# may pass a more specific message; this is the floor.
DEFAULT_MESSAGES = {
    INVALID_API_KEY: "The API key was rejected. Check it, or paste a new one.",
    PERMISSION_DENIED: "That key is valid but is not allowed to do this. "
    "Check the key's permissions on the provider's dashboard.",
    RATE_LIMITED: "The provider's rate limit was reached. Wait a moment and try again.",
    BILLING_ERROR: "The provider reports a billing or quota problem on this account.",
    NETWORK_ERROR: "Could not reach the provider. Check your internet connection.",
    PROVIDER_UNAVAILABLE: "The provider is temporarily unavailable.",
    MODEL_UNAVAILABLE: "That model is not available to this account.",
    INVALID_REQUEST: "The provider rejected the request.",
    CONTEXT_TOO_LONG: "The conversation is longer than this model can hold. "
    "Start a fresh conversation.",
    CONTENT_REFUSED: "The model declined to answer this request.",
    UNKNOWN_PROVIDER_ERROR: "The provider returned an unexpected error.",
}


class ProviderError(RuntimeError):
    """A provider failure, normalised.

    `message` is safe to show a user and safe to log: providers construct it
    from their own error text, which is scrubbed of anything key-shaped by
    :func:`redact` before it gets here.
    """

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        provider: str = "",
        status: int | None = None,
        detail: str = "",
    ):
        self.code = code
        self.message = message or DEFAULT_MESSAGES.get(code, DEFAULT_MESSAGES[UNKNOWN_PROVIDER_ERROR])
        self.provider = provider
        self.status = status
        # A short technical note (an upstream error type, say). Never a body
        # dump, never a header.
        self.detail = detail
        super().__init__(self.message)

    @property
    def retryable(self) -> bool:
        return self.code in FALLBACK_CODES

    def __str__(self) -> str:
        return self.message

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "provider": self.provider,
            "detail": self.detail,
        }


class ProviderNotConfigured(RuntimeError):
    """No usable provider. The one error the whole app is gated on.

    Raised by the manager rather than by a provider — there is no provider to
    raise it. Callers turn this into "connect a provider", never into a request.
    """


# ------------------------------------------------------------------ redaction

# Anything shaped like a credential, whatever provider it came from. Applied to
# every string that leaves a provider module for a log, an error or the UI, so a
# key echoed back inside an upstream error body cannot escape through it.
_SECRET_SHAPES = (
    ("sk-ant-", 20),
    ("sk-proj-", 20),
    ("sk-", 20),
    ("AIza", 30),
    ("gsk_", 20),
    ("Bearer ", 20),
)


def redact(text: Any, limit: int = 300) -> str:
    """Scrub credential-shaped runs out of text, then trim it.

    Deliberately blunt: it would rather mangle an innocent token than let a key
    through. Everything a provider puts in a ProviderError goes through here.
    """
    out = str(text)
    for marker, run in _SECRET_SHAPES:
        start = 0
        while True:
            index = out.find(marker, start)
            if index < 0:
                break
            end = index + len(marker)
            while end < len(out) and (out[end].isalnum() or out[end] in "-_"):
                end += 1
            if end - index >= run:
                out = out[:index] + "[REDACTED]" + out[end:]
                start = index + len("[REDACTED]")
            else:
                start = end
    out = " ".join(out.split())
    return out if len(out) <= limit else out[: limit - 1] + "…"


def mask(key: str) -> str:
    """How a stored credential is shown: last four characters, nothing else."""
    tail = (key or "")[-4:]
    return "••••••••••••••••" + tail if tail else "••••••••••••••••"


# ------------------------------------------------------------- conversation

Role = Literal["user", "assistant", "system"]


@dataclass
class Text:
    text: str


@dataclass
class Thinking:
    """Reasoning the model chose to show. Not every provider emits it."""

    text: str


@dataclass
class ToolUse:
    id: str
    name: str
    input: dict


@dataclass
class ToolResult:
    tool_use_id: str
    content: str
    is_error: bool = False
    # Gemini matches a function response to its call by name, not by id.
    name: str = ""


Block = Text | Thinking | ToolUse | ToolResult


@dataclass
class Message:
    role: Role
    blocks: list[Block] = field(default_factory=list)
    # Which provider produced this assistant turn, and its own serialisation of
    # it. Only that provider may replay `raw`; see the module docstring.
    provider: str | None = None
    raw: Any = None

    def text(self) -> str:
        return "".join(b.text for b in self.blocks if isinstance(b, Text))

    def tool_uses(self) -> list[ToolUse]:
        return [b for b in self.blocks if isinstance(b, ToolUse)]


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    input_schema: dict


@dataclass
class TurnRequest:
    model: str
    system: str
    messages: list[Message]
    tools: list[ToolDef] = field(default_factory=list)
    max_tokens: int = 32_000
    # low | medium | high | xhigh | max, or None for the provider's default.
    # Each provider maps this onto its own knob rather than pretending the
    # scales are the same.
    effort: str | None = None
    want_thinking: bool = False


StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "pause"]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.cache_read_tokens += other.cache_read_tokens

    def estimate_usd(self, price_in: float, price_out: float) -> float:
        """A local estimate only. The provider's own dashboard is the truth."""
        return (
            self.input_tokens * price_in
            + self.cache_write_tokens * price_in * 1.25
            + self.cache_read_tokens * price_in * 0.10
            + self.output_tokens * price_out
        ) / 1_000_000


@dataclass
class Turn:
    """One assistant response, normalised."""

    blocks: list[Block] = field(default_factory=list)
    stop_reason: StopReason = "end_turn"
    usage: Usage = field(default_factory=Usage)
    provider: str = ""
    raw: Any = None
    refusal_detail: str = ""

    def as_message(self) -> Message:
        # Thinking is dropped from replayed history unless the provider kept it
        # in `raw` — a summarised reasoning trace is not valid input anywhere.
        return Message(
            role="assistant",
            blocks=[b for b in self.blocks if not isinstance(b, Thinking)],
            provider=self.provider,
            raw=self.raw,
        )


class Sink:
    """Where streamed deltas go. The agent's callbacks implement this."""

    def text(self, delta: str) -> None: ...
    def thinking(self, delta: str) -> None: ...


# --------------------------------------------------------------- description


@dataclass(frozen=True)
class Capabilities:
    """What a provider/model pair can actually do.

    The agent branches on these rather than on provider ids, which is what lets
    a new provider arrive without editing the loop.
    """

    streaming: bool = True
    tools: bool = True
    vision: bool = False
    thinking: bool = False
    system_prompt: bool = True
    # A {"role": "system"} message *between* turns. Anthropic's newest models
    # take one; OpenAI and Gemini do not, and fold it into the user turn.
    mid_conversation_system: bool = False
    prompt_caching: bool = False
    effort: bool = False
    parallel_tool_calls: bool = True
    context_window: int = 128_000
    max_output_tokens: int = 8_192

    def as_dict(self) -> dict:
        return {
            "streaming": self.streaming,
            "tools": self.tools,
            "vision": self.vision,
            "thinking": self.thinking,
            "prompt_caching": self.prompt_caching,
            "effort": self.effort,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
        }


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str
    context_window: int = 128_000
    max_output_tokens: int = 8_192
    # USD per million tokens, for the local spend estimate. Published prices
    # move; this is signposted as an estimate everywhere it is shown.
    price_in: float = 0.0
    price_out: float = 0.0
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "context_window": self.context_window,
            "note": self.note,
        }


@dataclass
class ValidationResult:
    ok: bool
    message: str = ""
    code: str = ""
    # Filled in when the check could also read the account's model list.
    models: list[ModelInfo] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "message": self.message,
            "code": self.code,
            "models": [m.as_dict() for m in self.models],
        }


# ------------------------------------------------------------------ provider


class AIProvider(ABC):
    """One AI provider. Subclass, fill in the class attributes, done.

    Adding a provider is this file's whole purpose: implement the five abstract
    methods, register the class in `catalog.py`, and every part of the app —
    setup, settings, the agent loop, the lock, the tests — picks it up without
    further change.
    """

    # --- identity, declared by the subclass ---------------------------------
    id: str = ""
    name: str = ""
    # Where a user goes to create a key, shown in setup.
    console_url: str = ""
    # Cosmetic hint in the input box, e.g. "sk-ant-…". Never a real key.
    key_hint: str = ""
    # Read only in a development checkout — see providers/manager.py.
    env_var: str = ""
    # A one-line explanation of what the user is signing up to pay for.
    billing_note: str = ""

    def __init__(self, api_key: str, model: str | None = None):
        # The single copy of the credential in this process, per provider.
        self._api_key = api_key
        self.model = model or self.default_model()

    # A key is never part of a provider's identity for printing purposes.
    def __repr__(self) -> str:
        return f"<{type(self).__name__} model={self.model!r}>"

    __str__ = __repr__

    # --- key handling -------------------------------------------------------

    @classmethod
    def check_format(cls, key: str) -> str | None:
        """Cheap local sanity check. Returns a complaint, or None if plausible.

        Runs before any network call so an obvious paste error (a whole curl
        command, the wrong provider's key) is caught without a round trip.
        """
        if not key or not key.strip():
            return "Paste the key first."
        if len(key.strip()) != len(key):
            return "That key has whitespace around it."
        return None

    @abstractmethod
    def validate_key(self) -> ValidationResult:
        """Prove the key works, with the cheapest authenticated call there is.

        Must distinguish a rejected key from a rate limit, a billing problem, a
        permissions problem and an outage — never collapse them into "invalid".
        """

    # --- capability description --------------------------------------------

    @classmethod
    @abstractmethod
    def catalog(cls) -> list[ModelInfo]:
        """Known models, best first. Offline: this drives the picker with no key."""

    @classmethod
    def default_model(cls) -> str:
        models = cls.catalog()
        return models[0].id if models else ""

    def model_info(self, model: str | None = None) -> ModelInfo | None:
        wanted = model or self.model
        for info in self.catalog():
            if info.id == wanted:
                return info
        return None

    def price_per_mtok(self, model: str | None = None) -> tuple[float, float]:
        info = self.model_info(model)
        return (info.price_in, info.price_out) if info else (0.0, 0.0)

    @abstractmethod
    def capabilities(self, model: str | None = None) -> Capabilities:
        """What this provider can do with this model."""

    def live_models(self) -> list[ModelInfo]:
        """Models this *account* can actually reach. Falls back to the catalog.

        Optional: a provider without a usable listing endpoint just inherits
        this. Never fatal — a failed listing means the picker shows the static
        catalog, not that the provider is broken.
        """
        return self.catalog()

    # --- the actual work ----------------------------------------------------

    @abstractmethod
    def stream(self, request: TurnRequest, sink: Sink) -> Turn:
        """Run one turn, streaming deltas into `sink`, and return the result."""

    def generate(self, request: TurnRequest) -> Turn:
        """Run one turn without streaming. Defaults to draining the stream."""
        return self.stream(request, Sink())


# ------------------------------------------------------------------- helpers


def split_system(messages: Iterable[Message]) -> list[Message]:
    """Fold system messages into the user turn they belong to.

    The shape providers without a mid-conversation system role need. The agent
    emits its per-turn note *after* the user message it describes, so the note
    attaches backwards to that turn — putting it in front of the next one would
    date the wrong question. A note with no preceding user turn folds forward
    into the next one instead.

    Wrapped in <context> tags so the model can tell it from something the user
    typed: it carries the clock, and content the agent read out of an email
    must not be able to impersonate it.
    """
    out: list[Message] = []
    pending: list[str] = []

    def tagged() -> Text:
        return Text("\n".join(f"<context>{note}</context>" for note in pending))

    for message in messages:
        if message.role == "system":
            pending.append(message.text())
            # Attach to the user turn just emitted, which is what it describes.
            if out and out[-1].role == "user":
                previous = out[-1]
                out[-1] = Message(
                    role="user",
                    blocks=[*previous.blocks, tagged()],
                    provider=previous.provider,
                )
                pending = []
            continue
        if pending and message.role == "user":
            blocks = [*message.blocks, tagged()]
            pending = []
            out.append(Message(role="user", blocks=blocks, provider=message.provider))
            continue
        out.append(message)

    if pending:
        out.append(Message(role="user", blocks=[tagged()]))
    return out
