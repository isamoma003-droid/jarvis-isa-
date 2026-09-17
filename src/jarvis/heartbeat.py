"""The heartbeat: the loop that lets Jarvis act without being spoken to.

One background thread, separate from any conversation. It wakes on an interval,
sweeps reminders that came due, runs whichever configured checks are due, and
decides - per result - whether the outcome is worth interrupting for.

The governing rule is **quiet by default**. A check that surfaces nothing most of
the time is a check working correctly. Anything noteworthy becomes a Notice in the
store first and an interruption second, which is what makes the two hard parts
work: a notice raised while nobody was attached is still there when you come back,
and a restart resumes the schedule instead of firing everything at once.

Nothing here talks to the model. Checks are shell commands and file watches the
user wrote in their own config, so a heartbeat tick costs nothing and cannot
surprise anyone with a bill.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from .events import Event, NoticeSurfaced, ReminderFired
from .memory import NOTICE_LEVELS, MongoStore, Notice, Reminder, utcnow
from .reminders import _UNITS, ReminderScheduler

if TYPE_CHECKING:  # pragma: no cover
    from .audit import AuditLog
    from .config import JarvisConfig

CHECK_KINDS = ("shell", "file")
SURFACE_MODES = ("on_fail", "on_match", "on_change", "always", "on_missing", "on_exists")
DEFAULT_CHECK_TIMEOUT = 30
# How long a run may hold its claim before another heartbeat may retry it. A
# process that dies mid-check would otherwise wedge that check permanently.
CLAIM_LEASE_SECONDS = 900


class CheckError(ValueError):
    """A check definition does not make sense."""


def parse_every(text: str, default: int = 3600) -> int:
    """'30m', '2h', '90s', '1d' -> seconds. Bare numbers are seconds."""
    raw = (text or "").strip().lower()
    if not raw:
        return default
    if raw.isdigit():
        return max(1, int(raw))
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-z]+)", raw)
    if not match or match.group(2) not in _UNITS:
        raise CheckError(f"could not read {text!r} as an interval - try '30m', '2h', '1d'")
    unit = _UNITS[match.group(2)]
    return max(1, int(timedelta(**{unit: float(match.group(1))}).total_seconds()))


def parse_quiet_hours(window: str) -> tuple[time, time] | None:
    """'22:00-07:00' -> (start, end). Empty or malformed means no quiet hours."""
    raw = (window or "").strip()
    if not raw:
        return None
    match = re.fullmatch(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", raw)
    if not match:
        raise CheckError(f"quiet_hours must look like '22:00-07:00', got {window!r}")
    start_h, start_m, end_h, end_m = (int(group) for group in match.groups())
    if start_h > 23 or end_h > 23 or start_m > 59 or end_m > 59:
        raise CheckError(f"quiet_hours has an impossible time: {window!r}")
    return time(start_h, start_m), time(end_h, end_m)


def in_quiet_hours(moment: datetime, window: tuple[time, time] | None) -> bool:
    """Whether local `moment` falls inside the window, wrapping over midnight."""
    if window is None:
        return False
    start, end = window
    now = moment.timetz().replace(tzinfo=None) if moment.tzinfo else moment.time()
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end  # the window crosses midnight


def should_interrupt(
    level: str, moment: datetime, window: tuple[time, time] | None, paused: bool = False
) -> bool:
    """Whether this notice has earned the right to break into the user's day."""
    if paused:
        return False
    if level == "urgent":
        return True  # the whole point of urgent is that quiet hours do not apply
    if level == "notify":
        return not in_quiet_hours(moment, window)
    return False


