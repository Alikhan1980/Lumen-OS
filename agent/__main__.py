"""Entry point: `python -m agent`, or the packaged executable."""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

from . import onboarding, provider_ui, registry, ui
from .config import DATA_DIR, TOKEN_PATH, client_secret_path, is_frozen, load_config
from .core import Agent
from .providers import ProviderError, ProviderNotConfigured
from .providers.manager import SOURCE_ENVIRONMENT
from .providers.manager import shared as shared_manager
from .ui import console


def _check() -> int:
    """Verify the install without calling any paid API."""
    from .tools.google_auth import cached_account_email

    ok = True
    config = load_config()
    manager = shared_manager()
    registry.load_all()

    active = manager.active_id()
    console.print(f"[bold]AI provider[/bold]      {active or 'none'}")
    console.print(f"[bold]Model[/bold]            {manager.model_for(active) if active else '—'}")
    console.print(f"[bold]Tools registered[/bold] {len(registry.all_tools())}")
    console.print(f"[bold]Data folder[/bold]      {DATA_DIR}")
    console.print(f"[bold]Workspace[/bold]        {config.workspace}")

    store = manager.keystore_status()
    if not store.get("available"):
        console.print(f"[red]MISS[/red] No secure credential store — {store.get('detail')}")
    elif store.get("secure"):
        console.print(f"[green]OK[/green]   Keys stored in {store['name']} ({store['detail']})")
    else:
        console.print(f"[yellow]--[/yellow]   Keys stored UNENCRYPTED — {store['detail']}")

    from .providers import catalog

    configured = manager.configured_ids()
    from_env = False
    for provider_id in configured:
        credential = manager.credential(provider_id)
        origin = ""
        if credential.source == SOURCE_ENVIRONMENT:
            origin = f" (from {catalog.provider_class(provider_id).env_var})"
            from_env = True
        console.print(
            f"[green]OK[/green]   {catalog.provider_class(provider_id).name} connected "
            f"{credential.masked}{origin}"
        )
    if from_env:
        # A key in the environment is a plaintext credential in a file. It
        # works in a checkout, but it is not where a key ought to live.
        console.print(
            "[yellow]--[/yellow]   A key is coming from the environment, which "
            "means it is sitting in plaintext.\n"
            "[dim]     Add it with --providers to move it into the credential "
            "store, then remove the variable.[/dim]"
        )
    if manager.is_unlocked():
        console.print(f"[green]OK[/green]   Active provider: {active}")
    else:
        ok = False
        console.print(f"[red]MISS[/red] {manager.lock_reason()}")
        console.print("[dim]     Fix with:  .\\run.ps1 --providers[/dim]")

    secret = client_secret_path()
    if secret:
        console.print(f"[green]OK[/green]   Google OAuth client found ({secret})")
    else:
        ok = False
        console.print("[red]MISS[/red] No Google OAuth client — see DISTRIBUTION.md")

    email = cached_account_email()
    if email:
        console.print(f"[green]OK[/green]   Signed in to Google as {email}")
    else:
        console.print("[yellow]--[/yellow]   Not signed in to Google yet — run with --auth")

    from .tools.browser import status as browser_status
    from .tools.websearch import provider_order

    providers = provider_order(config)
    fallbacks = f" (then {', '.join(providers[1:])})" if len(providers) > 1 else ""
    console.print(f"[green]OK[/green]   Web search via {providers[0]}{fallbacks}")

    # Not fatal: everything else works without a browser, and the download is
    # 150 MB — worth telling someone about rather than failing the check over.
    browser = browser_status()
    if browser["ready"]:
        console.print("[green]OK[/green]   Browser automation ready (Chromium found)")
    else:
        console.print(f"[yellow]--[/yellow]   Browser automation unavailable — {browser['detail']}")

    from .notify import task_status
    from .reminders import DB_PATH, store

    counts = store().counts()
    console.print(
        f"[green]OK[/green]   Reminders database ({counts['upcoming']} pending, "
        f"{counts['completed']} done) at {DB_PATH.name}"
    )
    schedule = task_status()
    if schedule.get("installed"):
        console.print("[green]OK[/green]   Reminders ring when the app is closed (scheduled task)")
    else:
        console.print(
            "[yellow]--[/yellow]   Reminders only ring while the app is open — "
            "enable with --reminders-install"
        )

    console.print("\n[green]Ready.[/green]" if ok else "\n[red]Not ready.[/red]")
    return 0 if ok else 1


