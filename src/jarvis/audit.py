"""The audit trail: what Jarvis did, and what it cost.

One append-only JSONL file under the data directory. Plain text on purpose - when
something surprises you at 2am, `grep` and `tail` are the tools you actually have,
and a line per event survives a half-written record far better than one big JSON
document does.

Nothing here is on the critical path: a log that cannot be written must never take
the assistant down with it, so every failure is swallowed and the turn continues.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Dollars per million tokens, input and output. Cache rates are derived: a write
# costs 1.25x input, a read 0.1x.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1
DEFAULT_PRICE = (5.00, 25.00)

MAX_DETAIL = 400


def price_for(model: str) -> tuple[float, float]:
    """Input and output dollars per million tokens for a model id.

    An unknown id is priced at the Opus tier rather than free: a cost display
    that silently reads zero is worse than one that is roughly right.
    """
    if model in PRICES:
        return PRICES[model]
    for known, price in PRICES.items():
        if model.startswith(known):
            return price
    return DEFAULT_PRICE


def estimate_cost(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Dollars for one turn. An estimate - the invoice is the source of truth."""
    rate_in, rate_out = price_for(model)
    per_token = 1_000_000
    return (
        input_tokens * rate_in
        + output_tokens * rate_out
        + cache_read_tokens * rate_in * CACHE_READ_MULTIPLIER
        + cache_write_tokens * rate_in * CACHE_WRITE_MULTIPLIER
    ) / per_token


def _trim(value: Any, limit: int = MAX_DETAIL) -> Any:
    """Keep one log line to one log line."""
    if isinstance(value, str):
        flat = " ".join(value.split())
        return flat if len(flat) <= limit else flat[: limit - 1] + "…"
    if isinstance(value, dict):
        return {key: _trim(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_trim(item, limit) for item in value[:20]]
    return value


@dataclass
class Totals:
    """A running tally. A runaway loop shows up here before it shows up on a bill."""

    turns: int = 0
    tools: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "turns": self.turns,
            "tools": self.tools,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cost": round(self.cost, 4),
        }


class AuditLog:
    """Append-only record of everything consequential, plus the running cost."""

    def __init__(self, path: Path, session_id: str = "", enabled: bool = True) -> None:
        self.path = Path(path)
        self.session_id = session_id
        self.enabled = enabled
        self.totals = Totals()
        self._lock = threading.Lock()

    # -- writing -------------------------------------------------------
    def record(self, kind: str, **fields: Any) -> None:
        """Append one event. Never raises."""
        if not self.enabled:
            return
        entry = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "kind": kind,
            "session": self.session_id,
            **{key: _trim(value) for key, value in fields.items()},
        }
        line = json.dumps(entry, default=str, ensure_ascii=False)
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # Opened per write and in append mode: several interfaces may
                # share one log, and O_APPEND keeps their lines from interleaving.
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError:
            pass

    def tool(self, name: str, args: dict[str, Any], ok: bool, ms: int, result: str = "") -> None:
        with self._lock:
            self.totals.tools += 1
        self.record(
            "tool",
            name=name,
            args=args,
            ok=ok,
            ms=ms,
            **({"error": result} if not ok and result else {}),
        )

    def approval(self, action: str, detail: str, granted: bool, why: str = "") -> None:
        self.record(
            "approval",
            action=action,
            detail=detail,
            granted=granted,
            **({"why": why} if why else {}),
        )

    def turn(
        self,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        stop_reason: str | None = None,
    ) -> float:
        """Record a finished turn and return what it cost."""
        cost = estimate_cost(model, input_tokens, output_tokens, cache_read_tokens)
        with self._lock:
            self.totals.turns += 1
            self.totals.input_tokens += input_tokens
            self.totals.output_tokens += output_tokens
            self.totals.cache_read_tokens += cache_read_tokens
            self.totals.cost += cost
            running = self.totals.cost
        self.record(
            "turn",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            stop_reason=stop_reason,
            cost=round(cost, 6),
            session_cost=round(running, 4),
        )
        return cost

    # -- reading -------------------------------------------------------
    def entries(self, limit: int = 50, kind: str = "") -> list[dict[str, Any]]:
        """The last `limit` entries, newest last. Unreadable lines are skipped."""
        rows = [row for row in _read_lines(self.path) if not kind or row.get("kind") == kind]
        return rows[-limit:]


def _read_lines(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a torn final line from a killed process
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


def read_totals(path: Path) -> Totals:
    """Add up a whole log file. For `jarvis log --totals` across sessions."""
    totals = Totals()
    for row in _read_lines(path):
        if row.get("kind") == "turn":
            totals.turns += 1
            totals.input_tokens += int(row.get("input_tokens") or 0)
            totals.output_tokens += int(row.get("output_tokens") or 0)
            totals.cache_read_tokens += int(row.get("cache_read_tokens") or 0)
            totals.cost += float(row.get("cost") or 0.0)
        elif row.get("kind") == "tool":
            totals.tools += 1
    return totals


def default_path(data_dir: Path) -> Path:
    return Path(data_dir).expanduser() / "audit.jsonl"


def audit_enabled() -> bool:
    return os.environ.get("JARVIS_AUDIT", "1").strip().lower() not in {"0", "false", "no", "off"}
