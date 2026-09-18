"""The voice loop: press, speak, hear it answer.

A thin adapter, not a second assistant. Input arrives as a transcript instead
of a typed line and output is spoken as well as printed; between those two ends
it is `Session.send`, the same brain the terminal and the browser use. If this
file ever grows agent logic, that is the bug.

Three things make it feel alive rather than laggy:

- the mic opens when you say so, and closes when you stop talking, so there is
  never a question of whether it is listening;
- it speaks each sentence as the model finishes writing it, instead of waiting
  for the whole answer;
- a keypress cuts it off mid-sentence.
"""

from __future__ import annotations

import re
import threading
from typing import Any

from rich.console import Console
from rich.markup import escape

from ..config import JarvisConfig
from ..events import (
    ErrorEvent,
    NoticeSurfaced,
    ReminderFired,
    TextDelta,
    ToolStarted,
    TurnFinished,
)
from ..session import Session
from .audio import KeyWatcher, Recording, calibrate, record_utterance, wait_for_quiet
from .stt import TranscriptionError, load_stt
from .tts import SentenceStream, load_tts, speakable

console = Console()

YES = {"yes", "yeah", "yep", "sure", "ok", "okay", "go ahead", "do it", "please do", "affirmative"}
NO = {"no", "nope", "don't", "dont", "stop", "cancel", "negative", "no thanks"}

_PUNCT = re.compile(r"[^\w\s']")


def normalize(text: str) -> str:
    return _PUNCT.sub(" ", text.lower()).strip()


def strip_wake_word(text: str, wake_word: str) -> str | None:
    """Return what was said after the wake word, or None if it was not said.

    An empty string means the wake word was all they said - the caller should
    answer and listen again.
    """
    words = normalize(text).split()
    wake = normalize(wake_word).split()
    if not wake:
        return text.strip()
    # Only look near the start: "jarvis" halfway through a sentence is not a summons.
    for start in range(min(3, len(words))):
        if words[start : start + len(wake)] == wake:
            return " ".join(words[start + len(wake) :]).strip()
    return None


