"""Text to speech, and making text worth speaking.

One seam: give me text, say it aloud. ElevenLabs when a key is present -
natural enough that the assistant reads as a presence rather than a machine
reading - with the local engines as the offline fallback.

Every speaker is interruptible. `stop()` cuts playback mid-sentence, which is
what makes barge-in possible: an assistant you cannot cut off stops being
usable about a minute after the novelty wears off.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from ..config import JarvisConfig

ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech"
ELEVENLABS_TIMEOUT = 30.0
SAMPLE_RATE = 16000


class Speaker(Protocol):
    def say(self, text: str) -> None: ...
    def stop(self) -> None: ...
    @property
    def name(self) -> str: ...


# --- making text speakable -------------------------------------------
_CODE_FENCE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE = re.compile(r"`([^`]*)`")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BARE_URL = re.compile(r"https?://\S+")
_MARKUP = re.compile(r"[*_#>|]+")
_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_PATH = re.compile(r"(?<![\w/])(/[\w./-]{6,})")


def speakable(text: str, limit: int = 1200) -> str:
    """Strip what does not survive being read aloud."""
    spoken = _CODE_FENCE.sub(" (code omitted) ", text)
    spoken = _LINK.sub(r"\1", spoken)
    spoken = _BARE_URL.sub("a link", spoken)
    spoken = _INLINE_CODE.sub(r"\1", spoken)
    spoken = _BULLET.sub("", spoken)
    spoken = _MARKUP.sub("", spoken)
    spoken = _PATH.sub(lambda m: m.group(1).rsplit("/", 1)[-1], spoken)
    spoken = re.sub(r"\n{2,}", ". ", spoken)
    spoken = re.sub(r"\s+", " ", spoken)
    spoken = re.sub(r"\.(\s*\.)+", ".", spoken).strip()
    if len(spoken) > limit:
        cut = spoken[:limit].rsplit(". ", 1)[0]
        spoken = (cut or spoken[:limit]) + ". There is more, ask me to continue."
    return spoken


# --- backends --------------------------------------------------------
class PrintSpeaker:
    """The fallback: no audio, just show what would have been said."""

    name = "print"

    def say(self, text: str) -> None:
        print(f"[jarvis] {text}")

    def stop(self) -> None:
        return None


class CommandSpeaker:
    """A system speech binary: macOS `say`, `espeak-ng`, or `spd-say`."""

    def __init__(self, command: list[str], name: str) -> None:
        self.command = command
        self._name = name
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def name(self) -> str:
        return self._name

    def __post_init__(self) -> None:  # pragma: no cover - not a dataclass
        return None

    def say(self, text: str) -> None:
        if not text.strip():
            return
        try:
            with subprocess.Popen([*self.command, text]) as process:
                self._process = process
                process.wait(timeout=180)
        except (OSError, subprocess.TimeoutExpired):
            PrintSpeaker().say(text)
        finally:
            self._process = None

    def stop(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()


class Pyttsx3Speaker:
    """Offline, cross-platform. One engine, guarded by a lock."""

    name = "pyttsx3"

    def __init__(self) -> None:
        import pyttsx3

        self._engine: Any = pyttsx3.init()
        self._lock = threading.Lock()

    def say(self, text: str) -> None:
        if not text.strip():
            return
        with self._lock:
            self._engine.say(text)
            self._engine.runAndWait()

    def stop(self) -> None:
        try:
            self._engine.stop()
        except Exception:  # pragma: no cover - engine state varies by platform
            pass


class ElevenLabsSpeaker:
    """ElevenLabs, streamed as raw PCM and played as it arrives.

    PCM rather than MP3 on purpose: it needs no decoder, so playback starts on
    the first chunk off the socket instead of after a file has been assembled.
    That is most of the difference between an assistant that answers and one
    that pauses first.
    """

    def __init__(
        self,
        api_key: str,
        voice_id: str,
        model: str = "eleven_turbo_v2_5",
        opener: Any = None,
    ) -> None:
        self.api_key = api_key
        self.voice_id = voice_id
        self.model = model
        self._open = opener or self._open_stream
        self._stop = threading.Event()
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return f"elevenlabs {self.model}"

    def _open_stream(self, text: str) -> Any:
        url = (
            f"{ELEVENLABS_URL}/{self.voice_id}/stream"
            f"?output_format=pcm_{SAMPLE_RATE}&optimize_streaming_latency=3"
        )
        body = json.dumps({"text": text, "model_id": self.model}).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "xi-api-key": self.api_key,
                "Content-Type": "application/json",
                "Accept": "audio/pcm",
            },
        )
        return urllib.request.urlopen(request, timeout=ELEVENLABS_TIMEOUT)  # noqa: S310

    def say(self, text: str) -> None:
        if not text.strip():
            return
        self._stop.clear()
        try:
            self._play(text)
        except urllib.error.HTTPError as exc:
            hint = (
                " - check ELEVENLABS_API_KEY"
                if exc.code in (401, 403)
                else " - the voice id may be wrong"
                if exc.code == 404
                else ""
            )
            print(f"[jarvis] speech failed ({exc.code}{hint}); printing instead")
            PrintSpeaker().say(text)
        except Exception as exc:  # never lose the answer because audio failed
            print(f"[jarvis] speech failed ({type(exc).__name__}: {exc}); printing instead")
            PrintSpeaker().say(text)

    def _play(self, text: str) -> None:
        import numpy
        import sounddevice

        with self._lock, self._open(text) as response:
            with sounddevice.OutputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16"
            ) as out:
                while not self._stop.is_set():
                    chunk = response.read(4096)
                    if not chunk:
                        break
                    # A half sample at a chunk boundary would click.
                    if len(chunk) % 2:
                        chunk += response.read(1)
                    out.write(numpy.frombuffer(chunk, dtype="<i2"))

    def stop(self) -> None:
        """Cut playback at the next chunk. This is what barge-in calls."""
        self._stop.set()


class SentenceStream:
    """Turns a stream of text deltas into whole sentences, as soon as they are whole.

    The agent streams tokens and ElevenLabs wants text. Waiting for the full
    answer before speaking wastes the entire generation time; speaking every
    delta produces stuttering nonsense. Sentences are the unit that is both
    complete enough to pronounce and short enough to start early.
    """

    _END = re.compile(r"(?<=[.!?])\s+|\n{2,}")

    def __init__(self, minimum: int = 12) -> None:
        self.minimum = minimum
        self._buffer = ""

    def feed(self, delta: str) -> list[str]:
        """Add text; return any sentences that are now complete."""
        self._buffer += delta
        done: list[str] = []
        start = 0
        while True:
            match = self._END.search(self._buffer, start)
            if not match:
                break
            sentence = self._buffer[: match.start()].strip()
            # An abbreviation or a decimal point is not the end of a thought.
            # Move the scan past it and keep looking - rebuilding the buffer
            # from the same pieces would find this boundary again, forever.
            if len(sentence) < self.minimum:
                start = match.end()
                continue
            done.append(sentence)
            self._buffer = self._buffer[match.end() :]
            start = 0
        return done

    def flush(self) -> str:
        """Whatever is left at the end of the turn."""
        rest = self._buffer.strip()
        self._buffer = ""
        return rest


def load_tts(backend: str = "auto", config: JarvisConfig | None = None) -> Speaker:
    """Pick a speech backend. 'auto' takes the best one available here.

    ElevenLabs first when a key is set, then whatever the machine already has.
    The order is deliberate: the local engines always work, so without this the
    better voice would never be reached.
    """
    if config is not None:
        backend = (config.tts_backend or backend or "auto").lower()

    if backend == "print":
        return PrintSpeaker()

    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    voice = (getattr(config, "tts_voice", "") or os.environ.get("JARVIS_TTS_VOICE", "")).strip()
    if backend in {"auto", "elevenlabs"} and key:
        if not voice:
            # A voice id is not guessable, and a wrong one is a 404 mid-sentence.
            print(
                "[jarvis] ELEVENLABS_API_KEY is set but no voice chosen - "
                "set JARVIS_TTS_VOICE to a voice id from elevenlabs.io/app/voice-library"
            )
        else:
            return ElevenLabsSpeaker(
                key,
                voice,
                model=getattr(config, "tts_model", "eleven_turbo_v2_5") or "eleven_turbo_v2_5",
            )
    elif backend == "elevenlabs" and not key:
        print("[jarvis] JARVIS_TTS_BACKEND=elevenlabs but ELEVENLABS_API_KEY is not set")

    if backend in {"auto", "pyttsx3"}:
        try:
            return Pyttsx3Speaker()
        except Exception:
            if backend == "pyttsx3":
                raise
    if backend in {"auto", "say"} and platform.system() == "Darwin" and shutil.which("say"):
        return CommandSpeaker(["say"], "say")
    if backend in {"auto", "espeak"}:
        for binary in ("espeak-ng", "espeak"):
            if shutil.which(binary):
                return CommandSpeaker([binary], binary)
    if backend in {"auto", "spd-say"} and shutil.which("spd-say"):
        return CommandSpeaker(["spd-say", "-e"], "spd-say")
    return PrintSpeaker()
