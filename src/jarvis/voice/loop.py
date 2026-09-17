"""The voice loop: wake word, transcribe, answer, speak.

Speech is answered from the final message rather than token by token - a
half-formed sentence read aloud is worse than a short wait.
"""

from __future__ import annotations

import re
from typing import Any

from rich.console import Console
from rich.markup import escape

from ..config import JarvisConfig
from ..events import ErrorEvent, ReminderFired, ToolStarted, TurnFinished
from ..session import Session
from .audio import Recording, calibrate, record_utterance, wait_for_quiet
from .stt import load_stt
from .tts import load_tts, speakable

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

    def __init__(self, config: JarvisConfig, use_wake_word: bool = True) -> None:
        self.config = config
        self.use_wake_word = use_wake_word
        self.speaker = load_tts(config.tts_backend)
        self.transcriber = load_stt(config.stt_model)
        self.session = Session(config, interface="voice")
        self.session.context.confirm = self.spoken_confirm
        self.threshold = 0.02

    # -- speaking ------------------------------------------------------
    def say(self, text: str) -> None:
        spoken = speakable(text)
        if not spoken:
            return
        console.print(f"[cyan]jarvis[/cyan] {escape(spoken)}")
        self.speaker.say(spoken)
        wait_for_quiet()

    # -- listening -----------------------------------------------------
    def listen(self, start_timeout: float | None = None) -> str:
        recording: Recording = record_utterance(
            threshold=self.threshold,
            silence_seconds=self.config.voice_silence_seconds,
            max_seconds=self.config.voice_max_seconds,
            start_timeout=start_timeout,
        )
        if not recording:
            return ""
        return self.transcriber.transcribe(recording.samples)

    def spoken_confirm(self, action: str, detail: str) -> bool:
        """Approval, asked out loud."""
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
        answer_text = ""
        for event in self.session.send(question):
            if isinstance(event, ToolStarted):
                console.print(f"  [dim]⚙ {escape(event.name)}[/dim]")
            elif isinstance(event, ErrorEvent):
                console.print(f"  [red]{escape(event.message)}[/red]")
                answer_text = answer_text or "Something went wrong there."
            elif isinstance(event, TurnFinished):
                answer_text = event.text or answer_text
        self.say(answer_text or "I don't have an answer for that.")

    def on_reminder(self, event: Any) -> None:
        if isinstance(event, ReminderFired):
            self.say(f"Reminder: {event.text}")

    # -- the loop ------------------------------------------------------
    def run(self, once: bool = False) -> int:
        wake = (
            f"wake word: {self.config.wake_word}" if self.use_wake_word else "no wake word"
        )
        console.print(
            f"[bold cyan]◈ jarvis is listening[/bold cyan] "
            f"[dim]({self.speaker.name} · whisper {self.config.stt_model} · "
            f"{wake})[/dim]"
        )
        console.print("[dim]calibrating the room…[/dim]")
        try:
            self.threshold = calibrate()
        except Exception as exc:
            console.print(f"[yellow]could not calibrate ({exc}); using a default gate[/yellow]")
        console.print("[dim]ready - Ctrl-C to stop[/dim]\n")

        self.session.start_reminders(self.on_reminder)

        try:
            while True:
                heard = self.listen()
                if not heard:
                    continue
                console.print(f"[dim]heard:[/dim] {escape(heard)}")

                question = heard
                if self.use_wake_word:
                    remainder = strip_wake_word(heard, self.config.wake_word)
                    if remainder is None:
                        continue  # not addressed to Jarvis
                    if not remainder:
                        self.say("Yes?")
                        remainder = self.listen(start_timeout=6.0)
                        if not remainder:
                            continue
                        console.print(f"[dim]heard:[/dim] {escape(remainder)}")
                    question = remainder

                self.answer(question)
                if once:
                    return 0
        except KeyboardInterrupt:
            console.print("\n[dim]goodbye[/dim]")
            return 0
        finally:
            self.session.close()


def run_voice(config: JarvisConfig, once: bool = False, use_wake_word: bool = True) -> int:
    return VoiceLoop(config, use_wake_word=use_wake_word).run(once=once)