@dataclass
class Check:
    """One scheduled thing to look at.

    `surface` is the interesting field: it is how a check decides that what it
    found is worth anyone's attention. Most ticks should decide 'no'.
    """

    name: str
    kind: str = "shell"
    every: str = "1h"
    level: str = "quiet"
    surface: str = "on_fail"
    enabled: bool = True
    command: str = ""
    path: str = ""
    match: str = ""
    message: str = ""
    timeout: int = DEFAULT_CHECK_TIMEOUT

    def __post_init__(self) -> None:
        if not self.name or not str(self.name).strip():
            raise CheckError("every check needs a name")
        self.name = str(self.name).strip()
        if self.kind not in CHECK_KINDS:
            raise CheckError(f"check {self.name!r}: kind must be one of {CHECK_KINDS}")
        if self.level not in NOTICE_LEVELS:
            raise CheckError(f"check {self.name!r}: level must be one of {NOTICE_LEVELS}")
        if self.surface not in SURFACE_MODES:
            raise CheckError(f"check {self.name!r}: surface must be one of {SURFACE_MODES}")
        if self.kind == "shell" and not self.command:
            raise CheckError(f"check {self.name!r}: a shell check needs a command")
        if self.kind == "file" and not self.path:
            raise CheckError(f"check {self.name!r}: a file check needs a path")
        if self.surface == "on_match" and not self.match:
            raise CheckError(f"check {self.name!r}: surface='on_match' needs a match pattern")
        if self.match:
            try:
                re.compile(self.match)
            except re.error as exc:
                raise CheckError(f"check {self.name!r}: bad match pattern - {exc}") from exc
        self.interval = parse_every(self.every)

    interval: int = field(init=False, default=3600)


@dataclass
class CheckResult:
    """What one run of a check found."""

    surfaced: bool
    text: str = ""
    detail: str = ""
    status: str = "ok"
    fingerprint: str = ""


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def load_checks(raw: list[Any]) -> list[Check]:
    """Turn the config's list of tables into Check objects, rejecting nonsense early."""
    checks: list[Check] = []
    seen: set[str] = set()
    for entry in raw or []:
        if not isinstance(entry, dict):
            raise CheckError(f"a check must be a table of settings, got {type(entry).__name__}")
        known = {f for f in Check.__dataclass_fields__ if f != "interval"}
        unknown = set(entry) - known
        if unknown:
            raise CheckError(f"unknown check settings: {sorted(unknown)}")
        check = Check(**entry)
        if check.name in seen:
            raise CheckError(f"two checks are both called {check.name!r}")
        seen.add(check.name)
        checks.append(check)
    return checks


# ----------------------------------------------------------------------
# running a check
# ----------------------------------------------------------------------
def run_shell_check(check: Check, workspace: Any, previous: str = "") -> CheckResult:
    """Run the command and decide whether its output is worth surfacing."""
    try:
        completed = subprocess.run(  # noqa: S602 - the user wrote this command in their config
            ["bash", "-lc", check.command],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=max(1, int(check.timeout)),
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            surfaced=True,
            text=f"{check.name}: check timed out after {check.timeout}s",
            detail=check.command,
            status="timeout",
        )
    except (OSError, ValueError) as exc:
        return CheckResult(
            surfaced=True,
            text=f"{check.name}: could not run - {exc}",
            detail=check.command,
            status="error",
        )

    output = (completed.stdout + completed.stderr).strip()
    fingerprint = _fingerprint(output)
    failed = completed.returncode != 0

    if check.surface == "always":
        surfaced = True
    elif check.surface == "on_fail":
        surfaced = failed
    elif check.surface == "on_match":
        surfaced = bool(re.search(check.match, output, re.MULTILINE))
    elif check.surface == "on_change":
        # A first run has nothing to compare against, so it establishes the
        # baseline quietly rather than announcing everything it sees.
        surfaced = bool(previous) and fingerprint != previous
    else:
        surfaced = failed

    return CheckResult(
        surfaced=surfaced,
        text=check.message or f"{check.name}: {_headline(output) or 'no output'}",
        detail=output,
        status="fail" if failed else "ok",
        fingerprint=fingerprint,
    )


