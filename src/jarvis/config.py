"""Configuration: defaults, then ~/.jarvis/config.toml, then environment."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .errors import ConfigError

# The model everything runs on unless told otherwise.
DEFAULT_MODEL = "claude-opus-5"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
APPROVAL_POLICIES = ("auto", "prompt", "deny")


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

    # --- safety ---
    approval: str = "prompt"
    shell_timeout: int = 60
    max_tool_output: int = 20000
    max_depth: int = 2
    max_iterations: int = 40

    # --- reminders ---
    reminder_poll_seconds: int = 20

    # --- voice ---
    wake_word: str = "jarvis"
    stt_model: str = "base.en"
    tts_backend: str = "auto"
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

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jarvis.db"

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

        env_map = {
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
            "web_port": "JARVIS_WEB_PORT",
        }
        for key, env_name in int_map.items():
            if env_name in os.environ:
                values[key] = _env_int(env_name, 0)

        bool_map = {
            "thinking": "JARVIS_THINKING",
            "refusal_fallbacks": "JARVIS_REFUSAL_FALLBACKS",
            "web_tools": "JARVIS_WEB_TOOLS",
        }
        for key, env_name in bool_map.items():
            if env_name in os.environ:
                values[key] = _env_bool(env_name, True)

        values.update({k: v for k, v in overrides.items() if v is not None})

        unknown = set(values) - known
        if unknown:
            raise ConfigError(f"unknown configuration keys: {sorted(unknown)}")
        return cls(**values)  # type: ignore[arg-type]
