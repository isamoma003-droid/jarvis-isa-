"""Delegation: Jarvis handing a scoped task to a sub-agent.

A sub-agent is a fresh conversation with its own system prompt and a narrowed
tool set. It reports back a single answer, so a long search or a noisy build
does not fill the main conversation with intermediate steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import ToolError
from ..events import Event, Notice, ToolStarted
from .base import Tool, ToolContext, truncate


@dataclass(frozen=True)
class Profile:
    """A named sub-agent shape: which tools it gets and how it is briefed."""

    name: str
    description: str
    tools: tuple[str, ...]
    web: bool
    brief: str


PROFILES: dict[str, Profile] = {
    "scout": Profile(
        name="scout",
        description=(
            "Research: looks things up, reads without changing anything, cites sources."
        ),
        tools=("read_file", "list_dir", "search_text", "find_files", "recall", "remember"),
        web=True,
        brief=(
            "You are Scout, the research sub-agent. Answer the task from primary sources - "
            "search and fetch the web, read what is already in the workspace - and finish "
            "with a compact report: the answer first, then the evidence and sources behind "
            "it. You have no write access, so analysis and citation are the deliverable. "
            "Say plainly what you could not confirm."
        ),
    ),
    "relay": Profile(
        name="relay",
        description="Comms: drafts messages, replies, and summaries for a human to send.",
        tools=("read_file", "write_file", "list_dir", "search_text", "recall", "remember"),
        web=True,
        brief=(
            "You are Relay, the communications sub-agent. Draft the message, reply, or "
            "summary the task asks for, in the register the recipient expects. You cannot "
            "send anything - save the draft to the workspace and report where it is, so a "
            "human sends it. Never invent a fact about the recipient or the history; if you "
            "need something you were not given, leave a clearly marked gap in the draft."
        ),
    ),
    "flux": Profile(
        name="flux",
        description="Tasks: changes files, runs commands, and reports what actually happened.",
        tools=(
            "read_file", "write_file", "edit_file", "list_dir",
            "find_files", "search_text", "run_shell",
        ),
        web=False,
        brief=(
            "You are Flux, the task sub-agent. Do the work the task asks for, run whatever "
            "check the project already uses (tests, linter, build), and report what you "
            "changed, what you ran, and the real result - including failures. Do not widen "
            "the task beyond what was asked."
        ),
    ),
    "general": Profile(
        name="general",
        description="A general assistant with the usual tools, minus delegation.",
        tools=(
            "read_file", "write_file", "edit_file", "list_dir", "find_files",
            "search_text", "run_shell", "remember", "recall", "set_reminder",
        ),
        web=True,
        brief=(
            "You are a general-purpose sub-agent of Jarvis. Complete the task and report "
            "the outcome."
        ),
    ),
}


def _run_profile(
    ctx: ToolContext,
    profile: Profile,
    task_text: str,
    notes: str,
    log: list[str] | None = None,
    unattended: bool = False,
) -> str:
    """Run one sub-agent turn to completion and return its final answer.

    A sub-agent reports once, by design, so its tool events do not join the
    parent stream. They go to `log` instead, which is what check_task shows.
    """
    from ..agent import Agent  # imported here: agent.py builds tool registries
    from ..prompts import subagent_system
    from ..toolkit import build_registry

    registry = build_registry(ctx.config, only=profile.tools)

    def record(event: Event) -> None:
        if log is not None and isinstance(event, ToolStarted):
            log.append(event.name)

    child_ctx = ToolContext(
        config=ctx.config,
        store=ctx.store,
        client=ctx.client,
        tasks=ctx.tasks,
        emit=record,
        confirm=ctx.confirm,
        depth=ctx.depth + 1,
        audit=ctx.audit,
        # A background sub-agent runs with nobody watching, so it must never
        # block on an approval no one is there to give. The gate refuses and
        # leaves a note instead.
        unattended=unattended or ctx.unattended,
    )
    agent = Agent(
        client=ctx.client,
        config=ctx.config,
        registry=registry,
        context=child_ctx,
        system=subagent_system(profile.brief, ctx.config, profile.name),
        model=ctx.config.worker_model,
        effort=ctx.config.subagent_effort,
        name=profile.name,
        server_tools_enabled=profile.web and ctx.config.web_tools,
    )
    prompt = task_text
    if notes:
        prompt = f"{task_text}\n\nContext from the main conversation:\n{notes}"
    return agent.run_to_text(prompt)


def delegate(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.depth >= ctx.config.max_depth:
        raise ToolError(
            f"delegation depth limit reached ({ctx.config.max_depth}); do this one yourself"
        )
    profile_name = args.get("agent", "general")
    profile = PROFILES.get(profile_name)
    if profile is None:
        raise ToolError(f"unknown agent {profile_name!r}; choose from {sorted(PROFILES)}")

    task_text = args["task"]
    notes = args.get("context", "")
    background = bool(args.get("background"))

    if not background:
        steps: list[str] = []
        result = _run_profile(ctx, profile, task_text, notes, log=steps)
        if steps:
            ctx.post(Notice(message=f"{profile.name} used: {', '.join(steps)}", level="debug"))
        return truncate(result, ctx.config.max_tool_output, "sub-agent report")

    if ctx.tasks is None:
        raise ToolError("background delegation is not available in this interface")

    def work(task_record: Any) -> str:
        return _run_profile(
            ctx, profile, task_text, notes, log=task_record.log, unattended=True
        )

    task = ctx.tasks.submit(description=task_text, profile=profile.name, work=work)
    ctx.post(Notice(message=f"delegated to {profile.name} in the background as {task.id}"))
    return (
        f"Started {task.id} ({profile.name}) in the background: {task_text}\n"
        "Keep going with other work and use check_task when you need the result."
    )


def check_task(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.tasks is None:
        raise ToolError("no background tasks in this interface")
    task_id = args["id"]
    wait_seconds = float(args.get("wait_seconds") or 0)
    task = ctx.tasks.wait(task_id, timeout=wait_seconds) if wait_seconds else ctx.tasks.get(task_id)
    if task is None:
        raise ToolError(f"no task {task_id!r}")
    return truncate(task.summary(), ctx.config.max_tool_output, "task report")


def list_tasks(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.tasks is None:
        raise ToolError("no background tasks in this interface")
    tasks = ctx.tasks.all()
    if not tasks:
        return "No delegated tasks yet."
    return "\n".join(
        f"[{t.id}] {t.status} ({t.profile}, {t.duration}): {t.description}" for t in tasks
    )


_AGENT_LIST = "\n".join(f"- {p.name}: {p.description}" for p in PROFILES.values())

TOOLS = [
    Tool(
        name="delegate",
        description=(
            "Hand a self-contained task to a sub-agent with its own context window, and get "
            "back a single report. Use it when a task would otherwise flood this conversation "
            "with intermediate steps - broad research, a build-and-fix loop, reading a large "
            "tree. Set background to keep working while it runs, then use check_task.\n\n"
            "Available agents:\n" + _AGENT_LIST + "\n\n"
            "Write the task as a complete brief: the sub-agent cannot see this conversation "
            "except for what you put in 'context'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The full brief, including what 'done' means.",
                },
                "agent": {
                    "type": "string",
                    "enum": sorted(PROFILES),
                    "description": "Which sub-agent to use.",
                },
                "context": {
                    "type": "string",
                    "description": "Facts from this conversation the sub-agent needs.",
                },
                "background": {
                    "type": "boolean",
                    "description": "Run without blocking and return a task id.",
                },
            },
            "required": ["task"],
        },
        handler=delegate,
    ),
    Tool(
        name="check_task",
        description=(
            "Check a background task. Returns the report if it finished, progress if not. "
            "Set wait_seconds to block briefly for a task that is nearly done."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "wait_seconds": {"type": "number", "description": "Block up to this long."},
            },
            "required": ["id"],
        },
        handler=check_task,
    ),
    Tool(
        name="list_tasks",
        description="List delegated background tasks and their status.",
        input_schema={"type": "object", "properties": {}},
        handler=list_tasks,
    ),
]