def run_file_check(check: Check, workspace: Any, previous: str = "") -> CheckResult:
    """Watch a path for appearing, disappearing, or changing."""
    from pathlib import Path

    target = Path(check.path).expanduser()
    if not target.is_absolute():
        target = Path(workspace) / target

    exists = target.exists()
    stamp = ""
    if exists:
        try:
            stat = target.stat()
            stamp = _fingerprint(f"{stat.st_mtime_ns}:{stat.st_size}")
        except OSError as exc:
            return CheckResult(
                surfaced=True,
                text=f"{check.name}: cannot read {target}",
                detail=str(exc),
                status="error",
            )

    if check.surface == "on_missing":
        surfaced, note = (not exists), "is missing"
    elif check.surface == "on_exists":
        surfaced, note = exists, "exists"
    elif check.surface == "always":
        surfaced, note = True, "exists" if exists else "is missing"
    else:  # on_change, and the on_fail default
        surfaced = bool(previous) and stamp != previous
        note = "changed"

    return CheckResult(
        surfaced=surfaced,
        text=check.message or f"{check.name}: {target} {note}",
        detail=str(target),
        status="ok" if exists else "missing",
        fingerprint=stamp,
    )


RUNNERS: dict[str, Callable[[Check, Any, str], CheckResult]] = {
    "shell": run_shell_check,
    "file": run_file_check,
}


def _headline(output: str, limit: int = 160) -> str:
    """The first meaningful line of output, for the notice text."""
    for line in output.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped if len(stripped) <= limit else stripped[: limit - 1] + "…"
    return ""


