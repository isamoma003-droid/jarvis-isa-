"""One scripted conversation exercising the whole stack through a Session."""

from __future__ import annotations

from conftest import FakeClient, text_turn, tool_turn

from jarvis.events import ReminderFired, ToolFinished, TurnFinished
from jarvis.memory import utcnow
from jarvis.session import Session


def test_a_full_conversation(config, workspace, store):
    """Write a file, remember a fact, set a reminder, delegate, then answer."""
    client = FakeClient([
        tool_turn(
            [
                ("write_file", {"path": "plan.md", "content": "# Plan\n- ship it\n"}),
                ("remember", {"key": "project", "value": "jarvis", "category": "work"}),
            ],
            text="On it.",
        ),
        tool_turn([("set_reminder", {"text": "ship it", "when": "in 5 minutes"})]),
        tool_turn([("delegate", {"task": "check the plan reads well", "agent": "scout"})]),
        text_turn("Reads fine."),                      # the sub-agent's report
        text_turn("Plan written, reminder set, and it reads fine."),
    ])

    with Session(config, interface="cli", client=client, store=store) as session:
        events = list(session.send("write a plan, remember the project, remind me, check it"))

    # the tools really ran
    assert (workspace / "plan.md").read_text() == "# Plan\n- ship it\n"
    assert [f.value for f in store.recall("project")] == ["jarvis"]
    assert store.list_reminders("pending")[0].text == "ship it"

    # the stream is complete and in order
    tool_names = [e.name for e in events if isinstance(e, ToolFinished)]
    assert tool_names == ["write_file", "remember", "set_reminder", "delegate"]
    assert not any(e.is_error for e in events if isinstance(e, ToolFinished))

    finished = events[-1]
    assert isinstance(finished, TurnFinished)
    assert finished.text == "Plan written, reminder set, and it reads fine."
    assert finished.input_tokens > 0

    # and the transcript was persisted
    transcript = store.session_messages(session.session_id)
    assert transcript[0]["content"] == "write a plan, remember the project, remind me, check it"
    assert len(transcript) > 3


def test_the_conversation_survives_a_failing_tool(config, workspace, store):
    client = FakeClient([
        tool_turn([("read_file", {"path": "../../etc/passwd"})]),   # refused by the sandbox
        tool_turn([("read_file", {"path": "real.txt"})]),
        text_turn("It says hello."),
    ])
    (workspace / "real.txt").write_text("hello")

    with Session(config, interface="cli", client=client, store=store) as session:
        events = list(session.send("read something"))

    failures = [e for e in events if isinstance(e, ToolFinished) and e.is_error]
    assert len(failures) == 1
    assert "outside the workspace" in failures[0].result
    assert events[-1].text == "It says hello."


def test_a_reminder_reaches_the_interface(config, store):
    client = FakeClient([])
    delivered = []
    # Pinned, not left to the wall clock: the default quiet window is
    # 22:00-07:00, so this passed by day and failed at night until it was fixed.
    config.quiet_hours = ""
    store.add_reminder("drink water", utcnow())

    with Session(config, interface="cli", client=client, store=store) as session:
        heartbeat = session.start_heartbeat(delivered.append)
        # stop the polling thread, then beat once deterministically
        heartbeat.stop()
        heartbeat.tick()

    assert [e.text for e in delivered if isinstance(e, ReminderFired)] == ["drink water"]


def test_a_reminder_in_the_small_hours_is_held_rather_than_shouted(config, store):
    """The other half of the same behaviour, and the reason the test above
    needed pinning: at 3am a reminder waits for you instead of waking you."""
    client = FakeClient([])
    delivered = []
    config.quiet_hours = "00:00-23:59"
    store.add_reminder("drink water", utcnow())

    with Session(config, interface="cli", client=client, store=store) as session:
        heartbeat = session.start_heartbeat(delivered.append)
        heartbeat.stop()
        heartbeat.tick()
        assert delivered == []                      # nobody was woken
        assert [e.text for e in session.catch_up()] == ["drink water"]  # nor lost


def test_reset_starts_a_new_conversation(config, store):
    client = FakeClient([text_turn("one"), text_turn("two")])
    with Session(config, interface="cli", client=client, store=store) as session:
        list(session.send("first"))
        first_id = session.session_id
        assert len(session.messages) > 0

        session.reset()
        assert session.messages == []
        assert session.session_id != first_id

        list(session.send("second"))
        assert len(store.session_messages(first_id)) == 2
        assert len(store.session_messages(session.session_id)) == 2
