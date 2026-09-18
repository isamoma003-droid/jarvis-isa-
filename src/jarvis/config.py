"""Configuration: defaults, then ~/.jarvis/config.toml, then environment."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .memory import DEFAULT_DB, DEFAULT_URI

# The model everything runs on unless told otherwise.
DEFAULT_MODEL = "claude-opus-5"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
APPROVAL_POLICIES = ("auto", "prompt", "deny")
VOICE_INPUTS = ("press", "wake")
STT_BACKENDS = ("auto", "deepgram", "whisper")


def credentials_available() -> bool:
    """Whether the SDK will find a credential.

    An unset ANTHROPIC_API_KEY does not mean there is nothing: the SDK also
    reads ANTHROPIC_AUTH_TOKEN, an `ant auth login` profile, and workload
    identity federation.
    """
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    if (Path.home() / ".config" / "anthropic").exists():
        return True
    federation = (
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
        "ANTHROPIC_SERVICE_ACCOUNT_ID",
    )
    has_token = os.environ.get("ANTHROPIC_IDENTITY_TOKEN_FILE") or os.environ.get(
        "ANTHROPIC_IDENTITY_TOKEN"
    )
    return bool(has_token) and all(os.environ.get(name) for name in federation)


NO_CREDENTIALS = (
    "No Anthropic credentials found. Either export ANTHROPIC_API_KEY, or run "
    "`ant auth login` to store a profile. Run `jarvis doctor` to check the setup."
)


def load_env_file(path: Path) -> list[str]:
    """Read a .env file into the environment. Returns the names it set.

    Deliberately not a dependency: the realistic file is `KEY=value` lines with
    optional quotes, `export` prefixes and comments, and that is thirty lines.

    A variable already in the environment always wins, so an explicit
    `export MONGODB_URI=...` overrides the file rather than being silently
    ignored - the surprising direction is the other way round.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    applied: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, sep, value = line.partition("=")
        if not sep:
            continue
        name = name.strip()
        if not name or name in os.environ:
            continue
        value = value.strip()
        # Strip one matching pair of quotes; anything inside them is literal,
        # which is what saves passwords containing # or spaces.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #")[0].rstrip()
        os.environ[name] = value
        applied.append(name)
    return applied


