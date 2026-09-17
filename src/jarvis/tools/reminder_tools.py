"""Reminder tools."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..errors import ToolError
from ..reminders import TimeParseError, parse_recurrence, parse_when
from .base import Tool, ToolContext


def _local(iso_string: str) -> str:
    return datetime.fromisoformat(iso_string).astimezone().strftime("%a %d %b %Y at %H:%M")


def set_reminder(ctx: ToolContext, args: dict[str, Any]) -> str:
    try:
        due = parse_when(args["when"])
        recurrence = parse_recurrence(args.get("repeat"))
    except TimeParseError as exc:
        raise ToolError(str(exc)) from exc
    reminder = ctx.store.add_reminder(args["text"], due, recurrence)
    repeat = f", repeating {recurrence}" if recurrence else ""
    return f"Reminder #{reminder.id} set for {_local(reminder.due_at)}{repeat}: {reminder.text}"


def list_reminders(ctx: ToolContext, args: dict[str, Any]) -> str:
    status = args.get("status", "pending")
    reminders = ctx.store.list_reminders(status=status)
    if not reminders:
        return f"No {status} reminders."
    lines = []
    for reminder in reminders:
        repeat = f" (repeats {reminder.recurrence})" if reminder.recurrence else ""
        lines.append(f"#{reminder.id} {_local(reminder.due_at)}{repeat}: {reminder.text}")
    return "\n".join(lines)


def cancel_reminder(ctx: ToolContext, args: dict[str, Any]) -> str:
    reminder_id = int(args["id"])
    if ctx.store.cancel_reminder(reminder_id):
        return f"Cancelled reminder #{reminder_id}."
    return f"No pending reminder #{reminder_id}."


TOOLS = [
    Tool(
        name="set_reminder",
        description=(
            "Schedule a reminder. 'when' accepts an ISO timestamp, 'in 30 minutes', "
            "'tomorrow at 9am', 'friday 17:00', or a bare clock time. Set 'repeat' for a "
            "recurring one. The reminder fires wherever Jarvis is running."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "What to say when it fires."},
                "when": {"type": "string", "description": "When it should fire."},
                "repeat": {
                    "type": "string",
                    "description": (
                        "hourly, daily, weekdays, weekly, monthly, or 'every 30 minutes'."
                    ),
                },
            },
            "required": ["text", "when"],
        },
        handler=set_reminder,
    ),
    Tool(
        name="list_reminders",
        description="List reminders. Status is pending (default), done, cancelled, or all.",
        input_schema={
            "type": "object",
            "properties": {
                "status": {"enum": ["pending", "done", "cancelled", "all"], "type": "string"}
            },
        },
        handler=list_reminders,
    ),
    Tool(
        name="cancel_reminder",
        description="Cancel a pending reminder by its id.",
        input_schema={
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
        handler=cancel_reminder,
    ),
]
