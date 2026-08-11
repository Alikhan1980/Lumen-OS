"""First-run setup, for someone who was handed the app and nothing else.

Two steps, and neither is skippable:

1. **An AI provider.** The user picks one they hold an account with and pastes
   their own key. This app ships with no key and cannot obtain one, so until
   this step is done the agent does nothing at all.
2. **A Google sign-in**, which grants access to their own mail, files and
   calendar.

Runs once; after that the app starts straight into chat. There is no "skip",
no "continue without a key", and no guest mode — see providers/manager.py for
where that is actually enforced.
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel

from .config import ACCOUNT_PATH, DATA_DIR, Config, client_secret_path, load_config
from .provider_ui import choose_provider, connect_provider
from .providers import catalog, keystore
from .providers.manager import SOURCE_ENVIRONMENT, ProviderManager, shared

console = Console()

WELCOME = """\
This assistant works inside [bold]your own[/bold] Google account — it can read and \
search your mail, files, calendar, contacts and tasks, and can draft or send \
mail on your behalf.

Two one-time things are needed:

  [bold]1.[/bold] An AI provider — you bring your own API key
  [bold]2.[/bold] A Google sign-in, which grants access to your data

[bold]You pay your AI provider directly[/bold] for what you use, at their prices. \
This app includes no AI usage of its own and takes no cut.

Both credentials stay on this computer. Your key goes into
[dim]{store}[/dim]
and everything else into
[dim]{data_dir}[/dim]
"""

GOOGLE_HELP = """\
A browser window will open so you can pick a Google account and approve access.

[bold yellow]You will see a warning that says "Google hasn't verified this app".[/bold yellow]
That is expected — this is a private tool, not something published on the Google \
app store. Click [bold]Advanced[/bold], then [bold]Go to (app name) (unsafe)[/bold] \
to continue.

Nothing is sent anywhere except between this computer, Google, and the AI \
provider you chose.
"""


def needs_onboarding(manager: ProviderManager | None = None) -> bool:
    manager = manager or shared()
    return not manager.is_unlocked() or not ACCOUNT_PATH.exists()


# ----------------------------------------------------------------- google


def _collect_google_login(step_label: str = "Step 2 of 2 — Google sign-in") -> bool:
    from .tools.google_auth import GoogleAuthError, get_credentials, refresh_account_email

    console.print(Panel(GOOGLE_HELP, title=f"[bold]{step_label}[/bold]", border_style="blue"))
    if client_secret_path() is None:
        console.print(
            "  [red]This build has no Google OAuth client bundled, so sign-in "
            "cannot start. See DISTRIBUTION.md.[/red]"
        )
        return False
    try:
        console.input("  Press Enter to open your browser… ")
    except (EOFError, KeyboardInterrupt):
        return False

    try:
        get_credentials(force_login=True)
    except GoogleAuthError as exc:
        console.print(f"  [red]{exc}[/red]")
        return False
    except Exception as exc:
        console.print(f"  [red]Sign-in failed: {type(exc).__name__}: {exc}[/red]")
        return False

    email = refresh_account_email()
    if not email:
        console.print("  [yellow]Signed in, but could not read the account address.[/yellow]")
        return True
    console.print(f"  [green]Signed in as [bold]{email}[/bold][/green]\n")
    return True


# ------------------------------------------------------------------ step 1


def _connect_first_provider(manager: ProviderManager) -> bool:
    """Get the user to a working provider, however few steps that takes.

    A key can already exist without the agent being usable — a development
    checkout with an environment variable set, or a returning user who removed
    the provider that used to be active. In that case there is nothing to paste
    and the only missing decision is which one to use, so ask only that.
    """
    already = manager.configured_ids()
    if already:
        console.print(
            Panel(
                "These providers already have a key on this computer. Choose "
                "which one the agent should use.\n\n"
                "[dim]Nothing is selected automatically: which account gets "
                "billed is your decision, not this app's.[/dim]",
                title="[bold]Step 1 of 2 — choose your AI provider[/bold]",
                border_style="blue",
            )
        )
        for index, provider_id in enumerate(already, start=1):
            provider_class = catalog.provider_class(provider_id)
            credential = manager.credential(provider_id)
            origin = (
                f" [yellow](from {provider_class.env_var})[/yellow]"
                if credential and credential.source == SOURCE_ENVIRONMENT
                else ""
            )
            console.print(
                f"  [bold]{index}.[/bold] {provider_class.name} "
                f"[dim]{credential.masked if credential else ''}[/dim]{origin}"
            )
        console.print(f"  [bold]{len(already) + 1}.[/bold] Connect a different provider")
        try:
            answer = console.input(f"  Choose 1-{len(already) + 1}: ").strip()
        except (EOFError, KeyboardInterrupt):
            return False
        if answer.isdigit() and 1 <= int(answer) <= len(already):
            manager.set_active(already[int(answer) - 1])
            chosen = catalog.provider_class(manager.active_id())
            console.print(
                f"  [green]Using {chosen.name}[/green] "
                f"[dim]({manager.model_for(manager.active_id())})[/dim]\n"
            )
            return True

    console.print(
        Panel(
            "This agent needs an AI provider before it can do anything.\n"
            "Pick the one you have an account with.\n\n"
            "[dim]You will be billed by that provider for your own usage, at "
            "their published prices and under their terms.[/dim]",
            title="[bold]Step 1 of 2 — connect an AI provider[/bold]",
            border_style="blue",
        )
    )
    provider_class = choose_provider()
    if provider_class is None:
        return False
    if not connect_provider(manager, provider_class):
        return False
    # A key added while another provider was already active does not change the
    # active one, so make sure the user ends this step actually unlocked.
    if not manager.is_unlocked():
        manager.set_active(provider_class.id)
    return True


# -------------------------------------------------------------------- run


def run(config: Config, manager: ProviderManager | None = None) -> Config | None:
    """Walk the user through setup. Returns a reloaded Config, or None if aborted."""
    manager = manager or shared()
    store = keystore.describe()
    console.print(
        Panel(
            WELCOME.format(
                data_dir=DATA_DIR,
                store=store.get("name") if store.get("available") else "no secure store found",
            ),
            title="[bold]Welcome[/bold]",
            border_style="green",
        )
    )

    # Kept nested: _connect_first_provider blocks on the user, so it must not
    # be folded into a compound condition that reads as a pure test.
    if not manager.is_unlocked():  # noqa: SIM102
        if not _connect_first_provider(manager):
            console.print(
                "\n[red]No AI provider was connected, so the agent cannot start.[/red]\n"
                "[dim]Run the app again when you have a key.[/dim]"
            )
            return None

    if not ACCOUNT_PATH.exists() and not _collect_google_login():
        return None

    console.print("[bold green]Setup complete.[/bold green] This will not be asked again.\n")
    return load_config()
