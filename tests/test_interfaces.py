"""The three faces: terminal helpers, the web socket, and voice text handling."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClient, text_turn, tool_turn

from jarvis import cli
from jarvis.events import TextDelta, ToolFinished, ToolStarted, TurnFinished
from jarvis.session import Session
from jarvis.voice.loop import strip_wake_word
from jarvis.voice.tts import PrintSpeaker, load_tts, speakable


# --- terminal --------------------------------------------------------
def test_tool_arguments_are_summarised_for_display():
    assert cli._describe({"path": "src/app.py"}) == "src/app.py"
    assert cli._describe({"command": "ls  -la\n"}) == "ls -la"
    assert cli._describe({}) == ""
    assert cli._describe({"unexpected": "x" * 200}).endswith("…")


def test_terminal_renders_the_whole_event_stream(capsys):
    terminal = cli.Terminal(session=None)  # rendering needs no session
    for event in [
        TextDelta(text="hello "),
        TextDelta(text="world"),
        ToolStarted(name="read_file", tool_use_id="t1", input={"path": "a.txt"}),
        ToolFinished(name="read_file", tool_use_id="t1", result="ok", duration_ms=12),
        ToolFinished(name="run_shell", tool_use_id="t2", result="bad", is_error=True),
        TurnFinished(text="hello world"),
    ]:
        terminal.handle(event)

    printed = capsys.readouterr().out
    assert "hello world" in printed
    assert "read_file" in printed and "12ms" in printed
    assert "bad" in printed


def test_the_cli_parses_its_subcommands():
    parser = cli.build_parser()
    assert parser.parse_args(["ask", "what", "time"]).question == ["what", "time"]
    assert parser.parse_args(["web", "--port", "9000"]).port == 9000
    assert parser.parse_args(["voice", "--once"]).once is True
    assert parser.parse_args([]).command is None


def test_doctor_reports_on_mongo(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MONGODB_URI", "mongodb+srv://isa:hunter2@cluster0.abc.mongodb.net/")
    cli.main(["doctor"])

    printed = capsys.readouterr().out
    assert "workspace" in printed
    assert "mongodb" in printed
    assert "cluster0.abc.mongodb.net" in printed
    assert "hunter2" not in printed  # the password never reaches the terminal


def test_memory_and_reminder_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    from jarvis.config import JarvisConfig
    from jarvis.memory import MongoStore, utcnow

    config = JarvisConfig.load(config_file=tmp_path / "none.toml")
    store = MongoStore(config.mongodb_uri, config.mongodb_db)
    store.remember("editor", "neovim", "preference")
    store.add_reminder("standup", utcnow())

    assert cli.main(["memory"]) == 0
    assert "neovim" in capsys.readouterr().out

    assert cli.main(["reminders"]) == 0
    assert "standup" in capsys.readouterr().out

    assert cli.main(["memory", "--forget", "editor"]) == 0
    assert "forgot" in capsys.readouterr().out


# --- web -------------------------------------------------------------
@pytest.fixture
def web_client(config, workspace, store):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from jarvis.web.server import create_app

    config.approval = "prompt"
    holder: dict = {}

    def factory():
        session = Session(
            config,
            interface="web",
            client=holder["client"],
            store=store,
            persist=False,
        )
        holder["session"] = session
        return session

    app = create_app(config, session_factory=factory)
    return fastapi_testclient.TestClient(app), holder


def test_web_health_and_page(web_client):
    client, holder = web_client
    holder["client"] = FakeClient([])

    assert client.get("/api/health").json()["model"] == "claude-opus-5"

    page = client.get("/").text
    assert "<title>Jarvis</title>" in page
    assert "/static/app.js" in page and "/static/style.css" in page
    # The pieces the interface is actually made of: the orb canvas, the inbox,
    # the kill switch, and the composer.
    for element in ('id="orb"', 'id="notices-button"', 'id="hold"', 'id="input"'):
        assert element in page, element


def test_web_socket_streams_a_turn(web_client):
    client, holder = web_client
    holder["client"] = FakeClient([text_turn("Good evening.")])

    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["kind"] == "ready"
        socket.send_json({"type": "message", "text": "hello"})

        kinds = []
        while True:
            event = socket.receive_json()
            kinds.append(event["kind"])
            if event["kind"] == "turn_finished":
                assert event["text"] == "Good evening."
                break
        assert "text" in kinds


def test_web_socket_asks_the_browser_for_approval(web_client, workspace):
    client, holder = web_client
    holder["client"] = FakeClient([
        tool_turn([("write_file", {"path": "notes.txt", "content": "hi"})]),
        text_turn("Written."),
    ])

    with client.websocket_connect("/ws") as socket:
        socket.receive_json()  # ready
        socket.send_json({"type": "message", "text": "write notes.txt"})

        while True:
            event = socket.receive_json()
            if event["kind"] == "approval_request":
                assert "file" in event["action"]
                socket.send_json({"type": "approval", "id": event["id"], "allow": True})
            if event["kind"] == "turn_finished":
                break

    assert (workspace / "notes.txt").read_text() == "hi"


def test_a_denied_approval_stops_the_write(web_client, workspace):
    client, holder = web_client
    holder["client"] = FakeClient([
        tool_turn([("write_file", {"path": "nope.txt", "content": "hi"})]),
        text_turn("I did not write it."),
    ])

    with client.websocket_connect("/ws") as socket:
        socket.receive_json()
        socket.send_json({"type": "message", "text": "write nope.txt"})
        while True:
            event = socket.receive_json()
            if event["kind"] == "approval_request":
                socket.send_json({"type": "approval", "id": event["id"], "allow": False})
            if event["kind"] == "turn_finished":
                break

    assert not (workspace / "nope.txt").exists()


# --- voice -----------------------------------------------------------
@pytest.mark.parametrize(
    "heard,expected",
    [
        ("Jarvis, what time is it?", "what time is it"),
        ("hey jarvis remind me at five", "remind me at five"),
        ("JARVIS", ""),
        ("I was telling Bob that jarvis handles this", None),
        ("what time is it", None),
    ],
)
def test_wake_word_detection(heard, expected):
    assert strip_wake_word(heard, "jarvis") == expected


def test_speech_strips_what_cannot_be_spoken():
    spoken = speakable(
        "**Done.** Edited `/home/me/project/config.py`, see "
        "[the docs](https://example.com/docs).\n\n```python\nx = 1\n```"
    )
    assert "**" not in spoken and "`" not in spoken
    assert "https://" not in spoken
    assert "config.py" in spoken
    assert "code omitted" in spoken


def test_long_answers_are_cut_at_a_sentence():
    spoken = speakable("First sentence. " + "padding words here. " * 200)
    assert len(spoken) < 1400
    assert spoken.endswith("ask me to continue.")


def test_tts_falls_back_to_printing(capsys):
    speaker = load_tts("print")
    assert isinstance(speaker, PrintSpeaker)
    speaker.say("hello")
    assert "hello" in capsys.readouterr().out


def test_markup_in_tool_output_is_not_interpreted(capsys):
    """A file full of [brackets] must not corrupt the terminal or crash rich."""
    terminal = cli.Terminal(session=None)
    terminal.handle(ToolStarted(name="read_file", tool_use_id="t1", input={"path": "[bold]x[/]"}))
    terminal.handle(
        ToolFinished(name="read_file", tool_use_id="t1", result="[/not a tag]", is_error=True)
    )
    printed = capsys.readouterr().out
    assert "[bold]x[/]" in printed
    assert "[/not a tag]" in printed


def test_page_ids_are_unique_and_every_script_target_exists():
    """A duplicate id once pointed the approval modal at the header readout,
    so the browser never showed the dialog. Catch that class of bug here."""
    import re

    from jarvis.web.server import STATIC

    html = (STATIC / "index.html").read_text()
    script = (STATIC / "app.js").read_text()

    ids = re.findall(r'id="([^"]+)"', html)
    duplicates = {name for name in ids if ids.count(name) > 1}
    assert not duplicates, f"duplicate element ids: {duplicates}"

    wanted = set(re.findall(r'getElementById\("([^"]+)"\)', script))
    missing = wanted - set(ids)
    assert not missing, f"app.js reaches for elements that do not exist: {missing}"


# --- doctor, on a fresh Linux box ------------------------------------
def test_pip_hints_are_copy_pasteable():
    # Escaped once at print time, not twice: a literal backslash in a command
    # the user is meant to copy is a bug, not a cosmetic one.
    assert cli._pip_hint("voice") == "pip install 'jarvis[voice]'"
    assert "\\" not in cli._pip_hint("voice")


def test_a_missing_system_library_is_reported_not_raised(monkeypatch):
    """The regression this exists to prevent.

    `sounddevice` raises OSError - not ImportError - when PortAudio is absent,
    which is the single most common fresh-Linux problem. Catching only
    ImportError took `jarvis doctor` down with a traceback at exactly the
    moment its whole job was to explain what was wrong.
    """
    import builtins

    real_import = builtins.__import__

    def explode(name, *args, **kwargs):
        if name == "sounddevice":
            raise OSError("PortAudio library not found")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", explode)
    good, detail = cli._probe_audio()

    assert good is False
    assert "PortAudio library not found" in detail


def test_audio_tells_apt_distros_which_package_to_install(monkeypatch, tmp_path):
    release = tmp_path / "os-release"
    release.write_text('ID=linuxmint\nID_LIKE=ubuntu debian\nUBUNTU_CODENAME=noble\n')
    monkeypatch.setattr(cli, "Path", lambda p="": release if p == "/etc/os-release" else Path(p))
    assert cli._apt_hint("libportaudio2") == "sudo apt install libportaudio2"


def test_no_apt_advice_where_apt_is_the_wrong_answer(monkeypatch, tmp_path):
    release = tmp_path / "os-release"
    release.write_text('ID=fedora\nID_LIKE="rhel centos"\n')
    monkeypatch.setattr(cli, "Path", lambda p="": release if p == "/etc/os-release" else Path(p))
    assert cli._apt_hint("libportaudio2") == ""


def test_speech_out_reports_what_it_would_actually_speak_through(monkeypatch):
    # pyttsx3 imports cleanly with no engine behind it, so importing proves
    # nothing: the probe has to ask what load_tts actually picked.
    from jarvis.voice.tts import PrintSpeaker

    monkeypatch.setattr("jarvis.voice.tts.load_tts", lambda backend="auto": PrintSpeaker())
    good, detail = cli._probe_tts()
    assert good is False
    assert "printed" in detail


def test_doctor_survives_every_optional_probe_failing(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_WORKSPACE", str(tmp_path))

    def explode() -> tuple[bool, str]:
        raise OSError("some system library is missing")

    for name in ("_probe_web", "_probe_audio", "_probe_stt", "_probe_tts"):
        monkeypatch.setattr(cli, name, explode)

    assert cli.main(["doctor"]) in (0, 1)          # reports, never raises
    printed = capsys.readouterr().out
    assert "some system library is missing" in printed


# --- the socket checks who is asking ---------------------------------
def test_a_foreign_page_cannot_open_the_websocket(web_client):
    """Same-origin policy does not apply to WebSockets.

    Any page the user visits can open ws://127.0.0.1:8765/ws. Approvals are
    answered over that same socket, so without this check such a page would
    drive the agent *and* approve its own actions - the confirmation gate would
    be protecting nobody.
    """
    from starlette.websockets import WebSocketDisconnect

    client, holder = web_client
    holder["client"] = FakeClient([])

    # The handshake is refused, so the connection never opens: no `ready`, no
    # chance to send a message or approve anything.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/ws", headers={"origin": "https://evil.example.com"}
        ) as socket:
            socket.receive_json()


def test_the_real_page_still_connects(web_client):
    client, holder = web_client
    holder["client"] = FakeClient([])

    with client.websocket_connect("/ws", headers={"origin": "http://127.0.0.1:8765"}) as socket:
        assert socket.receive_json()["kind"] == "ready"


def test_a_non_browser_client_with_no_origin_is_allowed(web_client):
    # curl, a script, this test: no Origin header. A browser cannot suppress
    # it, so absence is not something an attacking page can fake.
    client, holder = web_client
    holder["client"] = FakeClient([])

    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["kind"] == "ready"


def test_the_allowed_origin_set_follows_the_configured_port():
    from jarvis.web.server import allowed_origins

    permitted = allowed_origins("127.0.0.1", 9000)
    assert "http://localhost:9000" in permitted
    assert "http://127.0.0.1:9000" in permitted
    assert "http://localhost:8765" not in permitted   # a different port is a different origin
