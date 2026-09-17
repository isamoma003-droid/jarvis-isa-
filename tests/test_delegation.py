"""Sub-agent delegation: scoping, depth limits, and background tasks."""

from __future__ import annotations

import pytest
from conftest import FakeClient, text_turn, tool_turn

from jarvis.agent import Agent
from jarvis.errors import ToolError
from jarvis.toolkit import build_registry
from jarvis.tools.subagents import PROFILES, check_task, delegate, list_tasks


def main_agent(client, config, context):
    return Agent(
        client=client,
        config=config,
        registry=build_registry(config),
        context=context,
        system=[{"type": "text", "text": "test"}],
    )


def test_delegation_returns_the_subagent_report(config, context):
    client = FakeClient([
        tool_turn([("delegate", {"task": "find the release date", "agent": "researcher"})]),
        text_turn("REPORT: shipped on Tuesday."),      # the sub-agent's turn
        text_turn("It shipped on Tuesday."),           # back in the main conversation
    ])
    context.client = client
    agent = main_agent(client, config, context)
    messages = [{"role": "user", "content": "when did it ship?"}]
    events = list(agent.run(messages))

    assert "REPORT: shipped on Tuesday." in messages[2]["content"][0]["content"]
    assert events[-1].text == "It shipped on Tuesday."


def test_a_subagent_gets_its_own_system_prompt_and_tools(config, context):
    client = FakeClient([
        tool_turn([("delegate", {"task": "look at the code", "agent": "analyst"})]),
        text_turn("read-only findings"),
        text_turn("done"),
    ])
    context.client = client
    list(main_agent(client, config, context).run([{"role": "user", "content": "go"}]))

    subagent_call = client.calls[1]
    tool_names = [tool["name"] for tool in subagent_call["tools"] if "name" in tool]
    assert "run_shell" not in tool_names       # the analyst is read-only
    assert "delegate" not in tool_names        # and cannot delegate onward
    assert "read_file" in tool_names
    assert subagent_call["output_config"] == {"effort": "low"}
    assert "analyst" in subagent_call["system"][0]["text"]


def test_the_coder_profile_has_no_web_access(config, context):
    client = FakeClient([
        tool_turn([("delegate", {"task": "fix the test", "agent": "coder"})]),
        text_turn("fixed"),
        text_turn("done"),
    ])
    context.client = client
    list(main_agent(client, config, context).run([{"role": "user", "content": "go"}]))

    types = [tool.get("type", "") for tool in client.calls[1]["tools"]]
    assert not any(t.startswith("web_") for t in types)
    assert "run_shell" in [t["name"] for t in client.calls[1]["tools"] if "name" in t]


def test_delegation_depth_is_capped(config, context):
    context.depth = config.max_depth
    with pytest.raises(ToolError, match="depth limit"):
        delegate(context, {"task": "recurse forever"})


def test_unknown_agent_is_rejected(config, context):
    with pytest.raises(ToolError, match="unknown agent"):
        delegate(context, {"task": "x", "agent": "wizard"})


def test_background_delegation_returns_a_task_id(config, context):
    client = FakeClient([text_turn("background report")])
    context.client = client

    answer = delegate(context, {"task": "slow research", "background": True, "agent": "researcher"})
    assert "task_" in answer

    task_id = answer.split("Started ")[1].split(" ")[0]
    assert context.tasks.wait(task_id, timeout=10).status == "done"

    report = check_task(context, {"id": task_id})
    assert "background report" in report
    assert task_id in list_tasks(context, {})


def test_checking_an_unknown_task_reports_back(config, context):
    with pytest.raises(ToolError, match="no task"):
        check_task(context, {"id": "task_nope"})


def test_every_profile_names_real_tools(config):
    everything = set(build_registry(config).names())
    for profile in PROFILES.values():
        unknown = set(profile.tools) - everything
        assert not unknown, f"{profile.name} lists tools that do not exist: {unknown}"
