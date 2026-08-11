"""Terminal UI for AI providers: connect, test, switch, replace, remove.

Shared by first-run setup and the `/providers` command, so the screens a user
meets on day one and on day one hundred are the same screens.

Nothing here ever prints a key. Input is read without echo, the local variable
is cleared as soon as the manager has it, and what is shown afterwards is the
masked tail and nothing more.
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .providers import catalog
from .providers.base import AIProvider, ProviderNotConfigured
from .providers.manager import SOURCE_ENVIRONMENT, ProviderManager

console = Console()


def read_key(prompt: str = "  Paste your key: ") -> str | None:
    """Read a key without echoing it. None means the user backed out."""
    try:
        return console.input(prompt, password=True).strip()
    except (EOFError, KeyboardInterrupt):
        return None


def choose_provider(prompt: str = "  Choose") -> type[AIProvider] | None:
    providers = catalog.all_providers()
    for index, provider in enumerate(providers, start=1):
        console.print(f"  [bold]{index}.[/bold] {provider.name}   [dim]{provider.console_url}[/dim]")
    while True:
        try:
            answer = console.input(f"{prompt} 1-{len(providers)} (or q to cancel): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if answer in {"q", "quit", "exit", ""}:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(providers):
            return providers[int(answer) - 1]
        for provider in providers:
            if answer in {provider.id, provider.name.lower()}:
                return provider
        console.print("  [yellow]Pick one of the numbers above.[/yellow]")


def connect_provider(manager: ProviderManager, provider_class: type[AIProvider]) -> bool:
    """Collect, validate and store one provider's key.

    Replacing an existing key runs through here too: the manager only writes
    once the provider has accepted the new key, so a failed attempt leaves the
    working one untouched.
    """
    replacing = manager.is_configured(provider_class.id)
    console.print(
        Panel(
            f"Get a key at [bold]{provider_class.console_url}[/bold]\n\n"
            f"{provider_class.billing_note}\n\n"
            f"[dim]The key looks like {provider_class.key_hint} and is pasted in once, "
            "here. It is not shown as you type, and it is never written into this "
            "project's files.[/dim]"
            + (
                "\n\n[dim]The key you have now keeps working unless the new one "
                "validates.[/dim]"
                if replacing
                else ""
            ),
            title=f"[bold]{provider_class.name}[/bold]",
            border_style="blue",
        )
    )

    store = manager.keystore_status()
    if not store.get("available"):
        console.print(f"  [red]{store.get('detail')}[/red]")
        return False
    if not store.get("secure"):
        console.print(
            f"  [yellow]Warning: keys will be stored unencrypted — {store.get('detail')}.[/yellow]"
        )

    for attempt in range(3):
        key = read_key()
        if key is None:
            return False
        if not key:
            continue

        console.print("  [dim]Checking with the provider…[/dim]")
        result = manager.add(provider_class.id, key)
        # The only copy in this process, gone the moment the keystore has it.
        key = ""
        if result.ok:
            credential = manager.credential(provider_class.id)
            console.print(
                f"  [green]Connected.[/green] {provider_class.name} "
                f"[dim]{credential.masked if credential else ''} · "
                f"stored in {store.get('name')}[/dim]"
            )
            console.print(f"  [dim]Model: {manager.model_for(provider_class.id)}[/dim]\n")
            return True
        console.print(f"  [red]{result.message}[/red]")
        if attempt < 2:
            console.print("  [dim]Try again, or press Enter on an empty line to stop.[/dim]")
    return False


def print_status(manager: ProviderManager) -> None:
    """The AI Providers screen."""
    state = manager.status()

    table = Table(show_header=True, box=None, header_style="bold", padding=(0, 2))
    table.add_column("", width=1)
    table.add_column("Provider")
    table.add_column("Status")
    table.add_column("Key")
    table.add_column("Model")

    for provider in state["providers"]:
        connected = provider["connected"]
        mark = "[green]✓[/green]" if connected else "[dim]○[/dim]"
        if not connected:
            status = "[dim]Not connected[/dim]"
        elif provider["active"]:
            status = "[bold green]Connected · active[/bold green]"
        else:
            status = "[green]Connected[/green]"
        note = ""
        if provider["source"] == SOURCE_ENVIRONMENT:
            note = f" [yellow](from {provider['env_var']})[/yellow]"
        table.add_row(
            mark,
            provider["name"],
            status + note,
            f"[dim]{provider['masked_key']}[/dim]" if connected else "",
            f"[dim]{provider['model']}[/dim]" if connected else "",
        )

    console.print()
    console.print(table)

    store = state["keystore"]
    where = (
        f"{store['name']} — {store['detail']}"
        if store.get("available")
        else "[red]no secure store available[/red]"
    )
    console.print(f"\n  [dim]Keys are stored in: {where}[/dim]")
    if store.get("available") and not store.get("secure"):
        console.print("  [yellow]These keys are NOT encrypted on this system.[/yellow]")

    if state["unlocked"]:
        active = next(p for p in state["providers"] if p["active"])
        console.print(f"  [dim]Active provider: [bold]{active['name']}[/bold] · {active['model']}[/dim]")
    else:
        console.print(f"  [yellow]{state['lock_reason']}[/yellow]")
    fallback = "on" if state["fallback_enabled"] else "off"
    console.print(f"  [dim]Automatic fallback to another provider: {fallback}[/dim]")
    console.print(
        "  [dim]You pay each provider directly for your own usage, at their prices.[/dim]\n"
    )


def _choose_model(manager: ProviderManager, provider_id: str) -> None:
    provider_class = catalog.provider_class(provider_id)
    console.print(f"\n  [dim]Reading the model list from {provider_class.name}…[/dim]")
    models = manager.models_for(provider_id, live=True)
    current = manager.model_for(provider_id)

    shown = models[:30]
    for index, info in enumerate(shown, start=1):
        marker = "[green]·[/green]" if info.id == current else " "
        note = f"  [dim]{info.note}[/dim]" if info.note else ""
        console.print(f"  {marker} [bold]{index}.[/bold] {info.id}{note}")
    console.print("    [dim]or type a model id directly[/dim]")

    try:
        answer = console.input("  Model (blank to keep): ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not answer:
        return
    if answer.isdigit() and 1 <= int(answer) <= len(shown):
        answer = shown[int(answer) - 1].id
    manager.set_model(provider_id, answer)
    console.print(f"  [green]{provider_class.name} will use {answer}.[/green]")


def _pick_configured(manager: ProviderManager, verb: str) -> str | None:
    configured = manager.configured_ids()
    if not configured:
        console.print("  [yellow]No providers are connected yet.[/yellow]")
        return None
    for index, provider_id in enumerate(configured, start=1):
        console.print(f"  [bold]{index}.[/bold] {catalog.provider_class(provider_id).name}")
    try:
        answer = console.input(f"  {verb} which? (or q): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    if answer.isdigit() and 1 <= int(answer) <= len(configured):
        return configured[int(answer) - 1]
    return answer if answer in configured else None


def _remove(manager: ProviderManager) -> None:
    provider_id = _pick_configured(manager, "Remove")
    if not provider_id:
        return
    name = catalog.provider_class(provider_id).name
    try:
        answer = console.input(f"  Remove the {name} key from this computer? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if answer not in {"y", "yes"}:
        console.print("  [dim]Left alone.[/dim]")
        return

    outcome = manager.remove(provider_id)
    console.print(f"  [green]{name} removed.[/green]")
    if outcome["locked"]:
        console.print(
            "  [yellow]That was the last provider. The agent is locked until you "
            "connect one.[/yellow]"
        )
    elif outcome["switched"]:
        switched = catalog.provider_class(outcome["active"]).name
        console.print(f"  [dim]Active provider is now {switched}.[/dim]")
    elif outcome["active"] is None:
        console.print("  [yellow]That was the active provider. Choose a new one.[/yellow]")

    credential = manager.credential(provider_id)
    if credential is not None:
        # An environment variable survives a keystore delete, and pretending
        # otherwise would leave the user thinking a key is gone when it is not.
        console.print(
            f"  [yellow]{name} is still configured from the {credential.source} "
            f"({catalog.provider_class(provider_id).env_var}). Unset it to remove "
            "it fully.[/yellow]"
        )


def _test(manager: ProviderManager) -> None:
    provider_id = _pick_configured(manager, "Test")
    if not provider_id:
        return
    name = catalog.provider_class(provider_id).name
    console.print(f"  [dim]Asking {name}…[/dim]")
    result = manager.test(provider_id)
    if result.ok:
        console.print(f"  [green]{name} is working.[/green] [dim]{result.message}[/dim]")
    else:
        console.print(f"  [red]{name}: {result.message}[/red] [dim]({result.code})[/dim]")


def _activate(manager: ProviderManager) -> None:
    provider_id = _pick_configured(manager, "Use")
    if not provider_id:
        return
    try:
        manager.set_active(provider_id)
    except ProviderNotConfigured as exc:
        console.print(f"  [red]{exc}[/red]")
        return
    console.print(
        f"  [green]Active provider: {catalog.provider_class(provider_id).name} "
        f"({manager.model_for(provider_id)})[/green]"
    )


def _fallback(manager: ProviderManager) -> None:
    enabled = manager.settings.fallback_enabled
    console.print(
        Panel(
            "When the provider you chose is unavailable or rate-limited, the "
            "agent can retry the same request with another provider you have "
            "connected.\n\n"
            "[bold]That spends money on the other account.[/bold] It is off "
            "unless you turn it on, and it never triggers for a rejected key.",
            title="[bold]Automatic provider fallback[/bold]",
            border_style="yellow",
        )
    )
    console.print(f"  Currently: [bold]{'ON' if enabled else 'OFF'}[/bold]")
    try:
        answer = console.input("  Turn it on? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    manager.set_fallback(answer in {"y", "yes"})
    console.print(f"  [dim]Fallback is now {'on' if manager.settings.fallback_enabled else 'off'}.[/dim]")


MENU = """\
  [bold]a[/bold]  add or replace a provider key
  [bold]u[/bold]  choose which provider to use
  [bold]m[/bold]  choose a model
  [bold]t[/bold]  test a connection
  [bold]r[/bold]  remove a provider
  [bold]f[/bold]  automatic fallback
  [bold]q[/bold]  back to chat\
"""


def manage(manager: ProviderManager) -> None:
    """The interactive provider screen behind /providers."""
    while True:
        print_status(manager)
        console.print(MENU)
        try:
            choice = console.input("\n  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if choice in {"q", "quit", "exit", ""}:
            return
        if choice == "a":
            provider_class = choose_provider()
            if provider_class:
                connect_provider(manager, provider_class)
        elif choice == "u":
            _activate(manager)
        elif choice == "m":
            provider_id = _pick_configured(manager, "Model for")
            if provider_id:
                _choose_model(manager, provider_id)
        elif choice == "t":
            _test(manager)
        elif choice == "r":
            _remove(manager)
        elif choice == "f":
            _fallback(manager)
        else:
            console.print("  [yellow]Not one of the options.[/yellow]")