class VoiceLoop:
    """Owns the microphone, the speaker, and one session."""

    def __init__(
        self,
        config: JarvisConfig,
        use_wake_word: bool | None = None,
        session: Session | None = None,
        speaker: Any = None,
        transcriber: Any = None,
        recorder: Any = None,
    ) -> None:
        self.config = config
        if use_wake_word is None:
            use_wake_word = config.voice_input == "wake"
        self.use_wake_word = use_wake_word
        # Injectable so a test can drive a whole turn without a microphone.
        self.speaker = speaker or load_tts(config.tts_backend, config)
        self.transcriber = transcriber or load_stt(config)
        self.recorder = recorder or record_utterance
        self.session = session or Session(config, interface="voice")
        self.session.context.confirm = self.spoken_confirm
        self.threshold = 0.02
        self._interrupted = threading.Event()

    # -- speaking ------------------------------------------------------
    def say(self, text: str, show: bool = True) -> None:
        spoken = speakable(text)
        if not spoken:
            return
        if show:
            console.print(f"[cyan]jarvis[/cyan] {escape(spoken)}")
        self.speaker.say(spoken)
        wait_for_quiet()

    # -- listening -----------------------------------------------------
    def listen(self, start_timeout: float | None = None) -> str:
        recording: Recording = self.recorder(
            threshold=self.threshold,
            silence_seconds=self.config.voice_silence_seconds,
            max_seconds=self.config.voice_max_seconds,
            start_timeout=start_timeout,
        )
        if not recording:
            return ""
        try:
            return self.transcriber.transcribe(recording.samples)
        except TranscriptionError as exc:
            console.print(f"  [red]{escape(str(exc))}[/red]")
            return ""

    def spoken_confirm(self, action: str, detail: str) -> bool:
        """Approval, asked out loud. The gate is the same one the terminal uses."""
        self.say(f"Do you want me to {action}? {detail}")
        for _ in range(2):
            answer = normalize(self.listen(start_timeout=10.0))
            if not answer:
                continue
            console.print(f"[dim]you: {escape(answer)}[/dim]")
            if any(word in answer for word in YES):
                return True
            if any(word in answer for word in NO):
                return False
            self.say("Was that a yes?")
        self.say("I'll leave it.")
        return False

    # -- turns ---------------------------------------------------------
    def answer(self, question: str) -> None:
        """Run one turn, speaking each sentence as it is finished.

        The model streams; sentences are spoken the moment they are whole. That
        turns "wait for the whole answer, then start talking" into "start
        talking almost immediately", which is most of the perceived latency.
        """
        self._interrupted.clear()
        watcher = KeyWatcher(self.barge_in)
        watcher.start()

        stream = SentenceStream()
        spoken_any = False
        failed = ""

        try:
            for event in self.session.send(question):
                if self._interrupted.is_set():
                    self.session.interrupt()
                    break
                if isinstance(event, TextDelta):
                    console.print(event.text, end="", markup=False, highlight=False)
                    for sentence in stream.feed(event.text):
                        self.speaker.say(speakable(sentence))
                        spoken_any = True
                        if self._interrupted.is_set():
                            break
                elif isinstance(event, ToolStarted):
                    console.print(f"\n  [dim]⚙ {escape(event.name)}[/dim]")
                elif isinstance(event, ErrorEvent):
                    console.print(f"\n  [red]{escape(event.message)}[/red]")
                    failed = "Something went wrong there."
                elif isinstance(event, TurnFinished):
                    console.print()
        finally:
            watcher.stop()

        if self._interrupted.is_set():
            console.print("  [yellow]stopped[/yellow]")
            return

        remainder = stream.flush()
        if remainder:
            self.say(remainder, show=not spoken_any)
        elif not spoken_any:
            self.say(failed or "I don't have an answer for that.")

    def barge_in(self) -> None:
        """A keypress while it is talking means: stop, I am speaking now."""
        self._interrupted.set()
        self.speaker.stop()

    def on_surfaced(self, event: Any) -> None:
        """What the heartbeat pushes, said out loud."""
        if isinstance(event, ReminderFired):
            self.say(f"Reminder: {event.text}")
        elif isinstance(event, NoticeSurfaced):
            lead = "Something urgent" if event.level == "urgent" else "Worth knowing"
            self.say(f"{lead}: {event.text}")

    # -- the loop ------------------------------------------------------
    def _banner(self) -> None:
        how = (
            f"wake word: {self.config.wake_word}"
            if self.use_wake_word
            else "press enter to speak"
        )
        console.print(
            f"[bold cyan]◈ jarvis[/bold cyan] "
            f"[dim]({self.transcriber.name} · {self.speaker.name} · {how})[/dim]"
        )

    def _prepare(self) -> None:
        console.print("[dim]calibrating the room…[/dim]")
        try:
            self.threshold = calibrate()
        except Exception as exc:
            console.print(f"[yellow]could not calibrate ({exc}); using a default gate[/yellow]")

    def one_turn(self) -> bool:
        """Wait for a turn, run it. False means the user wants out."""
        if not self.use_wake_word:
            from .audio import wait_for_key

            if not wait_for_key("[press enter to speak]"):
                return False
            # A sign the instant the key lands: silence here reads as "it broke".
            console.print("[bold cyan]● listening[/bold cyan] [dim]— speak, then pause[/dim]")

        heard = self.listen()
        if not heard:
            if not self.use_wake_word:
                console.print("[dim]heard nothing[/dim]")
            return True

        question = heard
        if self.use_wake_word:
            remainder = strip_wake_word(heard, self.config.wake_word)
            if remainder is None:
                return True  # not addressed to Jarvis
            if not remainder:
                self.say("Yes?")
                remainder = self.listen(start_timeout=6.0)
                if not remainder:
                    return True
                heard = remainder
            question = remainder

        # Always show what it thought it heard: when it answers the wrong
        # question, this is what tells you whether the ears or the brain missed.
        console.print(f"[dim]heard:[/dim] {escape(heard)}")
        self.answer(question)
        return True

    def run(self, once: bool = False) -> int:
        self._banner()
        self._prepare()
        console.print("[dim]ready — Ctrl-C to stop[/dim]\n")

        self.session.start_heartbeat(self.on_surfaced)
        for held in self.session.catch_up():
            self.on_surfaced(held)

        try:
            while True:
                if not self.one_turn():
                    return 0
                if once:
                    return 0
        except KeyboardInterrupt:
            console.print("\n[dim]goodbye[/dim]")
            return 0
        finally:
            self.session.close()


def run_voice(config: JarvisConfig, once: bool = False, use_wake_word: bool | None = None) -> int:
    return VoiceLoop(config, use_wake_word=use_wake_word).run(once=once)
