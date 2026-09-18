"""Speech to text, behind one seam: give me audio, get back text.

Two backends. Deepgram is the default when a key is present - it is fast, and
the gap between you finishing a sentence and Jarvis understanding it is most of
what makes voice feel alive or dead. faster-whisper runs locally, needs no key
and no network, and is the fallback when there is no Deepgram key or no
internet. Neither is reachable from the other's code: adding a third is one
class and one line in `load_stt`.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, Protocol

from ..errors import JarvisError, MissingDependency

if TYPE_CHECKING:  # pragma: no cover
    from ..config import JarvisConfig

SAMPLE_RATE = 16000
DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
DEEPGRAM_TIMEOUT = 20.0


class TranscriptionError(JarvisError):
    """The transcriber could not turn this audio into words."""


class Transcriber(Protocol):
    def transcribe(self, samples: Any) -> str: ...

    @property
    def name(self) -> str: ...


def _pcm16(samples: Any) -> bytes:
    """float32 in [-1, 1] to little-endian signed 16-bit, which is what both
    Deepgram and every other audio API actually want."""
    import numpy

    clipped = numpy.clip(numpy.asarray(samples, dtype="float32"), -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> bytes:
    """One HTTP POST. A seam of its own so tests never need a network."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


class Deepgram:
    """Deepgram's prerecorded endpoint, given one complete utterance.

    Push-to-talk hands over a finished recording, so this posts it whole rather
    than holding a websocket open. Fewer moving parts, and the latency that
    matters is the same.
    """

    def __init__(self, api_key: str, model: str = "nova-3", language: str = "en") -> None:
        self.api_key = api_key
        self.model = model
        self.language = language

    @property
    def name(self) -> str:
        return f"deepgram {self.model}"

    def transcribe(self, samples: Any) -> str:
        if samples is None or len(samples) == 0:
            return ""
        query = f"?model={self.model}&language={self.language}&smart_format=true&punctuate=true"
        try:
            raw = post(
                DEEPGRAM_URL + query,
                _pcm16(samples),
                {
                    "Authorization": f"Token {self.api_key}",
                    "Content-Type": f"audio/l16;rate={SAMPLE_RATE};channels=1",
                },
                DEEPGRAM_TIMEOUT,
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200] if exc.fp else ""
            if exc.code in (401, 403):
                raise TranscriptionError(
                    "Deepgram rejected the key - check DEEPGRAM_API_KEY. "
                    "Set JARVIS_STT_BACKEND=whisper to transcribe locally instead."
                ) from exc
            raise TranscriptionError(f"Deepgram returned {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise TranscriptionError(
                f"could not reach Deepgram ({exc}). JARVIS_STT_BACKEND=whisper "
                "transcribes locally with no network."
            ) from exc

        try:
            payload = json.loads(raw)
            alternatives = payload["results"]["channels"][0]["alternatives"]
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            raise TranscriptionError("Deepgram sent back something unreadable") from exc
        return str(alternatives[0].get("transcript", "")).strip() if alternatives else ""


class FasterWhisper:
    """Local Whisper via faster-whisper. The model loads once, on first use."""

    def __init__(self, model_size: str = "base.en") -> None:
        self.model_size = model_size
        self._model: Any = None

    @property
    def name(self) -> str:
        return f"whisper {self.model_size}"

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise MissingDependency("Speech recognition", "voice", "faster-whisper") from exc
        try:
            self._model = WhisperModel(self.model_size, device="auto", compute_type="int8")
        except Exception:  # some builds reject int8; fall back to the default
            self._model = WhisperModel(self.model_size)
        return self._model

    def transcribe(self, samples: Any) -> str:
        if samples is None or len(samples) == 0:
            return ""
        segments, _info = self._load().transcribe(samples, beam_size=1, vad_filter=True)
        return " ".join(segment.text.strip() for segment in segments).strip()


def load_stt(config: JarvisConfig | None = None, model_size: str = "base.en") -> Transcriber:
    """Pick a transcriber. 'auto' prefers Deepgram when a key is set."""
    backend = (getattr(config, "stt_backend", "auto") or "auto").lower()
    size = getattr(config, "stt_model", model_size) or model_size
    key = os.environ.get("DEEPGRAM_API_KEY", "").strip()

    if backend == "deepgram" and not key:
        raise TranscriptionError(
            "JARVIS_STT_BACKEND=deepgram but DEEPGRAM_API_KEY is not set. "
            "Set the key, or use JARVIS_STT_BACKEND=whisper to run locally."
        )
    if backend in {"auto", "deepgram"} and key:
        return Deepgram(key, model=getattr(config, "deepgram_model", "nova-3") or "nova-3")
    return FasterWhisper(size)