def _notify_sweep() -> int:
    """Fire whatever is due. This is what the scheduled task runs every minute.

    Deliberately light: no model client, no tool registry, no Google. It has to
    start, check a SQLite file and exit, 1440 times a day.
    """
    from .notify import deliver_due

    for reminder in deliver_due():
        print(f"fired {reminder['id']} {reminder['title']!r}")
    return 0


def _reminders_schedule(action: str) -> int:
    """Install, remove or report the Windows task that fires reminders."""
    from .notify import install_task, remove_task, sweep_command, task_status

    if action == "install":
        executable, arguments = sweep_command()
        console.print(f"[dim]Registering: {executable} {arguments}[/dim]")
        result = install_task()
        if not result.get("installed"):
            console.print(f"[red]Could not register the task:[/red] {result.get('error')}")
            return 1
        console.print(
            "[green]Reminder notifications are on.[/green] Windows will check every "
            "minute, whether or not this app is running."
        )
        return 0

    if action == "uninstall":
        remove_task()
        console.print(
            "[dim]Scheduled notifications removed. Reminders still exist and still "
            "show in the app — they just will not ring while it is closed.[/dim]"
        )
        return 0

    status = task_status()
    if not status.get("supported"):
        console.print("[yellow]Scheduled notifications are Windows-only.[/yellow]")
        return 1
    if not status.get("installed"):
        console.print(
            "[yellow]Not installed.[/yellow] Reminders only ring while the app is "
            "open. Turn it on with:  .\\run.ps1 --reminders-install"
        )
        return 1
    console.print(
        f"[green]Installed[/green] · state {status.get('state')}\n"
        f"[dim]last run {status.get('last_run')} · next run {status.get('next_run')}[/dim]"
    )
    return 0


def _auth() -> int:
    from .tools.google_auth import (
        GoogleAuthError,
        get_credentials,
        refresh_account_email,
        reset_service_cache,
    )

    try:
        console.print("Opening your browser to sign in to Google…")
        get_credentials(force_login=True)
        reset_service_cache()
        email = refresh_account_email()
        console.print(f"[green]Signed in as [bold]{email}[/bold][/green]")
        console.print(f"[dim]Token saved to {TOKEN_PATH}[/dim]")
        return 0
    except GoogleAuthError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    except Exception as exc:
        console.print(f"[red]Sign-in failed: {type(exc).__name__}: {exc}[/red]")
        return 1


