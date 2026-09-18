"""Microphone capture with a simple energy gate.

No wake-word engine: Jarvis records an utterance, transcribes it, and checks
the transcript for its name. One dependency fewer, and it works offline.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from ..errors import MissingDependency

SAMPLE_RATE = 16000
BLOCK = 1600  # 100ms


def _audio_modules() -> tuple[Any, Any]:
    try:
        import numpy
        import sounddevice
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise MissingDependency("Voice mode", "voice", "sounddevice") from exc
    return sounddevice, numpy


@dataclass
class Recording:
    """Captured audio, ready for transcription."""

    samples: Any  # numpy float32 array
    seconds: float

    def __bool__(self) -> bool:
        return self.seconds > 0.25


def calibrate(seconds: float = 0.6) -> float:
    """Measure the room, so the gate is not a guessed constant."""
    sounddevice, numpy = _audio_modules()
    frames = int(SAMPLE_RATE * seconds)
    ambient = sounddevice.rec(frames, samplerate=SAMPLE_RATE, channels=1, dtype="float32")
    sounddevice.wait()
    noise = float(numpy.sqrt(numpy.mean(numpy.square(ambient))))
    return max(noise * 3.5, 0.012)


def record_utterance(
    threshold: float,
    silence_seconds: float = 1.2,
    max_seconds: float = 30.0,
    start_timeout: float | None = None,
) -> Recording:
    """Record from speech start until it goes quiet again."""
    sounddevice, numpy = _audio_modules()
    blocks: list[Any] = []
    started = False
    quiet_for = 0.0
    waited = 0.0
    block_seconds = BLOCK / SAMPLE_RATE

    with sounddevice.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=BLOCK
    ) as stream:
        while True:
            chunk, _overflowed = stream.read(BLOCK)
            level = float(numpy.sqrt(numpy.mean(numpy.square(chunk))))

            if not started:
                waited += block_seconds
                if level >= threshold:
                    started = True
                    blocks.append(chunk.copy())
                elif start_timeout is not None and waited >= start_timeout:
                    return Recording(samples=numpy.zeros(0, dtype="float32"), seconds=0.0)
                continue

            blocks.append(chunk.copy())
            quiet_for = quiet_for + block_seconds if level < threshold else 0.0
            captured = len(blocks) * block_seconds
            if quiet_for >= silence_seconds or captured >= max_seconds:
                break

    samples = (
        numpy.concatenate(blocks, axis=0).flatten()
        if blocks
        else numpy.zeros(0, dtype="float32")
    )
    return Recording(samples=samples, seconds=len(samples) / SAMPLE_RATE)


def wait_for_quiet(seconds: float = 0.25) -> None:
    """Small pause so the speaker's own output is not recorded as input."""
    time.sleep(seconds)


# ----------------------------------------------------------------------
# push to talk
# ----------------------------------------------------------------------
# A terminal reports keys pressed, never keys released - there is no key-up
# event to read. True hold-to-talk therefore needs OS-level input (pynput, an
# X11 or Wayland connection), which breaks over SSH and adds a dependency for
# something the terminal can nearly do already.
#
# So "press to talk" is the default: one keypress opens the microphone, and it
# closes itself when you stop speaking. That keeps the property that actually
# matters - you always know whether it is listening, because you told it - and
# it works everywhere, including over SSH.


def wait_for_key(prompt: str = "press enter to speak") -> bool:
    """Block until the user asks to talk. False means they want out."""
    try:
        input(f"\r{prompt} ")
        return True
    except (EOFError, KeyboardInterrupt):
        return False


class KeyWatcher:
    """Watches for a keypress in the background, for barge-in.

    While Jarvis is speaking, one keypress means "stop, I am talking now". The
    thread is a daemon and is never joined: a blocking stdin read cannot be
    cancelled, so the alternative is hanging on exit.
    """

    def __init__(self, on_press: Any) -> None:
        self.on_press = on_press
        self._active = threading.Event()

    def start(self) -> None:
        self._active.set()

        def watch() -> None:
            try:
                input()
            except (EOFError, KeyboardInterrupt, OSError):
                return
            if self._active.is_set():
                self.on_press()

        threading.Thread(target=watch, name="jarvis-bargein", daemon=True).start()

    def stop(self) -> None:
        self._active.clear()


def record_push_to_talk(
    threshold: float,
    silence_seconds: float = 1.0,
    max_seconds: float = 60.0,
    announce: Any = None,
) -> Recording:
    """One utterance, starting the moment the key is pressed.

    `start_timeout=None` and no energy gate on the way in: you already said you
    were speaking, so waiting for a loud enough sound would only add a way for
    the first word to be clipped.
    """
    if announce:
        announce()
    return record_utterance(
        threshold=threshold,
        silence_seconds=silence_seconds,
        max_seconds=max_seconds,
        start_timeout=None,
    )
