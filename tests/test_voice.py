"""Tier 3: the voice layer.

No microphone and no provider keys in CI, so the audio and the network sit
behind seams and these tests drive the whole turn through fakes. What is tested
is everything except the two ends: backend selection, the Deepgram request and
its failure modes, sentence streaming, barge-in, and that a spoken turn goes
through the same brain a typed one does.
"""

from __future__ import annotations

import json

import pytest
from conftest import FakeClient, text_turn, tool_turn

from jarvis.config import JarvisConfig
from jarvis.session import Session
from jarvis.voice import stt as stt_module
from jarvis.voice.audio import Recording
from jarvis.voice.loop import VoiceLoop, strip_wake_word
from jarvis.voice.stt import Deepgram, FasterWhisper, TranscriptionError, load_stt
from jarvis.voice.tts import ElevenLabsSpeaker, SentenceStream, load_tts


# --- choosing a backend ----------------------------------------------
def test_deepgram_is_preferred_when_a_key_is_present(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-key")
    assert isinstance(load_stt(JarvisConfig()), Deepgram)


def test_without_a_key_it_falls_back_to_local_whisper(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    transcriber = load_stt(JarvisConfig(stt_model="small.en"))
    assert isinstance(transcriber, FasterWhisper)
    assert transcriber.model_size == "small.en"   # the configured size, not the default


def test_asking_for_deepgram_without_a_key_says_so(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    with pytest.raises(TranscriptionError, match="DEEPGRAM_API_KEY"):
        load_stt(JarvisConfig(stt_backend="deepgram"))


def test_whisper_can_be_forced_even_with_a_key(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-key")
    assert isinstance(load_stt(JarvisConfig(stt_backend="whisper")), FasterWhisper)


def test_elevenlabs_needs_both_a_key_and_a_voice(monkeypatch, capsys):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-key")
    monkeypatch.delenv("JARVIS_TTS_VOICE", raising=False)

    # A voice id is not guessable, and the wrong one is a 404 mid-sentence.
    speaker = load_tts("auto", JarvisConfig(tts_voice=""))
    assert not isinstance(speaker, ElevenLabsSpeaker)
    assert "no voice chosen" in capsys.readouterr().out

    chosen = load_tts("auto", JarvisConfig(tts_voice="voice-123"))
    assert isinstance(chosen, ElevenLabsSpeaker)
    assert chosen.voice_id == "voice-123"


# --- talking to Deepgram ---------------------------------------------
def test_audio_is_sent_as_the_pcm_deepgram_expects(monkeypatch):
    import numpy

    sent: dict = {}

    def fake_post(url, body, headers, timeout):
        sent.update(url=url, body=body, headers=headers)
        return json.dumps(
            {"results": {"channels": [{"alternatives": [{"transcript": "what's on my list"}]}]}}
        ).encode()

    monkeypatch.setattr(stt_module, "post", fake_post)
    samples = numpy.array([0.0, 1.0, -1.0, 0.5], dtype="float32")

    assert Deepgram("dg-key", model="nova-3").transcribe(samples) == "what's on my list"
    assert "model=nova-3" in sent["url"]
    assert sent["headers"]["Authorization"] == "Token dg-key"
    assert sent["headers"]["Content-Type"] == "audio/l16;rate=16000;channels=1"
    # float32 in [-1,1] becomes little-endian int16: two bytes per sample.
    assert len(sent["body"]) == 8
    assert int.from_bytes(sent["body"][2:4], "little", signed=True) == 32767


def test_silence_is_not_sent_anywhere(monkeypatch):
    import numpy

    def explode(*args, **kwargs):
        raise AssertionError("an empty recording should never reach the network")

    monkeypatch.setattr(stt_module, "post", explode)
    assert Deepgram("dg-key").transcribe(numpy.zeros(0, dtype="float32")) == ""


def test_a_rejected_key_points_at_the_local_fallback(monkeypatch):
    import urllib.error

    import numpy

    def unauthorised(*args, **kwargs):
        raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(stt_module, "post", unauthorised)
    with pytest.raises(TranscriptionError, match="whisper"):
        Deepgram("bad").transcribe(numpy.array([0.1], dtype="float32"))


# --- speaking as it thinks -------------------------------------------
@pytest.mark.parametrize(
    ("deltas", "expected"),
    [
        (["Hello there. ", "How are you?"], ["Hello there."]),
        (["One sentence only"], []),                      # nothing complete yet
        (
            ["Six TODOs, all in the parser.\n\n", "Four are errors."],
            ["Six TODOs, all in the parser."],
        ),
    ],
)
def test_sentences_come_out_as_soon_as_they_are_whole(deltas, expected):
    stream = SentenceStream()
    got: list[str] = []
    for delta in deltas:
        got.extend(stream.feed(delta))
    assert got == expected


def test_a_short_fragment_is_held_back_rather_than_spoken_alone():
    # "Dr. Smith" must not be spoken as "Dr." then "Smith".
    stream = SentenceStream()
    assert stream.feed("Dr. ") == []
    assert stream.feed("Smith says the build is broken. ") == [
        "Dr. Smith says the build is broken."
    ]


def test_the_tail_of_a_turn_is_never_dropped():
    stream = SentenceStream()
    # "Done." is shorter than the minimum, so it is held rather than spoken as
    # a fragment - and then it must still come out at the end.
    assert stream.feed("Done. ") == []
    assert stream.feed("And one more thing") == []
    assert stream.flush() == "Done. And one more thing"
    assert stream.flush() == ""


def test_a_long_first_sentence_is_spoken_and_only_the_tail_remains():
    stream = SentenceStream()
    assert stream.feed("The build is broken in the parser. ") == [
        "The build is broken in the parser."
    ]
    assert stream.feed("Want me to fix it") == []
    assert stream.flush() == "Want me to fix it"


# --- a whole spoken turn ---------------------------------------------
class FakeSpeaker:
    name = "fake"

    def __init__(self) -> None:
        self.said: list[str] = []
        self.stopped = False

    def say(self, text: str) -> None:
        if text.strip():
            self.said.append(text)

    def stop(self) -> None:
        self.stopped = True


class FakeTranscriber:
    name = "fake"

    def __init__(self, text: str = "") -> None:
        self.text = text

    def transcribe(self, samples):
        return self.text


def _loop(config, store, client, speaker=None, heard="what is on my list"):
    import numpy

    def recorder(**kwargs):
        return Recording(samples=numpy.ones(8000, dtype="float32"), seconds=0.5)

    session = Session(config, interface="voice", client=client, store=store, persist=False)
    return VoiceLoop(
        config,
        session=session,
        speaker=speaker or FakeSpeaker(),
        transcriber=FakeTranscriber(heard),
        recorder=recorder,
    )


def test_a_spoken_turn_runs_the_same_brain_and_speaks_the_answer(config, store, workspace):
    client = FakeClient([
        tool_turn([("list_dir", {"path": "."})], text="Let me look."),
        text_turn("Nothing in there. Want me to create something?"),
    ])
    loop = _loop(config, store, client)

    loop.answer("what is in the workspace")

    spoken = " ".join(loop.speaker.said)
    assert "Nothing in there." in spoken
    assert "Want me to create something?" in spoken
    # The same tool registry a typed turn uses - not a second implementation.
    assert any("list_dir" in str(call) for call in client.calls)


def test_it_starts_speaking_before_the_answer_is_finished(config, store, workspace):
    """The whole point of streaming: the first sentence is spoken while the
    rest is still being written."""
    client = FakeClient([text_turn("First sentence. Second sentence. Third.")])
    speaker = FakeSpeaker()
    loop = _loop(config, store, client, speaker=speaker)

    loop.answer("say three things")

    # Spoken in pieces as they completed, not as one block at the end.
    assert len(speaker.said) >= 2
    assert speaker.said[0].startswith("First sentence")


def test_barge_in_stops_it_talking(config, store, workspace):
    """A keypress mid-answer stops it dead.

    The interrupt has to arrive *during* the turn, the way a real keypress
    does: `answer` clears the flag on entry so a new turn never inherits the
    last one's interruption.
    """
    client = FakeClient([
        text_turn("The build is broken in the parser. And the tests are red. And more. And more.")
    ])

    class InterruptingSpeaker(FakeSpeaker):
        loop: object = None

        def say(self, text: str) -> None:
            super().say(text)
            self.loop.barge_in()      # as the key watcher does

    speaker = InterruptingSpeaker()
    loop = _loop(config, store, client, speaker=speaker)
    speaker.loop = loop

    loop.answer("go on")

    assert speaker.stopped
    # It spoke the sentence that was already out, then stopped - it did not
    # carry on through the rest of the answer.
    assert len(speaker.said) == 1
    assert speaker.said[0].startswith("The build is broken")


def test_a_transcription_failure_does_not_take_the_loop_down(config, store, workspace, capsys):
    class Broken:
        name = "broken"

        def transcribe(self, samples):
            raise TranscriptionError("Deepgram rejected the key")

    loop = _loop(config, store, FakeClient([]))
    loop.transcriber = Broken()

    assert loop.listen() == ""     # returns empty, does not raise
    assert "rejected the key" in capsys.readouterr().out


# --- the wake word still works ---------------------------------------
@pytest.mark.parametrize(
    ("heard", "expected"),
    [
        ("Jarvis, what time is it?", "what time is it"),
        ("JARVIS", ""),
        ("I was telling Bob that jarvis handles this", None),
    ],
)
def test_wake_word_detection_is_unchanged(heard, expected):
    assert strip_wake_word(heard, "jarvis") == expected


def test_press_to_talk_is_the_default_and_wake_is_opt_in(config, store, workspace):
    loop = _loop(config, store, FakeClient([]))
    assert loop.use_wake_word is False

    config.voice_input = "wake"
    assert _loop(config, store, FakeClient([])).use_wake_word is True
