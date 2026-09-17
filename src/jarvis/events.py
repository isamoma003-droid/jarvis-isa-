"""The event stream every interface consumes.

The agent core is a generator of these events. The terminal prints them, the
web server forwards them over a WebSocket, and the voice loop speaks the text
ones. Adding a fourth face means handling these events, nothing more.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Event:
    """Base event. `kind` is what interfaces switch on."""

    kind: str = field(init=False, default="event")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        for slot in getattr(self, "__slots__", ()):
            if slot == "kind":
                continue
            out[slot] = getattr(self, slot)
        return out


@dataclass(slots=True)
class TextDelta(Event):
    """A fragment of the assistant's visible answer."""

    text: str
    kind: str = field(init=False, default="text")


@dataclass(slots=True)
class ThinkingDelta(Event):
    """A fragment of summarized reasoning, when thinking display is on."""

    text: str
    kind: str = field(init=False, default="thinking")


@dataclass(slots=True)
class ToolStarted(Event):
    """A tool is about to run."""

    name: str
    tool_use_id: str
    input: dict[str, Any]
    kind: str = field(init=False, default="tool_started")


@dataclass(slots=True)
class ToolFinished(Event):
    """A tool returned. `result` is already truncated for display."""

    name: str
    tool_use_id: str
    result: str
    is_error: bool = False
    duration_ms: int = 0
    kind: str = field(init=False, default="tool_finished")


@dataclass(slots=True)
class Notice(Event):
    """Something the user should know that is not part of the answer."""

    message: str
    level: str = "info"
    kind: str = field(init=False, default="notice")


@dataclass(slots=True)
class ReminderFired(Event):
    """A reminder came due."""

    reminder_id: int
    text: str
    due_at: str
    kind: str = field(init=False, default="reminder")


@dataclass(slots=True)
class TurnFinished(Event):
    """The agent finished a turn.

    `text` is the final answer - the last assistant text of the turn, not the
    running commentary that preceded tool calls. Interfaces already streamed
    that live; this is the thing to speak, log, or hand back.
    """

    text: str
    stop_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    kind: str = field(init=False, default="turn_finished")


@dataclass(slots=True)
class ErrorEvent(Event):
    """The turn failed."""

    message: str
    recoverable: bool = True
    kind: str = field(init=False, default="error")
