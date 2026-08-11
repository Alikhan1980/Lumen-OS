"""Rich terminal front-end: streaming output, tool traces, approval prompts."""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.status import Status
from rich.table import Table
from rich.text import Text

from . import registry
from .config import Config
from .core import Callbacks

console = Console()

GROUP_STYLE = {
    "gmail": "red",
    "drive": "yellow",
    "calendar": "blue",
    "contacts": "magenta",
    "tasks": "green",
    "files": "cyan",
    "search": "bright_blue",
    "browser": "bright_magenta",
}


def _preview(value: Any, limit: int = 110) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class TerminalCallbacks(Callbacks):
    def __init__(self, config: Config):
        self.config = config
        self.show_thinking = config.show_thinking
        self._wrote_text = False
        self._thinking_open = False
        self._status: Status | None = None

    # -------------------------------------------------------------- spinner

    def _spin(self, label: str) -> None:
        """Show a spinner during the silent gaps: waiting on the model, or on a
        tool that is out talking to Google.

        Rich draws this in a live region it repaints several times a second, so
        anything that writes to the console has to stop it first — otherwise the
        repaint and the streamed tokens fight over the same line.
        """
        if self._status is not None or not console.is_terminal:
            return
        self._status = console.status(
            f"[dim italic]{label}[/dim italic]", spinner="dots", spinner_style="green"
        )
        self._status.start()

    def idle(self) -> None:
        """Stop the spinner. Safe to call when nothing is spinning."""
        if self._status is not None:
            self._status.stop()
            self._status = None

    # ------------------------------------------------------------- streaming

    def on_turn_start(self) -> None:
        self._wrote_text = False
        self._spin("thinking…")

    def on_turn_end(self) -> None:
        self.idle()
        self._close_thinking()
        if self._wrote_text:
            console.print()

    def on_text(self, delta: str) -> None:
        self.idle()
        self._close_thinking()
        if not self._wrote_text:
            console.print("[bold green]agent[/bold green] ", end="")
            self._wrote_text = True
        console.print(Text(delta, style="default"), end="")

    def on_thinking(self, delta: str) -> None:
        # With the reasoning hidden the spinner keeps turning — that is the only
        # sign anything is happening while the model works.
        if not self.show_thinking:
            return
        self.idle()
        if not self._thinking_open:
            console.print("[dim italic]thinking…[/dim italic] ", end="")
            self._thinking_open = True
        console.print(Text(delta, style="dim italic"), end="")

    def _close_thinking(self) -> None:
        if self._thinking_open:
            console.print()
            self._thinking_open = False

    # ----------------------------------------------------------------- tools

    def on_tool_start(self, name: str, params: dict) -> None:
        self.idle()
        self._close_thinking()
        if self._wrote_text:
            console.print()
            self._wrote_text = False
        spec = registry.get(name)
        color = GROUP_STYLE.get(spec.group if spec else "", "white")
        args = _preview(params) if params else ""
        console.print(f"  [{color}]▸ {name}[/{color}] [dim]{args}[/dim]")
        self._spin(f"{registry.activity(name)}…")

    def on_tool_end(self, name: str, result: Any, is_error: bool) -> None:
        self.idle()
        if is_error:
            console.print(f"    [red]✗ {_preview(result, 200)}[/red]")
            return
        summary = ""
        if isinstance(result, dict):
            if "count" in result:
                summary = f"{result['count']} result(s)"
            elif "status" in result:
                summary = str(result["status"])
            else:
                summary = _preview(result, 80)
        console.print(f"    [dim]✓ {summary}[/dim]")

    def on_notice(self, message: str) -> None:
        self.idle()
        self._close_thinking()
        console.print(f"  [yellow]! {message}[/yellow]")

    # ---------------------------------------------------------- confirmation

    def confirm(self, name: str, params: dict) -> bool:
        self.idle()
        self._close_thinking()
        if self._wrote_text:
            console.print()
            self._wrote_text = False

        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column(style="bold")
        table.add_column(overflow="fold")
        for key, value in params.items():
            table.add_row(key, _preview(value, 400))

        console.print(
            Panel(
                table,
                title=f"[bold yellow]Approve: {name}[/bold yellow]",
                subtitle="[dim]y = run once · n = skip[/dim]",
                border_style="yellow",
            )
        )
        try:
            answer = console.input("  [bold]Run this? [y/N][/bold] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("  [dim]skipped[/dim]")
            return False
        approved = answer in {"y", "yes"}
        console.print("  [dim]approved[/dim]" if approved else "  [dim]skipped[/dim]")
        return approved


def print_banner(
    config: Config,
    account_email: str | None = None,
    provider_label: str = "",
) -> None:
    tools = registry.all_tools()
    groups: dict[str, int] = {}
    for spec in tools:
        groups[spec.group] = groups.get(spec.group, 0) + 1
    connected = "  ".join(
        f"[{GROUP_STYLE.get(g, 'white')}]{g}[/{GROUP_STYLE.get(g, 'white')}] {n}"
        for g, n in sorted(groups.items())
    )
    # Whose account this is acting on is the single most important fact on
    # screen — it goes first, in bold, every session. Which provider is about
    # to be billed is the second.
    identity = (
        f"[bold green]{account_email}[/bold green]"
        if account_email
        else "[yellow]not signed in — use /signin[/yellow]"
    )
    console.print(
        Panel(
            f"acting as {identity}\n{connected}\n"
            f"[dim]{len(tools)} tools · {provider_label or 'no provider'}"
            + (f" · effort {config.effort}" if config.effort else "")
            + f"\nworkspace {config.workspace}[/dim]",
            title="[bold]Lumen OS[/bold]",
            subtitle="[dim]/help for commands · /exit to quit[/dim]",
            border_style="blue",
        )
    )


def print_tools() -> None:
    table = Table(title="Available tools", box=None, header_style="bold")
    table.add_column("Tool", style="bold")
    table.add_column("App")
    table.add_column("Asks first", justify="center")
    table.add_column("What it does", overflow="fold")
    for spec in registry.all_tools():
        color = GROUP_STYLE.get(spec.group, "white")
        table.add_row(
            spec.name,
            f"[{color}]{spec.group}[/{color}]",
            "yes" if spec.confirm else "",
            spec.description.split(".")[0].strip() + ".",
        )
    console.print(table)


def print_help() -> None:
    console.print(
        Panel(
            "[bold]/help[/bold]      show this\n"
            "[bold]/tools[/bold]     list every tool and which ones ask before running\n"
            "[bold]/providers[/bold] add, switch, test or remove an AI provider key\n"
            "[bold]/whoami[/bold]    which Google account the agent is acting as\n"
            "[bold]/thinking[/bold]  toggle showing the model's reasoning\n"
            "[bold]/clear[/bold]     start a fresh conversation\n"
            "[bold]/cost[/bold]      token usage and estimated spend this session\n"
            "[bold]/signin[/bold]    sign in, or switch to a different Google account\n"
            "[bold]/signout[/bold]   forget the Google account on this computer\n"
            "[bold]/exit[/bold]      quit\n\n"
            "[dim]Anything else is sent to the agent. Examples:\n"
            '  "what came into my inbox today that needs a reply?"\n'
            '  "find the Q3 budget sheet in Drive and summarize the totals"\n'
            '  "am I free Thursday afternoon? if so book 90 minutes for deep work"\n'
            '  "draft a reply to Sarah\'s last email agreeing to Tuesday"[/dim]',
            title="[bold]Commands[/bold]",
            border_style="blue",
        )
    )
