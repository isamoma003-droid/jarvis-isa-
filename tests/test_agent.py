"""The agent loop: tool dispatch, and the API edge cases it has to survive."""

from __future__ import annotations

import pytest
from conftest import FakeClient, FakeMessage, TextBlock, text_turn, tool_turn

from jarvis.agent import Agent
from jarvis.events import ErrorEvent, Notice, TextDelta, ToolFinished, ToolStarted, TurnFinished
from jarvis.tools.base import Tool, ToolRegistry


@pytest.fixture
def echo_registry():
    calls = []

    def handler(ctx, args):
        calls.append(args)
        return f"echo: {args['value']}"

    registry = ToolRegistry([
        Tool(
            name="echo",
            description="Echo a value.",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}, "count": {"type": "integer"}},
                "required": ["value"],
            },
            handler=handler,
        )
    ])
    registry.calls = calls  # type: ignore[attr-defined]
    return registry


def make_agent(client, config, context, registry, **kwargs):
    config.web_tools = False
    return Agent(
        client=client,
        config=config,
        registry=registry,
        context=context,
        system=[{"type": "text", "text": "test"}],
        **kwargs,
    )


def run(agent, text="hi"):
    messages = [{"role": "user", "content": text}]
    return list(agent.run(messages)), messages


def test_plain_answer_streams_and_finishes(config, context, echo_registry):
    agent = make_agent(FakeClient([text_turn("Good evening.")]), config, context, echo_registry)
    events, messages = run(agent)

    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Good evening."]
    finished = events[-1]
    assert isinstance(finished, TurnFinished)
    assert finished.text == "Good evening."
    assert finished.stop_reason == "end_turn"
    assert messages[-1]["role"] == "assistant"


def test_tool_call_round_trip(config, context, echo_registry):
    client = FakeClient([
        tool_turn([("echo", {"value": "one"})], text="checking"),
        text_turn("Done."),
    ])
    agent = make_agent(client, config, context, echo_registry)
    events, messages = run(agent)

    assert echo_registry.calls == [{"value": "one"}]
    assert [type(e).__name__ for e in events if isinstance(e, (ToolStarted, ToolFinished))] == [
        "ToolStarted", "ToolFinished",
    ]
    assert events[-1].text == "Done."

    # assistant turn, then exactly one user message carrying the results
    assert messages[1]["role"] == "assistant"
    assert messages[2]["role"] == "user"
    results = messages[2]["content"]
    assert len(results) == 1
    assert results[0]["tool_use_id"] == "tu_1"
    assert results[0]["content"] == "echo: one"
    assert results[0]["is_error"] is False