def load_dotenv(explicit: Path | None = None) -> Path | None:
    """Load the first .env we find. Returns which one, or None.

    Looks beside the working directory first so a checkout carries its own
    settings, then in the data directory so a laptop-wide one works from
    anywhere.
    """
    candidates = [explicit] if explicit else [
        Path.cwd() / ".env",
        Path.home() / ".jarvis" / ".env",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            load_env_file(candidate)
            return candidate
    return None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass
class JarvisConfig:
    """Everything tunable, in one place."""

    # --- model ---
    model: str = DEFAULT_MODEL
    subagent_model: str = ""  # empty means "same as model"
    max_tokens: int = 64000
    effort: str = "high"
    subagent_effort: str = "low"
    thinking: bool = True
    thinking_display: str = "summarized"
    refusal_fallbacks: bool = True
    web_tools: bool = True

    # --- where things live ---
    workspace: Path = field(default_factory=Path.cwd)
    data_dir: Path = field(default_factory=lambda: Path.home() / ".jarvis")
    mongodb_uri: str = DEFAULT_URI
    mongodb_db: str = DEFAULT_DB

    # --- safety ---
    approval: str = "prompt"
    shell_timeout: int = 60
    max_tool_output: int = 20000
    max_depth: int = 2
    max_iterations: int = 40
    # Outward-facing actions ask every time, whatever `approval` says. Turning
    # this off is a deliberate choice, not a default anyone drifts into.
    confirm_outward: bool = True
    audit: bool = True

    # --- reminders ---
    reminder_poll_seconds: int = 20

    # --- the heartbeat ---
    heartbeat_seconds: int = 60
    quiet_hours: str = "22:00-07:00"
    checks: list[dict[str, Any]] = field(default_factory=list)

    # --- voice ---
    wake_word: str = "jarvis"
    # How you start a turn. "press" opens the mic on a keypress and closes it
    # when you stop speaking; "wake" is the open-mic loop. A terminal cannot
    # see a key being released, so there is no true hold-to-talk here.
    voice_input: str = "press"
    stt_backend: str = "auto"          # auto | deepgram | whisper
    stt_model: str = "base.en"         # the whisper size
    deepgram_model: str = "nova-3"
    tts_backend: str = "auto"          # auto | elevenlabs | pyttsx3 | say | espeak | print
    tts_voice: str = ""                # an ElevenLabs voice id
    tts_model: str = "eleven_turbo_v2_5"
    voice_silence_seconds: float = 1.2
    voice_max_seconds: float = 30.0

    # --- web ---
    web_host: str = "127.0.0.1"
    web_port: int = 8765

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).expanduser().resolve()
        self.data_dir = Path(self.data_dir).expanduser()
        if self.effort not in EFFORT_LEVELS:
            raise ConfigError(f"effort must be one of {EFFORT_LEVELS}, got {self.effort!r}")
        if self.subagent_effort not in EFFORT_LEVELS:
            raise ConfigError(
                f"subagent_effort must be one of {EFFORT_LEVELS}, got {self.subagent_effort!r}"
            )
        if self.approval not in APPROVAL_POLICIES:
            raise ConfigError(
                f"approval must be one of {APPROVAL_POLICIES}, got {self.approval!r}"
            )
        if self.max_depth < 0:
            raise ConfigError("max_depth cannot be negative")
        if self.max_iterations < 1:
            raise ConfigError("max_iterations must be at least 1")
        if self.heartbeat_seconds < 1:
            raise ConfigError("heartbeat_seconds must be at least 1")
        if self.voice_input not in VOICE_INPUTS:
            raise ConfigError(
                f"voice_input must be one of {VOICE_INPUTS}, got {self.voice_input!r}"
            )
        if self.stt_backend not in STT_BACKENDS:
            raise ConfigError(
                f"stt_backend must be one of {STT_BACKENDS}, got {self.stt_backend!r}"
            )
        # Validate the heartbeat settings here, at load, rather than letting a
        # typo in the config file surface as a dead background thread hours later.
        from .heartbeat import CheckError, load_checks, parse_quiet_hours

        try:
            parse_quiet_hours(self.quiet_hours)
            load_checks(self.checks)
        except CheckError as exc:
            raise ConfigError(str(exc)) from exc

    @property
    def worker_model(self) -> str:
        """The model sub-agents run on - the main one unless overridden."""
        return self.subagent_model or self.model

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, config_file: Path | None = None, **overrides: object) -> JarvisConfig:
        """Defaults, overlaid with the TOML file, then environment, then kwargs."""
        values: dict[str, object] = {}
        known = {f.name for f in fields(cls)}

        path = config_file or (Path.home() / ".jarvis" / "config.toml")
        if path.is_file():
            with path.open("rb") as handle:
                parsed = tomllib.load(handle)
            section = parsed.get("jarvis", parsed)
            for key, value in section.items():
                if key in known:
                    values[key] = value
                else:
                    raise ConfigError(f"unknown option {key!r} in {path}")

        # MONGODB_URI is the conventional name; the JARVIS_ prefixed one wins.
        for name in ("MONGODB_URI", "JARVIS_MONGODB_URI"):
            if os.environ.get(name):
                values["mongodb_uri"] = os.environ[name]

        env_map = {
            "mongodb_db": "JARVIS_MONGODB_DB",
            "model": "JARVIS_MODEL",
            "subagent_model": "JARVIS_SUBAGENT_MODEL",
            "effort": "JARVIS_EFFORT",
            "subagent_effort": "JARVIS_SUBAGENT_EFFORT",
            "thinking_display": "JARVIS_THINKING_DISPLAY",
            "approval": "JARVIS_APPROVAL",
            "wake_word": "JARVIS_WAKE_WORD",
            "stt_model": "JARVIS_STT_MODEL",
            "tts_backend": "JARVIS_TTS_BACKEND",
            "web_host": "JARVIS_WEB_HOST",
            "quiet_hours": "JARVIS_QUIET_HOURS",
            "voice_input": "JARVIS_VOICE_INPUT",
            "stt_backend": "JARVIS_STT_BACKEND",
            "deepgram_model": "JARVIS_DEEPGRAM_MODEL",
            "tts_voice": "JARVIS_TTS_VOICE",
            "tts_model": "JARVIS_TTS_MODEL",
        }
        for key, env_name in env_map.items():
            raw = os.environ.get(env_name)
            if raw:
                values[key] = raw

        for key, env_name in (
            ("workspace", "JARVIS_WORKSPACE"),
            ("data_dir", "JARVIS_DATA_DIR"),
        ):
            raw = os.environ.get(env_name)
            if raw:
                values[key] = Path(raw).expanduser()

        int_map = {
            "max_tokens": "JARVIS_MAX_TOKENS",
            "shell_timeout": "JARVIS_SHELL_TIMEOUT",
            "max_tool_output": "JARVIS_MAX_TOOL_OUTPUT",
            "max_depth": "JARVIS_MAX_DEPTH",
            "max_iterations": "JARVIS_MAX_ITERATIONS",
            "reminder_poll_seconds": "JARVIS_REMINDER_POLL_SECONDS",
            "heartbeat_seconds": "JARVIS_HEARTBEAT_SECONDS",
            "web_port": "JARVIS_WEB_PORT",
        }
        for key, env_name in int_map.items():
            if env_name in os.environ:
                values[key] = _env_int(env_name, 0)

        bool_map = {
            "thinking": "JARVIS_THINKING",
            "refusal_fallbacks": "JARVIS_REFUSAL_FALLBACKS",
            "web_tools": "JARVIS_WEB_TOOLS",
            "audit": "JARVIS_AUDIT",
            "confirm_outward": "JARVIS_CONFIRM_OUTWARD",
        }
        for key, env_name in bool_map.items():
            if env_name in os.environ:
                values[key] = _env_bool(env_name, True)

        values.update({k: v for k, v in overrides.items() if v is not None})

        unknown = set(values) - known
        if unknown:
            raise ConfigError(f"unknown configuration keys: {sorted(unknown)}")
        return cls(**values)  # type: ignore[arg-type]