def _handle_command(command: str, agent: Agent) -> bool:
    """Return False to end the session."""
    from .tools.google_auth import cached_account_email, refresh_account_email, sign_out

    name = command.split()[0].lower()

    if name in {"/exit", "/quit", "/q"}:
        return False
    if name == "/help":
        ui.print_help()
    elif name == "/tools":
        ui.print_tools()
    elif name == "/clear":
        agent.reset()
        console.print("[dim]Conversation cleared.[/dim]")
    elif name == "/thinking":
        agent.callbacks.show_thinking = not agent.callbacks.show_thinking
        state = "on" if agent.callbacks.show_thinking else "off"
        console.print(f"[dim]Thinking display: {state}.[/dim]")
    elif name == "/whoami":
        email = refresh_account_email() or cached_account_email()
        if email:
            console.print(f"[dim]Acting as [bold]{email}[/bold] · data in {DATA_DIR}[/dim]")
        else:
            console.print("[yellow]Not signed in to Google. Use /signin.[/yellow]")
    elif name in {"/signin", "/auth"}:
        _auth()
    elif name == "/signout":
        sign_out()
        console.print(
            "[dim]Signed out on this computer. The app can no longer reach that "
            "Google account until you run /signin.\n"
            "To revoke access at Google's end too, visit "
            "https://myaccount.google.com/permissions[/dim]"
        )
    elif name in {"/providers", "/provider", "/keys"}:
        provider_ui.manage(agent.manager)
    elif name == "/cost":
        usage = agent.usage
        price_in, price_out = agent.price_per_mtok()
        estimate = (
            f"${usage.estimate_usd(price_in, price_out):.4f}"
            if price_in or price_out
            else "unknown — no published price for this model"
        )
        console.print(
            f"[dim]input {usage.input_tokens:,} · cache write {usage.cache_write_tokens:,} · "
            f"cache read {usage.cache_read_tokens:,} · output {usage.output_tokens:,}\n"
            f"estimated spend this session: {estimate} "
            f"({agent.provider_id or 'no provider'} · {agent.model or '—'})\n"
            "this is a local estimate from published rates — your provider's "
            "dashboard is the real number[/dim]"
        )
    else:
        console.print(f"[yellow]Unknown command {name}. Try /help.[/yellow]")
    return True


# Byte-order marks and zero-width characters arrive from piped input (PowerShell
# prepends a BOM) and from copy-pasted text. A leading one would stop "/exit"
# from being recognised as a command and send it to the model instead.
_INVISIBLE = (
    "\ufeff"  # byte-order mark
    "\u200b"  # zero-width space
    "\u200c"  # zero-width non-joiner
    "\u200d"  # zero-width joiner
    "\u2060"  # word joiner
)


def _clean_input(raw: str) -> str:
    return raw.strip().strip(_INVISIBLE).strip()


def _chat(agent: Agent) -> int:
    from .providers import catalog
    from .tools.google_auth import cached_account_email

    active = agent.provider_id
    provider_class = catalog.provider_class(active) if active else None
    label = f"{provider_class.name} · {agent.model}" if provider_class else ""
    ui.print_banner(agent.config, cached_account_email(), label)
    while True:
        try:
            user_input = _clean_input(console.input("\n[bold cyan]you[/bold cyan] "))
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Bye.[/dim]")
            return 0

        if not user_input:
            continue
        if user_input.startswith("/"):
            if not _handle_command(user_input, agent):
                console.print("[dim]Bye.[/dim]")
                return 0
            continue

        console.print()
        try:
            agent.send(user_input)
        except ProviderNotConfigured as exc:
            # The lock, reached from the chat loop: the key was removed or the
            # active provider changed while the session was open.
            console.print(f"[red]{exc}[/red]\n[dim]Use /providers to connect one.[/dim]")
        except ProviderError as exc:
            console.print(f"[red]{exc.message}[/red]")
            if exc.code == "INVALID_API_KEY":
                console.print("[dim]Use /providers to replace the key.[/dim]")
        except KeyboardInterrupt:
            console.print("\n[dim]Interrupted.[/dim]")
        finally:
            # Ctrl+C during a tool call skips the callback that would normally
            # stop the spinner, leaving it repainting over the next prompt.
            agent.callbacks.idle()


