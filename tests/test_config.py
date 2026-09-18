"""Configuration layering and validation."""

from __future__ import annotations

import os

import pytest

from jarvis.config import JarvisConfig
from jarvis.errors import ConfigError
from jarvis.prompts import system_blocks


def test_defaults(tmp_path):
    config = JarvisConfig(workspace=tmp_path, data_dir=tmp_path / "data")
    assert config.model == "claude-opus-5"
    assert config.worker_model == "claude-opus-5"
    assert config.mongodb_uri == "mongodb://localhost:27017"
    assert config.mongodb_db == "jarvis"


def test_mongodb_uri_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "mongodb+srv://u:p@cluster0.abc.mongodb.net/")
    config = JarvisConfig.load(config_file=tmp_path / "none.toml")
    assert config.mongodb_uri.startswith("mongodb+srv://")

    # the JARVIS_ prefixed name wins when both are set
    monkeypatch.setenv("JARVIS_MONGODB_URI", "mongodb://elsewhere:27017")
    assert JarvisConfig.load(config_file=tmp_path / "none.toml").mongodb_uri == (
        "mongodb://elsewhere:27017"
    )


def test_connection_strings_are_redacted_before_printing():
    from jarvis.memory import redact_uri

    redacted = redact_uri("mongodb+srv://isa:hunter2@cluster0.abc.mongodb.net/jarvis")
    assert "hunter2" not in redacted
    assert "isa" in redacted and "cluster0.abc.mongodb.net" in redacted


def test_environment_overrides_the_file(tmp_path, monkeypatch):
    config_file = tmp_path / "config.toml"
    config_file.write_text('[jarvis]\neffort = "medium"\nmax_tokens = 1000\n')
    monkeypatch.setenv("JARVIS_EFFORT", "xhigh")

    config = JarvisConfig.load(config_file=config_file)
    assert config.effort == "xhigh"      # environment wins
    assert config.max_tokens == 1000     # file still applies


def test_arguments_override_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_EFFORT", "xhigh")
    assert JarvisConfig.load(config_file=tmp_path / "none.toml", effort="max").effort == "max"


def test_unknown_option_in_the_file_is_an_error(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text('[jarvis]\nmagic = true\n')
    with pytest.raises(ConfigError, match="unknown option"):
        JarvisConfig.load(config_file=config_file)


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"effort": "turbo"}, "effort must be one of"),
        ({"approval": "maybe"}, "approval must be one of"),
        ({"max_iterations": 0}, "max_iterations"),
    ],
)
def test_impossible_values_are_rejected(kwargs, message):
    with pytest.raises(ConfigError, match=message):
        JarvisConfig(**kwargs)


def test_workspace_is_resolved_absolutely(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert JarvisConfig(workspace=".").workspace == tmp_path.resolve()


def test_system_prompt_splits_stable_from_volatile(config, store):
    store.remember("name", "Isa")
    blocks = system_blocks(config, store, "cli", "Isa")

    assert len(blocks) == 2
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "Isa" in blocks[0]["text"]
    assert "name: Isa" in blocks[0]["text"]
    assert "Current time" not in blocks[0]["text"]   # the clock must not break the cache
    assert "Current time" in blocks[1]["text"]
    assert "cache_control" not in blocks[1]


def test_voice_prompt_asks_for_speakable_answers(config):
    assert "spoken aloud" in system_blocks(config, None, "voice")[0]["text"]
    assert "spoken aloud" not in system_blocks(config, None, "cli")[0]["text"]


# --- .env ------------------------------------------------------------
def test_a_dotenv_file_is_actually_loaded(tmp_path, monkeypatch):
    """The repo ships .env.example and git-ignores .env, and the README says to
    copy it. For a long time nothing read the result."""
    from jarvis.config import load_env_file

    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        "MONGODB_URI=mongodb+srv://user:pw@cluster0.abc.mongodb.net/\n"
        "export ANTHROPIC_API_KEY=sk-ant-exported\n"
        'JARVIS_QUIET_HOURS="23:00-06:30"\n'
        "JARVIS_MODEL=claude-opus-5    # trailing comment\n"
    )
    for name in ("MONGODB_URI", "ANTHROPIC_API_KEY", "JARVIS_QUIET_HOURS", "JARVIS_MODEL"):
        monkeypatch.delenv(name, raising=False)

    applied = load_env_file(env)

    assert set(applied) == {
        "MONGODB_URI", "ANTHROPIC_API_KEY", "JARVIS_QUIET_HOURS", "JARVIS_MODEL",
    }
    assert os.environ["MONGODB_URI"].endswith("mongodb.net/")
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-exported"   # `export ` stripped
    assert os.environ["JARVIS_QUIET_HOURS"] == "23:00-06:30"      # quotes stripped
    assert os.environ["JARVIS_MODEL"] == "claude-opus-5"          # trailing comment dropped


def test_a_real_export_beats_the_file(tmp_path, monkeypatch):
    from jarvis.config import load_env_file

    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
    env = tmp_path / ".env"
    env.write_text("MONGODB_URI=mongodb+srv://from-the-file@example.net/\n")

    assert load_env_file(env) == []
    assert os.environ["MONGODB_URI"] == "mongodb://localhost:27017"


def test_quoted_values_keep_characters_that_would_otherwise_be_eaten(tmp_path, monkeypatch):
    # A generated password containing '#' is the realistic case here.
    from jarvis.config import load_env_file

    monkeypatch.delenv("MONGODB_PASSWORD", raising=False)
    env = tmp_path / ".env"
    env.write_text('MONGODB_PASSWORD="pa#ss word"\n')

    load_env_file(env)
    assert os.environ["MONGODB_PASSWORD"] == "pa#ss word"


def test_a_missing_or_unreadable_dotenv_is_not_an_error(tmp_path):
    from jarvis.config import load_dotenv, load_env_file

    assert load_env_file(tmp_path / "nope.env") == []
    assert load_dotenv(tmp_path / "nope.env") is None
