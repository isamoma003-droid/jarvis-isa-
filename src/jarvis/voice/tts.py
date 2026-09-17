"""Text to speech, and making text worth speaking."""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import threading
from typing import Any, Protocol


class Speaker(Protocol):
    def say(self, text: str) -> None: ...
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


class CommandSpeaker:
    """A system speech binary: macOS `say`, `espeak-ng`, or `spd-say`."""

    def __init__(self, command: list[str], name: str) -> None:
        self.command = command
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def say(self, text: str) -> None:
        if not text.strip():
            return
        try:
            subprocess.run([*self.command, text], check=False, timeout=180)
        except (OSError, subprocess.TimeoutExpired):
            PrintSpeaker().say(text)


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


def load_tts(backend: str = "auto") -> Speaker:
    """Pick a speech backend. 'auto' takes the best one available here."""
    if backend == "print":
        return PrintSpeaker()
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
