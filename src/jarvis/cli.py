"""The terminal face: a REPL, one-shot questions, and the launchers."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from pymongo.errors import PyMongoError
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import APPROVAL_POLICIES, EFFORT_LEVELS, JarvisConfig, credentials_available
from .errors import JarvisError
from .events import (
    ErrorEvent,
    Notice,
    ReminderFired,
    TextDelta,
    ThinkingDelta,
    ToolFinished,
    ToolStarted,
    TurnFinished,
)
from .session import Session

console = Console()

BANNER = r"""
     ╦╔═╗╦═╗╦  ╦╦╔═╗
     ║╠═╣╠╦╝╚╗╔╝║╚═╗
    ╚╝╩ ╩╩╚═ ╚╝ ╩╚═╝
"""

HELP = """\
[bold]Commands[/bold]
  /help                 this
  /tools                what Jarvis can do
  /memory               what Jarvis remembers
  /reminders            pending reminders
  /tasks                delegated background work
  /approve <policy>     auto | prompt | deny
  /thinking [on|off]    show summarized reasoning
  /new                  start a fresh conversation
  /exit                 quit

Ctrl-C stops the current turn. Ctrl-D exits.
"""


def _short(value: Any, limit: int = 60) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _describe(args: dict[str, Any]) -> str:
    if not args:
        return ""
    for key in ("path", "command", "query", "pattern", "task", "key", "text", "id"):
        if key in args:
            return _short(args[key])
    return _short(next(iter(args.values())))


class Terminal:
    """Renders the event stream to a terminal."""

    def __init__(self, session: Session, show_thinking: bool = True) -> None:
        self.session = session
        self.show_thinking = show_thinking
        self._mode: str | None = None

    # -- rendering -----------------------------------------------------
    def _switch(self, mode: str) -> None:
        if self._mode != mode:
            if self._mode is not None:
                console.print()
            self._mode = mode

    def handle(self, event: Any) -> None:
        if isinstance(event, TextDelta):
            self._switch("text")
            console.print(event.text, end="", markup=False, highlight=False)
        elif isinstance(event, ThinkingDelta):
            if self.show_thinking:
                self._switch("thinking")
                console.print(event.text, end="", style="dim italic", markup=False, highlight=False)
        elif isinstance(event, ToolStarted):
            self._switch("tool")
            console.print(
                f"  [cyan]⚙[/cyan] [bold]{escape(event.name)}[/bold] "
                f"[dim]{escape(_describe(event.input))}[/dim]"
            )
        elif isinstance(event, ToolFinished):
            self._mode = "tool"
            if event.is_error:
                console.print(f"    [red]✗[/red] [dim]{escape(_short(event.result, 100))}[/dim]")
            else:
                console.print(f"    [green]✓[/green] [dim]{event.duration_ms}ms[/dim]")
        elif isinstance(event, Notice):
            style = {"warn": "yellow", "debug": "dim"}.get(event.level, "dim")
            self._switch("notice")
            console.print(f"  [{style}]{escape(event.message)}[/{style}]")
        elif isinstance(event, ReminderFired):
            console.print()
            console.print(
                Panel(escape(event.text), title="⏰ reminder", border_style="yellow", expand=False)
            )
            self._mode = None
        elif isinstance(event, ErrorEvent):
            self._switch("error")
            console.print(f"  [red]{escape(event.message)}[/red]")
        elif isinstance(event, TurnFinished):
            if self._mode is not None:
                console.print()
            self._mode = None

    # -- approval ------------------------------------------------------
    def confirm(self, action: str, detail: str) -> bool:
        console.print()
        console.print(
            f"  [yellow]{escape(action)}:[/yellow] "
            f"[dim]{escape(_short(detail, 200))}[/dim]"
        )
        try:
            answer = console.input("  allow? [bold]\\[y/N][/bold] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("  [red]declined[/red]")
            return False
        return answer in {"y", "yes"}

    # -- turns ---------------------------------------------------------
    def ask(self, text: str) -> None:
        try:
            for event in self.session.send(text):
                self.handle(event)
        except KeyboardInterrupt:
            self.session.interrupt()
            console.print("\n  [yellow]stopped[/yellow]")
            self._mode = None
        except Exception as exc:  # one bad turn should not end the session
            console.print(f"\n  [red]{escape(f'{type(exc).__name__}: {exc}')}[/red]")
            self._mode = None


# ----------------------------------------------------------------------
# slash commands
# ----------------------------------------------------------------------
def _show_tools(session: Session) -> None:
    table = Table(box=None, pad_edge=False)
    table.add_column("tool", style="bold cyan")
    table.add_column("what it does", style="dim")
    for name in session.registry.names():
        tool = session.registry.get(name)
        table.add_row(name, escape(_short(tool.description, 90)))
    if session.config.web_tools:
        table.add_row("web_search", "Search the web (runs on Anthropic's servers)")
        table.add_row("web_fetch", "Fetch a page already mentioned in the conversation")
    console.print(table)


def _show_memory(session: Session) -> None:
    facts = session.store.recall(limit=200)
    if not facts:
        console.print("  [dim]nothing remembered yet[/dim]")
        return
    table = Table(box=None, pad_edge=False)
    table.add_column("key", style="bold")
    table.add_column("value")
    table.add_column("category", style="dim")
    for fact in facts:
        table.add_row(escape(fact.key), escape(_short(fact.value, 70)), escape(fact.category))
    console.print(table)


def _show_reminders(session: Session) -> None:
    from .tools.reminder_tools import list_reminders

    listing = list_reminders(session.context, {})
    for line in listing.splitlines():
        console.print(f"  [dim]{escape(line)}[/dim]")


def _show_tasks(session: Session) -> None:
    tasks = session.tasks.all()
    if not tasks:
        console.print("  [dim]nothing delegated yet[/dim]")
        return
    for task in tasks:
        colour = {"done": "green", "failed": "red"}.get(task.status, "yellow")
        console.print(
            f"  [{colour}]{task.status:7}[/{colour}] [bold]{task.id}[/bold] "
            f"[dim]({task.profile}, {task.duration})[/dim] {escape(_short(task.description, 60))}"
        )


def handle_command(line: str, terminal: Terminal) -> bool:
    """Run a /command. Returns False when it is time to quit."""
    session = terminal.session
    command, _, argument = line[1:].partition(" ")
    command, argument = command.strip().lower(), argument.strip()

    if command in {"exit", "quit", "q"}:
        return False
    if command in {"help", "h", "?"}:
        console.print(HELP)
    elif command == "tools":
        _show_tools(session)
    elif command == "memory":
        _show_memory(session)
    elif command == "reminders":
        _show_reminders(session)
    elif command == "tasks":
        _show_tasks(session)
    elif command == "new":
        session.reset()
        console.print("  [dim]fresh conversation[/dim]")
    elif command == "approve":
        if argument in APPROVAL_POLICIES:
            session.config.approval = argument
            console.print(f"  [dim]approval policy: {argument}[/dim]")
        else:
            console.print(f"  [red]choose one of {', '.join(APPROVAL_POLICIES)}[/red]")
    elif command == "thinking":
        terminal.show_thinking = argument != "off" if argument else not terminal.show_thinking
        console.print(f"  [dim]thinking display: {'on' if terminal.show_thinking else 'off'}[/dim]")
    else:
        console.print(f"  [red]unknown command {command!r}[/red] [dim]- try /help[/dim]")
    return True


# ----------------------------------------------------------------------
# entry points
# ----------------------------------------------------------------------
def repl(config: JarvisConfig) -> int:
    session = Session(config, interface="cli")
    terminal = Terminal(session)
    session.context.confirm = terminal.confirm
    session.start_reminders(terminal.handle)

    console.print(f"[bold cyan]{BANNER}[/bold cyan]", highlight=False)
    console.print(
        f"  [dim]{config.model} · workspace {config.workspace} · "
        f"approval {config.approval} · /help[/dim]\n"
    )

    try:
        while True:
            try:
                line = console.input("[bold cyan]›[/bold cyan] ").strip()
            except KeyboardInterrupt:
                console.print("  [dim]use /exit or Ctrl-D to leave[/dim]")
                continue
            except EOFError:
                break
            if not line:
                continue
            if line.startswith("/"):
                if not handle_command(line, terminal):
                    break
                continue
            terminal.ask(line)
            console.print()
    finally:
        session.close()
    console.print("[dim]goodbye[/dim]")
    return 0


def ask_once(config: JarvisConfig, question: str) -> int:
    with Session(config, interface="cli") as session:
        terminal = Terminal(session, show_thinking=False)
        session.context.confirm = terminal.confirm
        terminal.ask(question)
    console.print()
    return 0


def doctor(config: JarvisConfig) -> int:
    console.print("[bold]jarvis doctor[/bold]\n")
    ok = True

    def check(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        mark = "[green]✓[/green]" if good else "[red]✗[/red]"
        console.print(f"  {mark} {label}" + (f" [dim]{detail}[/dim]" if detail else ""))

    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    profile = Path.home() / ".config" / "anthropic"
    check(
        "credentials",
        credentials_available(),
        "ANTHROPIC_API_KEY" if has_key else (
            "using an ant auth profile" if profile.exists()
            else "set ANTHROPIC_API_KEY or run: ant auth login"
        ),
    )
    check("workspace", config.workspace.is_dir(), str(config.workspace))
    try:
        config.ensure_dirs()
        check("data directory", True, str(config.data_dir))
    except OSError as exc:
        check("data directory", False, str(exc))

    from .memory import MongoStore, StorageError, redact_uri

    store = None
    try:
        store = MongoStore(config.mongodb_uri, config.mongodb_db, ensure_indexes=False)
        store.ping()
        check("mongodb", True, f"{redact_uri(config.mongodb_uri)} -> {config.mongodb_db}")
    except StorageError as exc:
        check("mongodb", False, str(exc))
    except Exception as exc:
        check("mongodb", False, f"{type(exc).__name__}: {exc}")
    finally:
        if store is not None:
            store.close()
    check("shell", shutil.which("bash") is not None, "bash")

    console.print("\n  [bold]optional[/bold]")
    for label, module, extra in (
        ("web UI", "fastapi", "web"),
        ("audio capture", "sounddevice", "voice"),
        ("speech to text", "faster_whisper", "voice"),
        ("speech out", "pyttsx3", "voice"),
    ):
        try:
            __import__(module)
            console.print(f"  [green]✓[/green] {label}")
        except ImportError:
            console.print(
                f"  [yellow]-[/yellow] {label} "
                f"[dim]pip install 'jarvis{escape('[' + extra + ']')}'[/dim]"
            )

    console.print()
    console.print("[green]ready[/green]" if ok else "[red]something needs fixing[/red]")
    return 0 if ok else 1


def memory_command(config: JarvisConfig, args: argparse.Namespace) -> int:
    from .memory import MongoStore

    store = MongoStore(config.mongodb_uri, config.mongodb_db)
    store.ping()
    try:
        if args.forget:
            console.print(
                f"  forgot {args.forget!r}" if store.forget(args.forget) else "  nothing stored"
            )
            return 0
        facts = store.recall(args.query or "", limit=200)
        if not facts:
            console.print("  [dim]nothing remembered yet[/dim]")
            return 0
        for fact in facts:
            console.print(
                f"  [bold]{escape(fact.key)}[/bold] "
                f"[dim]({escape(fact.category)})[/dim]: {escape(fact.value)}"
            )
        return 0
    finally:
        store.close()


def reminders_command(config: JarvisConfig, args: argparse.Namespace) -> int:
    from .memory import MongoStore
    from .tools.reminder_tools import _local

    store = MongoStore(config.mongodb_uri, config.mongodb_db)
    store.ping()
    try:
        if args.cancel:
            ok = store.cancel_reminder(args.cancel)
            console.print(f"  cancelled #{args.cancel}" if ok else "  no pending reminder")
            return 0
        reminders = store.list_reminders(args.status)
        if not reminders:
            console.print(f"  [dim]no {args.status} reminders[/dim]")
            return 0
        for reminder in reminders:
            repeat = (
                f" [dim](repeats {reminder.recurrence})[/dim]" if reminder.recurrence else ""
            )
            console.print(
                f"  [bold]#{reminder.id}[/bold] {_local(reminder.due_at)}"
                f"{repeat}: {escape(reminder.text)}"
            )
        return 0
    finally:
        store.close()


# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarvis", description="A personal assistant: terminal, voice, and web."
    )
    parser.add_argument("--version", action="version", version=f"jarvis {__version__}")
    parser.add_argument(
        "--workspace", type=Path, help="Directory file and shell tools are confined to."
    )
    parser.add_argument("--model", help="Model id (default: claude-opus-5).")
    parser.add_argument(
        "--effort", choices=EFFORT_LEVELS, help="How hard the model works per turn."
    )
    parser.add_argument(
        "--approval", choices=APPROVAL_POLICIES, help="Approval policy for side effects."
    )
    parser.add_argument(
        "--no-web-tools", action="store_true", help="Run without web search and fetch."
    )

    sub = parser.add_subparsers(dest="command")
    ask = sub.add_parser("ask", help="Ask one question and exit.")
    ask.add_argument("question", nargs="+")
    sub.add_parser("chat", help="Start the terminal REPL (the default).")

    web = sub.add_parser("web", help="Serve the browser UI.")
    web.add_argument("--host")
    web.add_argument("--port", type=int)
    web.add_argument("--open", action="store_true", help="Open a browser window.")

    voice = sub.add_parser("voice", help="Listen for the wake word and talk.")
    voice.add_argument("--wake-word")
    voice.add_argument("--once", action="store_true", help="Handle one utterance and exit.")
    voice.add_argument("--no-wake-word", action="store_true", help="Skip the wake word.")

    sub.add_parser("doctor", help="Check the setup.")

    memory = sub.add_parser("memory", help="Inspect what Jarvis remembers.")
    memory.add_argument("query", nargs="?")
    memory.add_argument("--forget", metavar="KEY")

    reminders = sub.add_parser("reminders", help="Inspect reminders.")
    reminders.add_argument(
        "--status", default="pending", choices=["pending", "done", "cancelled", "all"]
    )
    reminders.add_argument("--cancel", type=int, metavar="ID")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = JarvisConfig.load(
            workspace=args.workspace,
            model=args.model,
            effort=args.effort,
            approval=args.approval,
            web_tools=False if args.no_web_tools else None,
        )
    except JarvisError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2

    try:
        if args.command == "doctor":
            return doctor(config)
        if args.command == "memory":
            return memory_command(config, args)
        if args.command == "reminders":
            return reminders_command(config, args)
        if args.command == "ask":
            return ask_once(config, " ".join(args.question))
        if args.command == "web":
            from .web.server import serve

            return serve(config, host=args.host, port=args.port, open_browser=args.open)
        if args.command == "voice":
            from .voice.loop import run_voice

            if args.wake_word:
                config.wake_word = args.wake_word
            return run_voice(config, once=args.once, use_wake_word=not args.no_wake_word)
        return repl(config)
    except JarvisError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    except PyMongoError as exc:
        from .memory import unreachable_message

        console.print(f"[red]{escape(unreachable_message(config.mongodb_uri, exc))}[/red]")
        return 1
    except KeyboardInterrupt:
        console.print("\n[dim]interrupted[/dim]")
        return 130
    finally:
        from .memory import close_clients

        close_clients()


if __name__ == "__main__":
    sys.exit(main())
