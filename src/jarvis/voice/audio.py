"""Microphone capture with a simple energy gate.

No wake-word engine: Jarvis records an utterance, transcribes it, and checks
the transcript for its name. One dependency fewer, and it works offline.
"""

from __future__ import annotations

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
