"""File, shell, memory, and reminder tools - including the sandbox."""

from __future__ import annotations

import pytest

from jarvis.errors import ApprovalDenied, SandboxViolation, ToolError
from jarvis.tools import files, memory_tools, reminder_tools, shell


# --- sandbox ---------------------------------------------------------
def test_paths_stay_inside_the_workspace(context, workspace):
    (workspace / "inside.txt").write_text("fine")
    assert context.resolve("inside.txt").name == "inside.txt"

    for escape in ("../outside.txt", "/etc/passwd", "../../root/.ssh/id_rsa"):
        with pytest.raises(SandboxViolation):
            context.resolve(escape)


def test_symlink_out_of_the_workspace_is_refused(context, workspace, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("classified")
    (workspace / "link.txt").symlink_to(secret)

    with pytest.raises(SandboxViolation):
        context.resolve("link.txt")


def test_reading_a_file_outside_the_workspace_fails(context, workspace):
    with pytest.raises(SandboxViolation):
        files.read_file(context, {"path": "/etc/hostname"})


# --- files -----------------------------------------------------------
def test_read_write_edit_cycle(context, workspace):
    files.write_file(context, {"path": "notes/todo.md", "content": "one\ntwo\n"})
    assert (workspace / "notes" / "todo.md").read_text() == "one\ntwo\n"

    shown = files.read_file(context, {"path": "notes/todo.md"})
    assert "1\tone" in shown and "2 lines" in shown

    files.edit_file(context, {"path": "notes/todo.md", "find": "two", "replace": "three"})
    assert "three" in (workspace / "notes" / "todo.md").read_text()


def test_edit_refuses_an_ambiguous_match(context, workspace):
    (workspace / "dup.txt").write_text("x\nx\n")
    with pytest.raises(ValueError, match="appears 2 times"):
        files.edit_file(context, {"path": "dup.txt", "find": "x", "replace": "y"})

    files.edit_file(context, {"path": "dup.txt", "find": "x", "replace": "y", "replace_all": True})
    assert (workspace / "dup.txt").read_text() == "y\ny\n"


def test_edit_reports_missing_text(context, workspace):
    (workspace / "a.txt").write_text("hello")
    with pytest.raises(ValueError, match="does not appear"):
        files.edit_file(context, {"path": "a.txt", "find": "goodbye", "replace": "x"})


def test_read_file_paging(context, workspace):
    (workspace / "long.txt").write_text("\n".join(f"line {i}" for i in range(1, 101)))
    shown = files.read_file(context, {"path": "long.txt", "start_line": 50, "max_lines": 3})
    assert "line 50" in shown and "line 52" in shown and "line 53" not in shown


def test_find_and_search(context, workspace):
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("def handler():\n    return TODO\n")
    (workspace / "src" / "util.py").write_text("x = 1\n")
    (workspace / "src" / "__pycache__").mkdir()
    (workspace / "src" / "__pycache__" / "app.pyc").write_text("junk")

    found = files.find_files(context, {"pattern": "*.py"})
    assert "app.py" in found and "util.py" in found
    assert ".pyc" not in found  # noise directories are skipped

    hits = files.search_text(context, {"pattern": r"TODO", "glob": "*.py"})
    assert "app.py:2" in hits
    assert "No matches" in files.search_text(context, {"pattern": "nothing_here"})


def test_search_text_rejects_a_bad_regex(context, workspace):
    with pytest.raises(ValueError, match="invalid regular expression"):
        files.search_text(context, {"pattern": "([unclosed"})


def test_list_dir(context, workspace):
    (workspace / "a").mkdir()
    (workspace / "b.txt").write_text("hi")
    listing = files.list_dir(context, {})
    assert "a/" in listing and "b.txt" in listing


# --- approval --------------------------------------------------------
def test_writes_ask_before_acting(context, workspace):
    asked = []
    context.config.approval = "prompt"
    context.confirm = lambda action, detail: asked.append((action, detail)) or True

    files.write_file(context, {"path": "new.txt", "content": "hi"})
    assert asked and asked[0][0] == "create file"


def test_a_declined_write_does_not_happen(context, workspace):
    context.config.approval = "prompt"
    context.confirm = lambda action, detail: False

    with pytest.raises(ApprovalDenied):
        files.write_file(context, {"path": "new.txt", "content": "hi"})
    assert not (workspace / "new.txt").exists()


def test_deny_policy_blocks_without_asking(context, workspace):
    context.config.approval = "deny"
    context.confirm = lambda action, detail: True

    with pytest.raises(ApprovalDenied):
        files.write_file(context, {"path": "new.txt", "content": "hi"})


def test_prompt_policy_without_a_way_to_ask_refuses(context, workspace):
    context.config.approval = "prompt"
    context.confirm = None

    with pytest.raises(ApprovalDenied, match="cannot ask"):
        files.write_file(context, {"path": "new.txt", "content": "hi"})


# --- shell -----------------------------------------------------------
def test_shell_runs_in_the_workspace(context, workspace):
    (workspace / "marker.txt").write_text("x")
    assert "marker.txt" in shell.run_shell(context, {"command": "ls"})


def test_shell_reports_failure_output_and_exit_code(context, workspace):
    result = shell.run_shell(context, {"command": "echo oops >&2; exit 3"})
    assert "oops" in result and "exit code 3" in result


def test_shell_refuses_obvious_catastrophes(context, workspace):
    for command in ("rm -rf /", "mkfs.ext4 /dev/sda1", "shutdown -h now", ":(){ :|:& };:"):
        with pytest.raises(ToolError, match="refused"):
            shell.run_shell(context, {"command": command})


def test_read_only_commands_skip_the_prompt(context, workspace):
    context.config.approval = "prompt"
    context.confirm = None  # asking is impossible, so a prompt would fail

    assert shell.run_shell(context, {"command": "pwd"})
    with pytest.raises(ApprovalDenied):
        shell.run_shell(context, {"command": "touch new.txt"})


def test_read_only_detection_is_not_fooled_by_chaining(context, workspace):
    context.config.approval = "prompt"
    context.confirm = None
    with pytest.raises(ApprovalDenied):
        shell.run_shell(context, {"command": "ls && rm -f important.txt"})


def test_shell_timeout(context, workspace):
    with pytest.raises(ToolError, match="timed out"):
        shell.run_shell(context, {"command": "sleep 5", "timeout": 1})


# --- memory ----------------------------------------------------------
def test_memory_tools(context):
    memory_tools.remember(context, {"key": "Timezone", "value": "CET", "category": "identity"})
    assert "CET" in memory_tools.recall(context, {"query": "timezone"})
    assert "CET" in memory_tools.recall(context, {})

    memory_tools.remember(context, {"key": "timezone", "value": "GMT"})
    assert "GMT" in memory_tools.recall(context, {"query": "timezone"})
    assert "CET" not in memory_tools.recall(context, {"query": "timezone"})

    assert "Forgot" in memory_tools.forget(context, {"key": "timezone"})
    assert "Nothing" in memory_tools.forget(context, {"key": "timezone"})


# --- reminders -------------------------------------------------------
def test_reminder_tools(context):
    created = reminder_tools.set_reminder(context, {"text": "standup", "when": "in 10 minutes"})
    assert "#1" in created and "standup" in created

    listed = reminder_tools.list_reminders(context, {})
    assert "standup" in listed

    assert "Cancelled" in reminder_tools.cancel_reminder(context, {"id": 1})
    assert "No pending reminders" in reminder_tools.list_reminders(context, {})


def test_reminder_rejects_unreadable_time(context):
    with pytest.raises(ToolError, match="could not read"):
        reminder_tools.set_reminder(context, {"text": "x", "when": "at some point"})


def test_recurring_reminder(context):
    created = reminder_tools.set_reminder(
        context, {"text": "standup", "when": "tomorrow at 9am", "repeat": "weekdays"}
    )
    assert "repeating weekdays" in created
