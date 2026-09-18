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
from .config import (
    APPROVAL_POLICIES,
    EFFORT_LEVELS,
    JarvisConfig,
    credentials_available,
    load_dotenv,
)
from .errors import JarvisError
from .events import (
    ErrorEvent,
    Notice,
    NoticeSurfaced,
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
  /notices              what Jarvis surfaced on its own
  /dismiss <id|all>     clear a notice
  /checks               scheduled checks and when they next run
  /tasks                delegated background work
  /pause, /resume       the kill switch: stop or restart all proactive behaviour
  /cost                 tokens and spend this session
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
        elif isinstance(event, NoticeSurfaced):
            console.print()
            border = "red" if event.level == "urgent" else "cyan"
            body = escape(event.text)
            if event.detail:
                body += f"\n[dim]{escape(_short(event.detail, 300))}[/dim]"
            console.print(
                Panel(
                    body,
                    title=f"◈ {escape(event.source)}",
                    subtitle=f"[dim]/dismiss {event.notice_id}[/dim]",
                    border_style=border,
                    expand=False,
                )
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


def _show_notices(session: Session) -> None:
    notices = session.open_notices(limit=100)
    if not notices:
        console.print("  [dim]nothing waiting[/dim]")
        return
    table = Table(box=None, pad_edge=False)
    table.add_column("id", style="bold")
    table.add_column("", width=1)
    table.add_column("source", style="dim")
    table.add_column("what")
    for notice in notices:
        mark = {"urgent": "[red]![/red]", "notify": "[yellow]*[/yellow]"}.get(notice.level, " ")
        again = f" [dim]×{notice.repeats + 1}[/dim]" if notice.repeats else ""
        table.add_row(
            f"#{notice.id}", mark, escape(notice.source), escape(_short(notice.text, 70)) + again
        )
    console.print(table)
    # Looking at the inbox counts as seeing it; clearing still takes /dismiss.
    session.store.mark_delivered([n.id for n in notices if n.status == "pending"])
    console.print("  [dim]/dismiss <id> to clear one, /dismiss all to clear the lot[/dim]")


def _show_checks(session: Session) -> None:
    from .heartbeat import load_checks
    from .tools.reminder_tools import _local

    checks = load_checks(session.config.checks)
    if not checks:
        console.print("  [dim]no checks configured - add [[jarvis.checks]] to config.toml[/dim]")
        return
    states = {doc["_id"]: doc for doc in session.store.all_check_states()}
    table = Table(box=None, pad_edge=False)
    table.add_column("check", style="bold cyan")
    table.add_column("every", style="dim")
    table.add_column("surfaces", style="dim")
    table.add_column("next", style="dim")
    table.add_column("last")
    for check in checks:
        state = states.get(check.name, {})
        due = state.get("next_due")
        last = str(state.get("last_status") or "-")
        colour = {"fail": "red", "error": "red", "timeout": "yellow"}.get(last, "green")
        table.add_row(
            check.name if check.enabled else f"{check.name} (off)",
            check.every,
            f"{check.surface} → {check.level}",
            _local(due.isoformat()) if due else "-",
            f"[{colour}]{last}[/{colour}]",
        )
    console.print(table)


def _show_cost(session: Session) -> None:
    totals = session.audit.totals
    console.print(
        f"  [bold]{totals.turns}[/bold] turns, [bold]{totals.tools}[/bold] tool calls\n"
        f"  [dim]{totals.input_tokens:,} in · {totals.output_tokens:,} out · "
        f"{totals.cache_read_tokens:,} cached[/dim]\n"
        f"  [bold]${totals.cost:.4f}[/bold] [dim]this session, estimated "
        f"({session.config.model})[/dim]"
    )


def _set_paused(session: Session, paused: bool) -> None:
    session.set_paused(paused)
    if paused:
        console.print(
            "  [yellow]paused[/yellow] [dim]- checks, reminders and background work are "
            "held. You can still talk to me. /resume to restart.[/dim]"
        )
    else:
        console.print("  [green]resumed[/green] [dim]- proactive behaviour is back on[/dim]")


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
    elif command == "notices":
        _show_notices(session)
    elif command == "dismiss":
        if argument in {"all", "*"}:
            cleared = session.store.dismiss_all_notices()
            console.print(f"  [dim]dismissed {cleared}[/dim]")
        elif argument.lstrip("#").isdigit():
            notice_id = int(argument.lstrip("#"))
            done = session.store.dismiss_notice(notice_id)
            console.print(
                f"  [dim]dismissed #{notice_id}[/dim]" if done else "  [dim]not open[/dim]"
            )
        else:
            console.print("  [red]/dismiss <id> or /dismiss all[/red]")
    elif command == "checks":
        _show_checks(session)
    elif command == "cost":
        _show_cost(session)
    elif command in {"pause", "stop"}:
        _set_paused(session, True)
    elif command in {"resume", "start"}:
        _set_paused(session, False)
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
    session.start_heartbeat(terminal.handle)

    console.print(f"[bold cyan]{BANNER}[/bold cyan]", highlight=False)
    paused = " · [yellow]paused[/yellow]" if session.paused else ""
    console.print(
        f"  [dim]{config.model} · workspace {config.workspace} · "
        f"approval {config.approval}{paused} · /help[/dim]\n"
    )

    # Anything raised while nothing was attached has been waiting for this.
    held = session.catch_up()
    if held:
        console.print(f"  [dim]while you were away ({len(held)}):[/dim]")
        for event in held:
            terminal.handle(event)
        console.print()
    waiting = len(session.open_notices(limit=100)) - len(held)
    if waiting > 0:
        console.print(f"  [dim]{waiting} quieter notice(s) in the inbox - /notices[/dim]\n")

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


def _apt_hint(packages: str) -> str:
    """An apt line, but only where apt is the right answer."""
    try:
        release = Path("/etc/os-release").read_text()
    except OSError:
        return ""
    if "debian" not in release.lower():  # covers Ubuntu, Mint, Pop!_OS, LMDE
        return ""
    return f"sudo apt install {packages}"


def _pip_hint(extra: str) -> str:
    # Plain text: the caller escapes once, at print time. Escaping here too
    # puts a literal backslash in the command the user is meant to copy.
    return f"pip install 'jarvis[{extra}]'"


def _probe_web() -> tuple[bool, str]:
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        return False, _pip_hint("web")
    return True, ""


def _probe_audio() -> tuple[bool, str]:
    """The microphone. Two separate failures, and they need different fixes."""
    try:
        import sounddevice
    except ImportError:
        return False, _pip_hint("voice")
    except OSError as exc:
        # The wheel installed fine; the system library it binds to is missing.
        # Distinct from "not installed", and a pip command will not fix it.
        hint = _apt_hint("libportaudio2") or "install PortAudio"
        return False, f"{exc} - {hint}"
    try:
        inputs = [d for d in sounddevice.query_devices() if d.get("max_input_channels", 0) > 0]
    except Exception as exc:  # noqa: BLE001 - no sound server, no devices, ...
        return False, f"no audio devices ({type(exc).__name__}: {exc})"
    if not inputs:
        return False, "PortAudio works, but no input device - is a microphone connected?"
    return True, f"{len(inputs)} input device(s)"


def _probe_stt() -> tuple[bool, str]:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False, _pip_hint("voice")
    return True, "faster-whisper (the model downloads on first use)"


def _probe_tts() -> tuple[bool, str]:
    """What it would actually speak through, not what is merely importable.

    pyttsx3 imports cleanly with no speech engine behind it and only fails when
    you ask it to talk, so importing proves nothing here.
    """
    from .voice.tts import PrintSpeaker, load_tts

    speaker = load_tts("auto")
    if isinstance(speaker, PrintSpeaker):
        hint = _apt_hint("espeak-ng") or "install a speech engine"
        return False, f"nothing to speak through - {hint} (replies will be printed)"
    return True, speaker.name


def doctor(config: JarvisConfig) -> int:
    console.print("[bold]jarvis doctor[/bold]\n")
    ok = True

    def check(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        mark = "[green]✓[/green]" if good else "[red]✗[/red]"
        console.print(f"  {mark} {label}" + (f" [dim]{detail}[/dim]" if detail else ""))

    # Checked first because nothing else can be right if this is wrong, and
    # because Linux Mint 21 still ships 3.10 - where `tomllib` and
    # `datetime.UTC` simply do not exist.
    env_file = load_dotenv()
    if env_file:
        console.print(f"  [dim]settings loaded from {escape(str(env_file))}[/dim]\n")

    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    check(
        "python",
        sys.version_info >= (3, 11),
        version if sys.version_info >= (3, 11) else f"{version} - Jarvis needs 3.11 or newer",
    )

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
        from .heartbeat import load_checks, parse_quiet_hours

        checks = load_checks(config.checks)
        window = parse_quiet_hours(config.quiet_hours)
        check(
            "heartbeat",
            True,
            f"{len(checks)} check(s), every {config.heartbeat_seconds}s, "
            f"quiet {config.quiet_hours if window else 'never'}",
        )
    except (JarvisError, ValueError) as exc:
        check("heartbeat", False, str(exc))
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
        waiting = len(store.open_notices(limit=200))
        if store.is_paused():
            console.print(
                "  [yellow]![/yellow] proactive behaviour is [yellow]paused[/yellow] "
                "[dim]- `jarvis resume` to restart it[/dim]"
            )
        if waiting:
            console.print(f"  [cyan]◈[/cyan] {waiting} notice(s) waiting [dim]- /notices[/dim]")
    except StorageError as exc:
        check("mongodb", False, str(exc))
    except Exception as exc:
        check("mongodb", False, f"{type(exc).__name__}: {exc}")
    finally:
        if store is not None:
            store.close()
    check("shell", shutil.which("bash") is not None, "bash")

    console.print("\n  [bold]optional[/bold]")
    for label, probe in (
        ("web UI", _probe_web),
        ("audio capture", _probe_audio),
        ("speech to text", _probe_stt),
        ("speech out", _probe_tts),
    ):
        # Every probe is wrapped: the command whose job is to diagnose a broken
        # setup must never itself be what breaks. A missing system library
        # raises OSError, not ImportError - that is exactly the case that used
        # to take `jarvis doctor` down on a fresh Linux box.
        try:
            good, detail = probe()
        except Exception as exc:  # noqa: BLE001 - a probe failing is a finding
            good, detail = False, f"{type(exc).__name__}: {exc}"
        mark = "[green]✓[/green]" if good else "[yellow]-[/yellow]"
        console.print(f"  {mark} {label}" + (f" [dim]{escape(detail)}[/dim]" if detail else ""))

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


def notices_command(config: JarvisConfig, args: argparse.Namespace) -> int:
    """The inbox from outside a conversation - and the way to empty it."""
    from .memory import MongoStore
    from .tools.reminder_tools import _local

    store = MongoStore(config.mongodb_uri, config.mongodb_db)
    store.ping()
    try:
        if args.dismiss is not None:
            if args.dismiss == "all":
                console.print(f"  dismissed {store.dismiss_all_notices()}")
            elif args.dismiss.lstrip("#").isdigit():
                notice_id = int(args.dismiss.lstrip("#"))
                ok = store.dismiss_notice(notice_id)
                console.print(f"  dismissed #{notice_id}" if ok else "  not open")
            else:
                console.print("  [red]--dismiss takes an id or 'all'[/red]")
                return 2
            return 0
        notices = store.open_notices(limit=200)
        if not notices:
            console.print("  [dim]nothing waiting[/dim]")
            return 0
        for notice in notices:
            mark = {"urgent": "[red]![/red]", "notify": "[yellow]*[/yellow]"}.get(
                notice.level, "[dim]-[/dim]"
            )
            console.print(
                f"  {mark} [bold]#{notice.id}[/bold] [dim]{_local(notice.created_at)} "
                f"{escape(notice.source)}[/dim] {escape(notice.text)}"
            )
        store.mark_delivered([n.id for n in notices if n.status == "pending"])
        return 0
    finally:
        store.close()


def pause_command(config: JarvisConfig, paused: bool) -> int:
    """The kill switch, usable without starting a session."""
    from .memory import MongoStore

    store = MongoStore(config.mongodb_uri, config.mongodb_db)
    store.ping()
    try:
        store.set_paused(paused)
        if paused:
            console.print(
                "[yellow]paused[/yellow] - every heartbeat holds: no checks, no reminders, "
                "no background work. Conversations still work. `jarvis resume` to restart."
            )
        else:
            console.print("[green]resumed[/green] - proactive behaviour is back on.")
        return 0
    finally:
        store.close()


def log_command(config: JarvisConfig, args: argparse.Namespace) -> int:
    """What Jarvis did, and what it cost."""
    from .audit import default_path, read_totals

    path = default_path(config.data_dir)
    if not path.exists():
        console.print(f"  [dim]no audit log yet at {path}[/dim]")
        return 0
    if args.totals:
        totals = read_totals(path)
        console.print(
            f"  [bold]{totals.turns}[/bold] turns · [bold]{totals.tools}[/bold] tool calls\n"
            f"  [dim]{totals.input_tokens:,} in · {totals.output_tokens:,} out · "
            f"{totals.cache_read_tokens:,} cached[/dim]\n"
            f"  [bold]${totals.cost:.4f}[/bold] [dim]estimated, all time[/dim]\n"
            f"  [dim]{path}[/dim]"
        )
        return 0

    from .audit import AuditLog

    rows = AuditLog(path, enabled=False).entries(limit=args.lines, kind=args.kind or "")
    if not rows:
        console.print("  [dim]nothing logged yet[/dim]")
        return 0
    colours = {"approval": "yellow", "injection": "red", "notice": "cyan", "turn": "dim"}
    for row in rows:
        kind = str(row.get("kind", "?"))
        rest = {
            key: value
            for key, value in row.items()
            if key not in {"at", "kind", "session"}
        }
        body = " ".join(f"{key}={value}" for key, value in rest.items())
        colour = colours.get(kind, "white")
        console.print(
            f"  [dim]{escape(str(row.get('at', '')))}[/dim] "
            f"[{colour}]{kind:10}[/{colour}] {escape(_short(body, 160))}"
        )
    return 0


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

    notices = sub.add_parser("notices", help="What Jarvis surfaced while you were away.")
    notices.add_argument("--dismiss", metavar="ID|all", help="Clear a notice, or all of them.")

    sub.add_parser("pause", help="Kill switch: stop all proactive behaviour.")
    sub.add_parser("resume", help="Restart proactive behaviour.")

    log = sub.add_parser("log", help="The audit trail: what Jarvis did, and what it cost.")
    log.add_argument("-n", "--lines", type=int, default=40, help="How many entries.")
    log.add_argument(
        "--kind",
        help="Only this kind: tool, approval, turn, notice, check, injection, kill_switch.",
    )
    log.add_argument("--totals", action="store_true", help="Just the tally.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Before anything reads os.environ. The repo ships a .env.example and
    # git-ignores .env, so copying it has to actually do something.
    load_dotenv()
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
        if args.command == "notices":
            return notices_command(config, args)
        if args.command in {"pause", "resume"}:
            return pause_command(config, args.command == "pause")
        if args.command == "log":
            return log_command(config, args)
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
