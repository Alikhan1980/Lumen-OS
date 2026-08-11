"""The agent loop: stream a turn, run any tools the model asks for, repeat.

Provider-agnostic. Nothing here knows which company answers the request — it
asks the provider manager for the active provider, hands it a normalised turn,
and reads a normalised result back. Swapping OpenAI for Gemini changes what
`provider` is bound to and nothing else in this file.

The first thing `send` does is ask for a provider, and that call raises when
the user has not connected one. That is the application lock: it lives here, in
the service layer, not in a screen that a future entry point might forget to
draw.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from . import approvals, registry
from .config import Config
from .prompts import STYLE_PROMPTS, SYSTEM_PROMPT
from .providers import (
    Message,
    ProviderError,
    ProviderManager,
    ProviderNotConfigured,
    Text,
    ToolDef,
    ToolResult,
    ToolUse,
    TurnRequest,
    Usage,
)
from .providers import manager as provider_manager
from .providers.base import Sink

# Stops a malfunctioning loop from burning tokens forever. A normal multi-step
# request settles in well under this.
MAX_TOOL_ROUNDS = 25
MAX_TOOL_RESULT_CHARS = 60_000


class Callbacks:
    """Hooks the UI implements. Defaults make the agent usable headless."""

    def on_text(self, delta: str) -> None: ...
    def on_thinking(self, delta: str) -> None: ...
    def on_turn_start(self) -> None: ...
    def on_turn_end(self) -> None: ...
    def on_tool_start(self, name: str, params: dict) -> None: ...
    def on_tool_end(self, name: str, result: Any, is_error: bool) -> None: ...
    def on_notice(self, message: str) -> None: ...

    def confirm(self, name: str, params: dict) -> bool:
        """Approve an outward-facing or hard-to-reverse action. Deny by default."""
        return False


class _CallbackSink(Sink):
    """Adapts a provider's stream to the UI's callbacks."""

    def __init__(self, callbacks: Callbacks):
        self._callbacks = callbacks

    def text(self, delta: str) -> None:
        self._callbacks.on_text(delta)

    def thinking(self, delta: str) -> None:
        self._callbacks.on_thinking(delta)


class Agent:
    def __init__(
        self,
        config: Config,
        callbacks: Callbacks | None = None,
        manager: ProviderManager | None = None,
    ):
        self.config = config
        self.callbacks = callbacks or Callbacks()
        self.manager = manager or provider_manager.shared()
        self.messages: list[Message] = []
        self.usage = Usage()
        # A preference rather than conversation state, so reset() leaves it be.
        self.style = "default"

        registry.load_all()
        self.tools = [
            ToolDef(spec.name, spec.description, spec.input_schema)
            for spec in registry.all_tools()
        ]

    # -------------------------------------------------------------- provider

    @property
    def provider_id(self) -> str | None:
        return self.manager.active_id()

    @property
    def model(self) -> str:
        active = self.manager.active_id()
        return self.manager.model_for(active) if active else ""

    def price_per_mtok(self) -> tuple[float, float]:
        """Published rates for the active model, for the local spend estimate."""
        active = self.manager.active_id()
        if not active:
            return (0.0, 0.0)
        from .providers import catalog

        provider_class = catalog.provider_class(active)
        if provider_class is None:
            return (0.0, 0.0)
        wanted = self.manager.model_for(active)
        for info in provider_class.catalog():
            if info.id == wanted:
                return (info.price_in, info.price_out)
        return (0.0, 0.0)

    # ---------------------------------------------------------------- system

    def _system_prompt(self) -> str:
        style = STYLE_PROMPTS.get(self.style)
        return f"{SYSTEM_PROMPT}\n\n{style}" if style else SYSTEM_PROMPT

    def _context_note(self) -> str:
        now = datetime.now().astimezone()
        return (
            f"Current local date and time: {now.strftime('%A, %d %B %Y, %H:%M %Z')} "
            f"({now.isoformat()}). Local timezone offset: {now.strftime('%z')}."
        )

    def _request(self, provider) -> TurnRequest:
        capabilities = provider.capabilities()
        return TurnRequest(
            model=provider.model,
            system=self._system_prompt(),
            messages=self.messages,
            tools=self.tools if capabilities.tools else [],
            max_tokens=min(self.config.max_tokens, capabilities.max_output_tokens),
            effort=self.config.effort if capabilities.effort else None,
            want_thinking=self.config.show_thinking and capabilities.thinking,
        )

    # ------------------------------------------------------------------- tools

    def _approve(self, action: str, details: dict) -> bool:
        """Approval asked for by a tool that is already running.

        Same gate as the registry's `confirm` flag, reached from a different
        place, so both kinds of prompt look identical to the user.
        """
        if self.config.auto_approve:
            return True
        return self.callbacks.confirm(action, details)

    def _run_tool(self, name: str, params: dict) -> tuple[str, bool]:
        spec = registry.get(name)
        if spec is None:
            return f"Unknown tool: {name}", True

        # Kept nested: confirm() blocks on the user, so it must not be folded
        # into a compound condition that reads as a pure test.
        if spec.confirm and not self.config.auto_approve:  # noqa: SIM102
            if not self.callbacks.confirm(name, params):
                return (
                    (
                        "The user declined this action. Do not retry it. Ask what "
                        "they would like instead, or continue with the rest of the task."
                    ),
                    False,
                )

        self.callbacks.on_tool_start(name, params)
        try:
            # Some actions can only be judged once the handler has looked: a
            # browser click is a link or a Place order button depending on the
            # page. Handlers ask through this channel — see agent/approvals.py.
            with approvals.bind(self._approve):
                result = spec.handler(**params)
        except Exception as exc:  # surfaced to the model so it can adapt
            detail = f"{type(exc).__name__}: {exc}"
            self.callbacks.on_tool_end(name, detail, True)
            return detail, True

        rendered = (
            result
            if isinstance(result, str)
            else json.dumps(result, ensure_ascii=False, default=str)
        )
        if len(rendered) > MAX_TOOL_RESULT_CHARS:
            rendered = (
                rendered[:MAX_TOOL_RESULT_CHARS]
                + f"\n\n[result truncated at {MAX_TOOL_RESULT_CHARS} characters — "
                "narrow the query or request fewer items]"
            )
        self.callbacks.on_tool_end(name, result, False)
        return rendered, False

    # -------------------------------------------------------------------- turn

    def _stream_once(self, provider, sink: _CallbackSink):
        """One model call, with the user's fallback preference applied.

        Fallback is off unless the user turned it on, and only ever triggers on
        an outage, a rate limit or a network failure — never on a bad key,
        which another provider would not fix and which would mean spending
        someone else's credits to hide a fixable mistake.
        """
        try:
            return provider, provider.stream(self._request(provider), sink)
        except ProviderError as first:
            if not self.manager.should_fall_back(first):
                raise
            for candidate_id in self.manager.fallback_chain():
                try:
                    candidate = self.manager.build(candidate_id)
                except (ProviderNotConfigured, ProviderError):
                    continue
                self.callbacks.on_notice(
                    f"{provider.name} was unavailable ({first.code}). Falling back to "
                    f"{candidate.name}, which will bill your {candidate.name} account."
                )
                try:
                    return candidate, candidate.stream(self._request(candidate), sink)
                except ProviderError:
                    continue
            raise

    def send(self, user_input: str) -> str:
        """Run one user turn to completion. Returns the assistant's final text.

        Raises ProviderNotConfigured before touching anything if no provider is
        connected — the lock.
        """
        provider = self.manager.require_active()

        self.messages.append(Message(role="user", blocks=[Text(user_input)]))
        # Operator channel: on a provider that takes a mid-conversation system
        # message this cannot be spoofed by content the agent reads out of an
        # email. On one that does not, the provider folds it into the user turn
        # inside <context> tags instead.
        self.messages.append(Message(role="system", blocks=[Text(self._context_note())]))

        sink = _CallbackSink(self.callbacks)
        final_text: list[str] = []

        for _ in range(MAX_TOOL_ROUNDS):
            self.callbacks.on_turn_start()
            try:
                provider, turn = self._stream_once(provider, sink)
            except (ProviderError, ProviderNotConfigured):
                self._rollback_turn()
                raise
            finally:
                self.callbacks.on_turn_end()

            self.usage.add(turn.usage)

            if turn.stop_reason == "refusal":
                self.callbacks.on_notice(
                    "The model declined this request"
                    + (f" ({turn.refusal_detail})." if turn.refusal_detail else ".")
                )
                self._rollback_turn()
                return ""

            self.messages.append(turn.as_message())
            final_text.extend(block.text for block in turn.blocks if isinstance(block, Text) and block.text)

            if turn.stop_reason == "pause":
                # Server-side tool hit its iteration cap; re-send to resume.
                continue

            if turn.stop_reason == "max_tokens":
                self.callbacks.on_notice(
                    "Output hit the token limit and was cut off. Raise "
                    "AGENT_MAX_TOKENS or ask for a smaller slice of the task."
                )
                break

            if turn.stop_reason != "tool_use":
                break

            results: list[Any] = []
            for call in (b for b in turn.blocks if isinstance(b, ToolUse)):
                content, is_error = self._run_tool(call.name, call.input)
                # The name rides along because Gemini pairs a result to its
                # call by name rather than by id.
                results.append(ToolResult(call.id, content, is_error, name=call.name))
            # All results for one assistant turn go back in a single user message.
            self.messages.append(Message(role="user", blocks=results))
        else:
            self.callbacks.on_notice(
                f"Stopped after {MAX_TOOL_ROUNDS} tool rounds without finishing."
            )

        return "\n".join(final_text).strip()

    def _rollback_turn(self) -> None:
        """Undo a failed turn so the next request has a valid history.

        Pops trailing user/system messages, and any assistant message left
        holding a tool call with no matching result — every provider rejects an
        unanswered tool call.
        """
        while self.messages:
            last = self.messages[-1]
            if last.role in {"user", "system"}:
                self.messages.pop()
                continue
            if last.tool_uses():
                self.messages.pop()
                continue
            break

    def reset(self) -> None:
        self.messages.clear()