def _pause_if_windowed() -> None:
    """Keep a double-clicked .exe window open long enough to read the error."""
    if is_frozen():
        with contextlib.suppress(EOFError, KeyboardInterrupt):
            console.input("\n[dim]Press Enter to close…[/dim] ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent",
        description="An AI assistant with access to your Gmail, Drive, Calendar, Contacts and Tasks.",
    )
    parser.add_argument("--check", action="store_true", help="verify setup and exit")
    parser.add_argument("--auth", action="store_true", help="sign in to Google and exit")
    parser.add_argument("--setup", action="store_true", help="re-run first-time setup")
    parser.add_argument(
        "--providers",
        action="store_true",
        help="manage AI providers and API keys, then exit",
    )
    parser.add_argument(
        "-w", "--web", action="store_true", help="open the chat window in your browser"
    )
    parser.add_argument(
        "-p", "--prompt", help="run a single prompt non-interactively and print the answer"
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="auto-approve actions that normally ask first (sending mail, sharing, deleting)",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="fire any reminders that are due and exit (what the scheduled task runs)",
    )
    parser.add_argument(
        "--reminders-install",
        action="store_true",
        help="let Windows fire reminders even when this app is closed",
    )
    parser.add_argument(
        "--reminders-uninstall",
        action="store_true",
        help="stop Windows firing reminders when the app is closed",
    )
    parser.add_argument(
        "--reminders-status",
        action="store_true",
        help="report whether scheduled reminder notifications are set up",
    )
    args = parser.parse_args(argv)

    # Before anything heavier: this path runs once a minute, all day.
    if args.notify:
        return _notify_sweep()
    if args.reminders_install:
        return _reminders_schedule("install")
    if args.reminders_uninstall:
        return _reminders_schedule("uninstall")
    if args.reminders_status:
        return _reminders_schedule("status")

    if args.check:
        return _check()
    if args.auth:
        return _auth()

    manager = shared_manager()

    if args.providers:
        provider_ui.manage(manager)
        return 0

    config = load_config()

    # The chat window carries the whole of setup itself — the API Keys screen
    # and the Google sign-in are both reachable from the page — so it opens
    # whatever state this install is in and asks for what it needs there. A
    # terminal prompt in front of it would be unanswerable anyway when the
    # launcher runs from a shortcut with no console attached. --setup still
    # forces the terminal walkthrough, because that is what it was asked for.
    if args.setup or (onboarding.needs_onboarding(manager) and not args.web):
        updated = onboarding.run(config, manager)
        if updated is None:
            console.print("[yellow]Setup was not completed, so the app cannot start.[/yellow]")
            _pause_if_windowed()
            return 1
        config = updated

    if args.yes:
        config.auto_approve = True

    agent = Agent(config, ui.TerminalCallbacks(config), manager)

    if args.web or not args.prompt:
        # Reminders ring while the app is open whether or not the scheduled task
        # exists; the task is what covers the app being closed. Both claim from
        # the same table, so a reminder is still announced exactly once.
        from .notify import start_watcher

        start_watcher()

    if args.web:
        from .tools.google_auth import cached_account_email
        from .web import serve

        serve(agent, cached_account_email())
        return 0

    if args.prompt:
        try:
            agent.send(args.prompt)
        except (ProviderError, ProviderNotConfigured) as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
        return 0

    code = _chat(agent)
    return code


def _log_crash() -> Path | None:
    """Record an unhandled failure where it can be found afterwards.

    Launched from the desktop shortcut there is no console, so a crash would
    otherwise be a double-click that visibly does nothing at all.
    """
    import traceback
    from datetime import datetime

    path = DATA_DIR / "crash.log"
    try:
        with path.open("a", encoding="utf-8") as log:
            log.write(f"\n===== {datetime.now().isoformat(timespec='seconds')} =====\n")
            traceback.print_exc(file=log)
        return path
    except OSError:
        return None  # nothing useful left to do; the original error still shows below


def cli() -> int:
    """Entry point for both `python -m agent` and the packaged executable."""
    try:
        return main()
    except Exception:
        path = _log_crash()
        # show_locals stays off, and is passed explicitly rather than left to
        # rich's default: a frame inside the provider layer holds an API key in
        # a local, and a crash screen is not somewhere it may appear.
        console.print_exception(show_locals=False)
        if path:
            console.print(f"[dim]Written to {path}[/dim]")
        _pause_if_windowed()
        return 1


if __name__ == "__main__":
    sys.exit(cli())
