"""Shell tool.

The workspace is the working directory and the approval gate is the real
control. The refusal list below catches obvious catastrophes; treat it as a
guardrail against a slip, not as a security boundary - a shell cannot be
sandboxed by pattern matching.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any

from ..errors import ToolError
from ..guard import outward_reason
from .base import Tool, ToolContext, truncate

REFUSED = [
    (
        re.compile(r"\brm\s+(-[a-z]*\s+)*-[a-z]*[rf][a-z]*\s+(/|~|\$HOME)\s*$"),
        "recursive delete of a home or root path",
    ),
    (re.compile(r"\bmkfs(\.|\s)"), "filesystem format"),
    (re.compile(r"\bdd\b.*\bof=/dev/"), "raw write to a device"),
    (re.compile(r":\(\)\s*\{.*\};\s*:"), "fork bomb"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"), "machine power control"),
    (re.compile(r">\s*/dev/(sd|nvme|disk)"), "raw write to a disk device"),
    (re.compile(r"\bchmod\s+-R\s+777\s+/\s*$"), "world-writable root"),
]

# Commands that only look at things - no approval prompt for these.
READ_ONLY = {"ls", "cat", "head", "tail", "pwd", "whoami", "date", "df", "du", "wc",
             "grep", "rg", "find", "which", "echo", "env", "uname", "ps", "stat",
             "git status", "git log", "git diff", "git show", "git branch"}


def _is_read_only(command: str) -> bool:
    stripped = command.strip()
    if any(char in stripped for char in ">|&;$`"):
        return False
    return any(
        stripped == prefix or stripped.startswith(prefix + " ")
        for prefix in READ_ONLY
    )


def run_shell(ctx: ToolContext, args: dict[str, Any]) -> str:
    command = args["command"].strip()
    if not command:
        raise ToolError("empty command")
    for pattern, why in REFUSED:
        if pattern.search(command):
            raise ToolError(f"refused: this looks like {why}. Ask the user to run it themselves.")

    timeout = int(args.get("timeout") or ctx.config.shell_timeout)
    timeout = max(1, min(timeout, 600))

    # Checked before the read-only shortcut and before the ordinary gate: a
    # command that leaves the machine is never waived by the approval policy,
    # and one previous yes does not cover the next send.
    reaching_out = outward_reason(command)
    if reaching_out:
        ctx.approve(reaching_out, command, outward=True)
    elif not _is_read_only(command):
        ctx.approve("run shell command", command)

    try:
        completed = subprocess.run(  # noqa: S602 - a shell is the point of this tool
            ["bash", "-lc", command],
            cwd=str(ctx.workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"command timed out after {timeout}s: {command}") from None
    except FileNotFoundError as exc:  # no bash on this machine
        raise ToolError(f"cannot run shell commands: {exc}") from exc

    parts = []
    if completed.stdout.strip():
        parts.append(completed.stdout.rstrip())
    if completed.stderr.strip():
        parts.append(f"[stderr]\n{completed.stderr.rstrip()}")
    if completed.returncode != 0:
        parts.append(f"[exit code {completed.returncode}]")
    body = "\n".join(parts) or "(no output)"
    return truncate(body, ctx.config.max_tool_output, "command output")


TOOLS = [
    Tool(
        name="run_shell",
        description=(
            "Run a bash command in the workspace directory and return its output. "
            "Use this for git, builds, tests, and anything the file tools do not cover. "
            "Commands that change state need the user's approval first, and anything "
            "that reaches the network or another machine - pushing, sending, uploading - "
            "is confirmed every time, however the approval policy is set."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The bash command line to run."},
                "timeout": {"type": "integer", "description": "Seconds before giving up."},
            },
            "required": ["command"],
        },
        handler=run_shell,
        dangerous=True,
        untrusted=True,
    ),
]
