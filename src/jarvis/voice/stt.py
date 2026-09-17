"""Speech to text."""

from __future__ import annotations

from typing import Any, Protocol

from ..errors import MissingDependency


class Transcriber(Protocol):
    def transcribe(self, samples: Any) -> str: ...


class FasterWhisper:
    """Local Whisper via faster-whisper. The model loads once, on first use."""

    def __init__(self, model_size: str = "base.en") -> None:
        self.model_size = model_size
        self._model: Any = None

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


def load_stt(model_size: str = "base.en") -> Transcriber:
    return FasterWhisper(model_size)
