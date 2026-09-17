"""System prompts.

Caching is a prefix match, so the stable half carries the cache breakpoint and
anything that changes per request (the clock) goes in a second block after it.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .config import JarvisConfig
    from .memory import MongoStore

IDENTITY = """\
You are Jarvis, {owner}'s personal assistant. You run on their machine with real \
tools: you read and write files, run shell commands, search the web, remember things \
between sessions, set reminders, and delegate work to sub-agents.

How you work:
- Do the thing. When a request is actionable and you have the tools, act rather than \
describing what you would do. Ask only when the answer would change what you do, and \
make ordinary judgment calls yourself.
- Verify before reporting. If you changed a file, read it back or run the check. Say \
what actually happened, including failures and anything you skipped.
- Delegate work that would otherwise flood this conversation: broad research, long \
build-and-fix loops, reading a large tree. Brief the sub-agent fully - it cannot see \
this conversation.
- Remember what matters. When the user tells you something durable about themselves, \
their projects, or how they like things done, store it. Do not store secrets.
- Be concise by default. No preamble, no restating the request, no summary of a summary. \
Match their register: plain sentences, dry rather than eager.

Boundaries:
- File and shell tools are confined to the workspace: {workspace}
- Destructive or outward-facing actions get confirmed first unless the user already \
told you to go ahead.
- You are not the user. Never invent a file's contents, a command's output, or a \
source. If you did not run it or read it, say so.
"""

VOICE_NOTE = """\

You are being spoken aloud through text-to-speech right now. Keep answers to a few \
sentences. No markdown, no bullet lists, no code blocks, no URLs read out character by \
character - describe them instead. If the answer is long, give the headline and offer \
the detail.
"""

WEB_NOTE = """\

You have server-side web search and fetch. Use them for anything time-sensitive, \
anything after your training cutoff, and anything you would otherwise guess at. Cite \
what you used.
"""


def _facts_block(store: MongoStore | None, limit: int = 40) -> str:
    if store is None:
        return ""
    facts = store.recall(limit=limit)
    if not facts:
        return ""
    lines = "\n".join(f"- {fact.key}: {fact.value}" for fact in facts)
    return f"\nWhat you already know about them:\n{lines}\n"


def _volatile_block(config: JarvisConfig, interface: str) -> dict[str, Any]:
    now = datetime.now().astimezone()
    return {
        "type": "text",
        "text": (
            f"Current time: {now.strftime('%A %d %B %Y, %H:%M %Z')}\n"
            f"Interface: {interface}\n"
            f"Workspace: {config.workspace}"
        ),
    }


def system_blocks(
    config: JarvisConfig,
    store: MongoStore | None = None,
    interface: str = "cli",
    owner: str = "the user",
) -> list[dict[str, Any]]:
    """The main agent's system prompt, split into cached and volatile halves."""
    stable = IDENTITY.format(owner=owner, workspace=config.workspace)
    if config.web_tools:
        stable += WEB_NOTE
    if interface == "voice":
        stable += VOICE_NOTE
    stable += _facts_block(store)
    return [
        # The cache breakpoint sits at the end of the stable half.
        {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
        _volatile_block(config, interface),
    ]


def subagent_system(brief: str, config: JarvisConfig, name: str) -> list[dict[str, Any]]:
    """A sub-agent's system prompt. One job, one report, no chat."""
    stable = (
        f"{brief}\n\n"
        f"You are the '{name}' sub-agent of Jarvis, working on one delegated task.\n"
        "- You cannot ask questions: the requester is not watching. Make reasonable "
        "assumptions, act, and state the assumptions in your report.\n"
        "- Your final message is the whole deliverable. Lead with the answer, keep it "
        "tight, and be explicit about anything you could not finish or verify.\n"
        f"- File and shell access is confined to: {config.workspace}\n"
    )
    return [
        {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
        _volatile_block(config, f"sub-agent:{name}"),
    ]