def test_parallel_tool_calls_return_in_one_message(config, context, echo_registry):
    client = FakeClient([
        tool_turn([("echo", {"value": "a"}), ("echo", {"value": "b"})]),
        text_turn("Both done."),
    ])
    agent = make_agent(client, config, context, echo_registry)
    _, messages = run(agent)

    results = messages[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["tu_1", "tu_2"]
    assert len(echo_registry.calls) == 2


def test_invalid_tool_input_is_rejected_before_the_handler(config, context, echo_registry):
    client = FakeClient([
        tool_turn([("echo", {"value": 42})]),  # wrong type: the parser is tolerant, we are not
        text_turn("Recovered."),
    ])
    agent = make_agent(client, config, context, echo_registry)
    events, messages = run(agent)

    assert echo_registry.calls == []
    result = messages[2]["content"][0]
    assert result["is_error"] is True
    assert "INVALID_JSON" in result["content"]
    assert any(isinstance(e, ToolFinished) and e.is_error for e in events)


def test_missing_required_argument_is_rejected(config, context, echo_registry):
    client = FakeClient([tool_turn([("echo", {"count": 2})]), text_turn("ok")])
    agent = make_agent(client, config, context, echo_registry)
    _, messages = run(agent)

    assert echo_registry.calls == []
    assert "missing required argument 'value'" in messages[2]["content"][0]["content"]


def test_unknown_tool_reports_back_instead_of_crashing(config, context, echo_registry):
    client = FakeClient([tool_turn([("nope", {})]), text_turn("ok")])
    agent = make_agent(client, config, context, echo_registry)
    _, messages = run(agent)

    result = messages[2]["content"][0]
    assert result["is_error"] is True
    assert "unknown tool" in result["content"]


def test_failing_tool_is_reported_not_raised(config, context):
    def explode(ctx, args):
        raise RuntimeError("boom")

    registry = ToolRegistry([
        Tool(name="explode", description="", input_schema={"type": "object"}, handler=explode)
    ])
    client = FakeClient([tool_turn([("explode", {})]), text_turn("handled")])
    agent = make_agent(client, config, context, registry)
    events, messages = run(agent)

    assert "failed unexpectedly" in messages[2]["content"][0]["content"]
    assert events[-1].text == "handled"


def test_truncated_tool_input_is_never_executed(config, context, echo_registry):
    client = FakeClient([tool_turn([("echo", {"value": "half"})], stop_reason="max_tokens")])
    agent = make_agent(client, config, context, echo_registry)
    events, _ = run(agent)

    assert echo_registry.calls == []
    assert any(isinstance(e, ErrorEvent) and "cut off" in e.message for e in events)


def test_truncated_text_answer_is_kept(config, context, echo_registry):
    client = FakeClient([text_turn("A long answer that ran out of", stop_reason="max_tokens")])
    agent = make_agent(client, config, context, echo_registry)
    events, _ = run(agent)

    assert events[-1].text.startswith("A long answer")
    assert not any(isinstance(e, ErrorEvent) for e in events)


def test_pause_turn_is_resumed(config, context, echo_registry):
    paused = FakeMessage(content=[TextBlock(text="searching")], stop_reason="pause_turn")
    client = FakeClient([paused, text_turn("Found it.")])
    agent = make_agent(client, config, context, echo_registry)
    events, messages = run(agent)

    assert len(client.calls) == 2
    assert events[-1].text == "Found it."
    assert messages[1]["role"] == "assistant"


def test_refusal_stops_cleanly(config, context, echo_registry):
    from conftest import StopDetails

    refused = FakeMessage(
        content=[TextBlock(text="")], stop_reason="refusal", stop_details=StopDetails()
    )
    client = FakeClient([refused])
    agent = make_agent(client, config, context, echo_registry)
    events, _ = run(agent)

    assert any(isinstance(e, Notice) and "declined" in e.message for e in events)
    assert isinstance(events[-1], TurnFinished)


def test_unparseable_tool_json_retries_then_gives_up(config, context, echo_registry):
    client = FakeClient([ValueError("bad json"), text_turn("Recovered.")])
    agent = make_agent(client, config, context, echo_registry)
    events, _ = run(agent)
    assert events[-1].text == "Recovered."

    client = FakeClient([ValueError("bad")] * 3)
    agent = make_agent(client, config, context, echo_registry)
    events, _ = run(agent)
    assert any(isinstance(e, ErrorEvent) and "unparseable" in e.message for e in events)


def test_runaway_loop_is_capped(config, context, echo_registry):
    config.max_iterations = 3
    client = FakeClient([tool_turn([("echo", {"value": "x"})]) for _ in range(10)])
    agent = make_agent(client, config, context, echo_registry)
    events, _ = run(agent)

    assert len(client.calls) == 3
    assert any(isinstance(e, Notice) and "without finishing" in e.message for e in events)


def test_request_shape_is_cache_friendly(config, context, echo_registry):
    client = FakeClient([text_turn("hi")])
    agent = make_agent(client, config, context, echo_registry)
    run(agent)
    call = client.calls[0]

    assert call["model"] == "claude-opus-5"
    assert call["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert call["output_config"] == {"effort": "high"}
    assert call["tools"][0]["name"] == "echo"
    assert call["tools"][0]["eager_input_streaming"] is True
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["fallbacks"] == "default"


def test_fallbacks_can_be_turned_off(config, context, echo_registry):
    config.refusal_fallbacks = False
    client = FakeClient([text_turn("hi")])
    agent = make_agent(client, config, context, echo_registry)
    run(agent)

    assert "betas" not in client.calls[0]


def test_tools_are_sorted_for_a_stable_cache_prefix(config, context):
    def noop(ctx, args):
        return ""

    registry = ToolRegistry([
        Tool(name=name, description="", input_schema={"type": "object"}, handler=noop)
        for name in ("zulu", "alpha", "mike")
    ])
    client = FakeClient([text_turn("hi")])
    agent = make_agent(client, config, context, registry)
    run(agent)

    assert [t["name"] for t in client.calls[0]["tools"]] == ["alpha", "mike", "zulu"]


def test_run_to_text_returns_the_final_answer(config, context, echo_registry):
    client = FakeClient([
        tool_turn([("echo", {"value": "x"})], text="working"),
        text_turn("The answer."),
    ])
    agent = make_agent(client, config, context, echo_registry)

    assert agent.run_to_text("go") == "The answer."
