"""Configuration layering and validation."""

from __future__ import annotations

import pytest

from jarvis.config import JarvisConfig
from jarvis.errors import ConfigError
from jarvis.prompts import system_blocks


def test_defaults(tmp_path):
    config = JarvisConfig(workspace=tmp_path, data_dir=tmp_path / "data")
    assert config.model == "claude-opus-5"
    assert config.worker_model == "claude-opus-5"
    assert config.db_path == tmp_path / "data" / "jarvis.db"


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