# ----------------------------------------------------------------------
# the loop
# ----------------------------------------------------------------------
class Heartbeat:
    """The background loop. Owns reminders and scheduled checks, nothing else."""

    def __init__(
        self,
        config: JarvisConfig,
        store: MongoStore,
        on_event: Callable[[Event], None] | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.on_event = on_event or (lambda event: None)
        # Whether anything is actually listening. A notice is only marked
        # delivered when someone was there to receive it - deciding that from
        # policy alone is how a proactive feature loses what you missed.
        self.attached = on_event is not None
        self.audit = audit
        self.checks = load_checks(config.checks)
        self.quiet_window = parse_quiet_hours(config.quiet_hours)
        self.poll_seconds = max(1, int(config.heartbeat_seconds))
        self.reminders = ReminderScheduler(
            store, self._reminder_due, poll_seconds=self.poll_seconds
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._local_claims: set[str] = set()
        self._lock = threading.Lock()
        # Where `surface` drops notices so `tick` can return them. The scheduler
        # hands back fired reminders, not the notices they became.
        self._raised: list[Notice] = []

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        """Begin beating. Seeds the schedule without firing everything at once."""
        self.prime()
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="jarvis-heartbeat", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    def detach(self) -> None:
        """The interface has gone. Keep beating, but hold everything raised.

        Called when a terminal exits or a browser disconnects while the loop
        outlives it. From here on notices stay `pending` and wait for whoever
        attaches next.
        """
        self.attached = False

    def prime(self, now: datetime | None = None) -> None:
        """Give every check a due time, keeping any it already has.

        A check the store has never seen is due one interval from now, not
        immediately - otherwise every restart would fire the whole set on boot.
        """
        moment = now or utcnow()
        for check in self.checks:
            self.store.schedule_check(check.name, moment + timedelta(seconds=check.interval))
        self.store.forget_checks([check.name for check in self.checks])

    @property
    def paused(self) -> bool:
        return self.store.is_paused()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # a bad check must never stop the heartbeat
                pass
            self._stop.wait(self.poll_seconds)

    # -- one beat ------------------------------------------------------
    def tick(self, now: datetime | None = None) -> list[Notice]:
        """Sweep reminders and due checks. Returns the notices raised."""
        moment = now or utcnow()
        if self.paused:
            return []  # the kill switch: the loop keeps turning, nothing acts
        with self._lock:
            self._raised = []
        self.reminders.tick(moment)
        for check in self.checks:
            if self._stop.is_set():
                break
            self.run_check(check, moment)
        with self._lock:
            notices, self._raised = self._raised, []
        return notices

    def run_check(self, check: Check, now: datetime | None = None) -> Notice | None:
        """Run one check if it is due and not already running. Returns any notice."""
        moment = now or utcnow()
        if not check.enabled:
            return None
        with self._lock:
            if check.name in self._local_claims:
                return None  # still running here; skipping beats stacking runs up
            if not self.store.claim_check(check.name, moment, CLAIM_LEASE_SECONDS):
                return None  # not due, or another heartbeat has it
            self._local_claims.add(check.name)

        state = self.store.check_state(check.name)
        previous = str(state.get("fingerprint") or "")
        runner = RUNNERS.get(check.kind, run_shell_check)
        try:
            result = runner(check, self.config.workspace, previous)
        except Exception as exc:  # a broken check reports itself and carries on
            result = CheckResult(
                surfaced=True,
                text=f"{check.name}: check raised {type(exc).__name__}",
                detail=str(exc),
                status="error",
            )
        finally:
            # Released whatever happened: a check that keeps its claim after a
            # crash never runs again until the lease expires.
            with self._lock:
                self._local_claims.discard(check.name)

        self.store.release_check(
            check.name,
            moment + timedelta(seconds=check.interval),
            status=result.status,
            fingerprint=result.fingerprint,
        )

        if self.audit is not None:
            self.audit.record(
                "check",
                name=check.name,
                status=result.status,
                surfaced=result.surfaced,
                detail=result.detail if result.surfaced else "",
            )
        if not result.surfaced:
            return None
        return self.surface(
            source=f"check:{check.name}",
            text=result.text,
            detail=result.detail,
            level=check.level,
            now=moment,
        )

    # -- surfacing -----------------------------------------------------
    def surface(
        self,
        source: str,
        text: str,
        detail: str = "",
        level: str = "quiet",
        now: datetime | None = None,
        event: Event | None = None,
        dedupe: bool = True,
    ) -> Notice:
        """Record a notice, then interrupt only if it has earned it.

        Three things have to be true at once, and each is its own decision:

        - **Is this news?** A condition that is still true is not. While an
          identical notice is open, the repeat is counted and nothing else
          happens; dismissing it makes the next occurrence news again.
        - **Has it earned an interruption?** Level and quiet hours decide.
        - **Is anyone there?** Only then is it marked delivered. Otherwise it
          stays `pending` and waits for whoever attaches next.
        """
        moment = now or utcnow()
        if dedupe:
            existing = self.store.duplicate_notice(source, text)
            if existing is not None:
                # Still true, still in the inbox. Saying it again is how a
                # proactive assistant gets muted.
                self.store.bump_notice(existing.id)
                return existing

        notice = self.store.add_notice(source=source, text=text, detail=detail, level=level)
        with self._lock:
            self._raised.append(notice)
        if self.audit is not None:
            self.audit.record("notice", id=notice.id, source=source, level=level, text=text)
        if should_interrupt(level, moment.astimezone(), self.quiet_window, self.paused):
            if self.attached:
                self.store.mark_delivered([notice.id])
                self.on_event(event or as_event(notice))
        return notice

    def _reminder_due(self, reminder: Reminder) -> Notice:
        """A reminder came due. It earns an interruption; quiet hours still apply.

        No de-duplication here: a recurring reminder firing again is the whole
        point of it, even if the last one is still sitting unread.
        """
        return self.surface(
            source="reminder",
            text=reminder.text,
            detail=f"reminder #{reminder.id}",
            level="notify",
            dedupe=False,
            event=ReminderFired(
                reminder_id=reminder.id, text=reminder.text, due_at=reminder.due_at
            ),
        )


def as_event(notice: Notice) -> NoticeSurfaced:
    """A stored notice as the event interfaces render. Used for catch-up too."""
    return NoticeSurfaced(
        notice_id=notice.id,
        text=notice.text,
        level=notice.level,
        source=notice.source,
        detail=notice.detail,
    )
