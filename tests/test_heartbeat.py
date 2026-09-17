"""Tier 5: the heartbeat, and the rules that keep it from becoming noise.

The hard parts of a proactive assistant are not "can it run a check on a timer".
They are: does it stay quiet when it should, does it hold what you were not there
to see, and does restarting it reset every timer. Those are what this file tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from jarvis.events import NoticeSurfaced, ReminderFired
from jarvis.heartbeat import (
    Check,
    CheckError,
    Heartbeat,
    in_quiet_hours,
    load_checks,
    parse_every,
    parse_quiet_hours,
    should_interrupt,
)
from jarvis.memory import utcnow


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 17, hour, minute, tzinfo=UTC)


def beat(config, store, checks=None, **overrides):
    config.checks = checks or []
    for key, value in overrides.items():
        setattr(config, key, value)
    collected: list = []
    heartbeat = Heartbeat(config, store, collected.append)
    return heartbeat, collected


# --- parsing ---------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "seconds"),
    [("30s", 30), ("5m", 300), ("2h", 7200), ("1d", 86400), ("90", 90), ("", 3600)],
)
def test_interval_parsing(text, seconds):
    assert parse_every(text) == seconds


def test_a_nonsense_interval_is_rejected_loudly():
    with pytest.raises(CheckError, match="interval"):
        parse_every("whenever")


def test_quiet_hours_parsing():
    assert parse_quiet_hours("22:00-07:00") == (time(22, 0), time(7, 0))
    assert parse_quiet_hours("") is None
    with pytest.raises(CheckError):
        parse_quiet_hours("late at night")
    with pytest.raises(CheckError):
        parse_quiet_hours("25:00-07:00")


def test_quiet_hours_wrap_over_midnight():
    window = parse_quiet_hours("22:00-07:00")
    assert in_quiet_hours(_at(23), window)
    assert in_quiet_hours(_at(3), window)
    assert in_quiet_hours(_at(22), window)      # inclusive at the start
    assert not in_quiet_hours(_at(7), window)   # exclusive at the end
    assert not in_quiet_hours(_at(12), window)


def test_only_urgent_gets_through_quiet_hours():
    window = parse_quiet_hours("22:00-07:00")
    assert not should_interrupt("quiet", _at(12), window)      # never interrupts
    assert should_interrupt("notify", _at(12), window)
    assert not should_interrupt("notify", _at(23), window)     # held until morning
    assert should_interrupt("urgent", _at(23), window)         # earns the exception
    assert not should_interrupt("urgent", _at(23), window, paused=True)


# --- check definitions -----------------------------------------------
def test_a_check_definition_is_validated_at_load():
    with pytest.raises(CheckError, match="needs a command"):
        load_checks([{"name": "empty", "kind": "shell"}])
    with pytest.raises(CheckError, match="needs a match pattern"):
        load_checks([{"name": "x", "command": "true", "surface": "on_match"}])
    with pytest.raises(CheckError, match="level must be"):
        load_checks([{"name": "x", "command": "true", "level": "shouty"}])
    with pytest.raises(CheckError, match="unknown check settings"):
        load_checks([{"name": "x", "command": "true", "colour": "blue"}])
    with pytest.raises(CheckError, match="both called"):
        load_checks([{"name": "x", "command": "true"}, {"name": "x", "command": "true"}])


def test_a_bad_regex_is_caught_before_it_ever_runs():
    with pytest.raises(CheckError, match="bad match pattern"):
        Check(name="x", command="true", surface="on_match", match="[unclosed")


# --- running ---------------------------------------------------------
def test_a_passing_check_surfaces_nothing(config, store, workspace):
    heartbeat, collected = beat(
        config, store, [{"name": "ok", "command": "true", "every": "1s"}]
    )
    # Due now rather than one interval out, so the beat actually runs it.
    store.schedule_check("ok", utcnow() - timedelta(seconds=1))

    assert heartbeat.tick() == []
    assert collected == []
    assert store.open_notices() == []


def test_a_failing_check_surfaces_and_is_held_when_quiet(config, store, workspace):
    heartbeat, collected = beat(
        config,
        store,
        [{"name": "disk", "command": "exit 1", "every": "1h", "level": "notify"}],
        quiet_hours="00:00-23:59",  # the whole day is quiet, so nothing interrupts
    )
    store.schedule_check("disk", utcnow() - timedelta(seconds=1))

    raised = heartbeat.tick()
    assert [notice.text for notice in raised] == ["disk: no output"]
    # Recorded, but nobody was interrupted - it is waiting instead.
    assert collected == []
    assert [n.status for n in store.open_notices()] == ["pending"]


def test_a_notify_check_interrupts_outside_quiet_hours(config, store, workspace):
    heartbeat, collected = beat(
        config,
        store,
        [{
            "name": "build",
            "command": "echo 'build is broken'; exit 1",
            "every": "1h",
            "level": "notify",
        }],
        quiet_hours="",  # no quiet window at all
    )
    store.schedule_check("build", utcnow() - timedelta(seconds=1))

    heartbeat.tick()
    assert len(collected) == 1
    assert isinstance(collected[0], NoticeSurfaced)
    assert "build is broken" in collected[0].text
    assert [n.status for n in store.open_notices()] == ["delivered"]


def test_on_match_surfaces_only_when_the_pattern_hits(config, store, workspace):
    spec = {
        "name": "space",
        "command": "echo '/dev/disk1 91% /'",
        "every": "1h",
        "surface": "on_match",
        "match": r"9[0-9]%",
        "message": "disk is nearly full",
    }
    heartbeat, _ = beat(config, store, [spec])
    store.schedule_check("space", utcnow() - timedelta(seconds=1))
    assert [n.text for n in heartbeat.tick()] == ["disk is nearly full"]

    # Same check, output that does not match: silence.
    quiet_spec = dict(spec, name="space2", command="echo '/dev/disk1 12% /'")
    heartbeat2, _ = beat(config, store, [quiet_spec])
    store.schedule_check("space2", utcnow() - timedelta(seconds=1))
    assert heartbeat2.tick() == []


def test_on_change_stays_quiet_on_the_first_run(config, store, workspace):
    marker = workspace / "watched.txt"
    marker.write_text("one")
    spec = {"name": "watch", "kind": "file", "path": "watched.txt", "surface": "on_change"}
    heartbeat, _ = beat(config, store, [spec])

    store.schedule_check("watch", utcnow() - timedelta(seconds=1))
    assert heartbeat.tick() == []  # the first run only establishes the baseline

    marker.write_text("two, definitely different")
    store.schedule_check("watch", utcnow() - timedelta(seconds=1))
    store.checks.update_one({"_id": "watch"}, {"$set": {"next_due": utcnow() - timedelta(1)}})
    assert [n.text for n in heartbeat.tick()] == ["watch: " + str(marker) + " changed"]


def test_a_check_that_explodes_reports_itself_and_the_loop_survives(
    config, store, workspace, monkeypatch
):
    heartbeat, _ = beat(
        config, store, [{"name": "boom", "command": "true", "every": "1h", "timeout": 1}]
    )
    store.schedule_check("boom", utcnow() - timedelta(seconds=1))

    def explode(check, workspace, previous):
        raise RuntimeError("the check itself is broken")

    monkeypatch.setattr("jarvis.heartbeat.RUNNERS", {"shell": explode})
    raised = heartbeat.tick()

    assert len(raised) == 1
    assert "raised RuntimeError" in raised[0].text
    # and the claim was released, so a crashed run does not wedge the check
    assert store.check_state("boom").get("running_since") is None


# --- not crying wolf --------------------------------------------------
def test_a_condition_that_stays_true_does_not_keep_interrupting(config, store, workspace):
    """The failure that gets a proactive assistant muted.

    Found by running it: a check on a short interval re-announced the same
    still-true condition on every single beat.
    """
    spec = [{
        "name": "disk",
        "command": "echo '91% full'",
        "every": "1s",
        "surface": "on_match",
        "match": r"9[0-9]%",
        "level": "notify",
        "message": "disk is nearly full",
    }]
    heartbeat, collected = beat(config, store, spec, quiet_hours="")

    for _ in range(4):
        store.checks.update_one({"_id": "disk"}, {"$set": {"next_due": utcnow()}})
        store.schedule_check("disk", utcnow())
        heartbeat.run_check(heartbeat.checks[0])

    assert len(collected) == 1, "it interrupted more than once about the same thing"
    notices = store.open_notices()
    assert len(notices) == 1
    assert notices[0].repeats == 3  # counted, not repeated at the user


def test_dismissing_makes_the_next_occurrence_news_again(config, store, workspace):
    spec = [{
        "name": "disk",
        "command": "echo '91% full'",
        "every": "1s",
        "surface": "on_match",
        "match": r"9[0-9]%",
        "level": "notify",
    }]
    heartbeat, collected = beat(config, store, spec, quiet_hours="")
    store.schedule_check("disk", utcnow() - timedelta(seconds=1))
    heartbeat.run_check(heartbeat.checks[0])
    assert len(collected) == 1

    store.dismiss_all_notices()  # dealt with it
    store.checks.update_one({"_id": "disk"}, {"$set": {"next_due": utcnow()}})
    heartbeat.run_check(heartbeat.checks[0])

    assert len(collected) == 2, "after dismissing, it should speak up again"


def test_a_recurring_reminder_still_fires_every_time(config, store, workspace):
    """Reminders are exempt from de-duplication - repeating is their whole job."""
    heartbeat, collected = beat(config, store, quiet_hours="")
    store.add_reminder("stand up", utcnow() - timedelta(seconds=1), recurrence="every:1minutes")

    heartbeat.tick()
    store.reminders.update_one({"_id": 1}, {"$set": {"due_at": utcnow()}})
    heartbeat.tick()

    assert [e.text for e in collected if isinstance(e, ReminderFired)] == ["stand up", "stand up"]


# --- delivery vs. being raised ----------------------------------------
def test_a_notice_raised_with_nobody_attached_is_held(config, store, workspace):
    """The other bug running it found.

    Delivery used to be decided by policy alone: an `urgent` notice was marked
    delivered even when no interface existed to receive it, so what you missed
    while away was silently lost.
    """
    config.checks = [{
        "name": "deploy", "kind": "file", "path": "FLAG",
        "surface": "on_exists", "level": "urgent", "every": "1s",
    }]
    (workspace / "FLAG").write_text("go")
    headless = Heartbeat(config, store)  # no listener at all
    store.schedule_check("deploy", utcnow() - timedelta(seconds=1))

    headless.tick()
    held = store.pending_notices()
    assert [n.text for n in held] == ["deploy: " + str(workspace / "FLAG") + " exists"]
    assert held[0].status == "pending", "marked delivered with nobody there to receive it"


def test_detaching_holds_everything_raised_from_then_on(config, store, workspace):
    heartbeat, collected = beat(
        config,
        store,
        [{"name": "x", "command": "exit 1", "every": "1s", "level": "urgent"}],
        quiet_hours="",
    )
    store.schedule_check("x", utcnow() - timedelta(seconds=1))
    heartbeat.run_check(heartbeat.checks[0])
    assert len(collected) == 1

    heartbeat.detach()  # the terminal exited, the browser tab closed
    store.dismiss_all_notices()
    store.checks.update_one({"_id": "x"}, {"$set": {"next_due": utcnow()}})
    heartbeat.run_check(heartbeat.checks[0])

    assert len(collected) == 1, "delivered to a listener that is gone"
    assert [n.status for n in store.pending_notices()] == ["pending"]


# --- the schedule ----------------------------------------------------
def test_priming_does_not_fire_everything_on_boot(config, store, workspace):
    heartbeat, collected = beat(
        config, store, [{"name": "hourly", "command": "exit 1", "every": "1h"}]
    )
    heartbeat.prime()
    assert heartbeat.tick() == []  # not due yet - a restart is not an excuse to fire

    state = store.check_state("hourly")
    assert state["next_due"] > utcnow()


def test_a_restart_resumes_the_schedule_rather_than_resetting_it(config, store, workspace):
    spec = [{"name": "keep", "command": "true", "every": "1h"}]
    first, _ = beat(config, store, spec)
    first.prime()
    due = store.check_state("keep")["next_due"]

    # A fresh process, same database.
    second, _ = beat(config, store, spec)
    second.prime()
    assert store.check_state("keep")["next_due"] == due


def test_a_check_dropped_from_the_config_loses_its_schedule(config, store, workspace):
    first, _ = beat(config, store, [{"name": "gone", "command": "true"}])
    first.prime()
    assert store.check_state("gone")

    second, _ = beat(config, store, [{"name": "kept", "command": "true"}])
    second.prime()
    assert store.check_state("gone") == {}


def test_a_running_check_is_skipped_rather_than_stacked(config, store, workspace):
    heartbeat, _ = beat(
        config, store, [{"name": "slow", "command": "exit 3", "every": "1h", "level": "urgent"}]
    )
    store.schedule_check("slow", utcnow() - timedelta(seconds=1))
    check = heartbeat.checks[0]

    # Claimed a moment ago by a run still in flight: the new turn is skipped,
    # not queued behind it.
    store.checks.update_one({"_id": "slow"}, {"$set": {"running_since": utcnow()}})
    assert heartbeat.run_check(check) is None
    assert store.check_state("slow").get("last_status", "") == ""  # never ran

    # A claim older than the lease is a run that died. That one is retried.
    store.checks.update_one(
        {"_id": "slow"}, {"$set": {"running_since": utcnow() - timedelta(hours=1)}}
    )
    notice = heartbeat.run_check(check)
    assert notice is not None
    assert store.check_state("slow")["last_status"] == "fail"


# --- reminders, held ---------------------------------------------------
def test_a_reminder_that_fires_with_nobody_there_is_held(config, store, workspace):
    heartbeat, collected = beat(config, store, quiet_hours="00:00-23:59")
    store.add_reminder("stand up", utcnow() - timedelta(seconds=1))

    heartbeat.tick()
    assert collected == []                       # quiet hours: not delivered now
    held = store.pending_notices()
    assert [n.text for n in held] == ["stand up"]  # but definitely not lost


def test_a_reminder_outside_quiet_hours_reaches_the_interface(config, store, workspace):
    heartbeat, collected = beat(config, store, quiet_hours="")
    store.add_reminder("stand up", utcnow() - timedelta(seconds=1))

    heartbeat.tick()
    assert [e.text for e in collected if isinstance(e, ReminderFired)] == ["stand up"]


# --- the kill switch ---------------------------------------------------
def test_pausing_stops_every_beat_without_tearing_anything_down(config, store, workspace):
    heartbeat, collected = beat(
        config,
        store,
        [{"name": "noisy", "command": "exit 1", "every": "1h", "level": "urgent"}],
        quiet_hours="",
    )
    store.schedule_check("noisy", utcnow() - timedelta(seconds=1))
    store.set_paused(True)

    assert heartbeat.tick() == []
    assert collected == []
    assert store.open_notices() == []

    store.set_paused(False)
    assert len(heartbeat.tick()) == 1
