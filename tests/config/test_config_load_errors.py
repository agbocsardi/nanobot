import json

import pytest

from nanobot.config.loader import load_config


def test_load_config_missing_file_uses_defaults(tmp_path) -> None:
    config = load_config(tmp_path / "missing.json")

    assert config.agents.defaults.model


def test_load_config_legacy_api_block_is_ignored(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"api": {"host": "127.0.0.1", "port": 8765, "timeout": 30}}),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.agents.defaults.model


def test_load_config_invalid_json_fails_fast(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{broken json", encoding="utf-8")

    with pytest.raises(ValueError, match="Failed to load config"):
        load_config(config_path)


def test_load_config_invalid_schema_fails_fast(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"tools": {"exec": {"timeout": -1}}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Failed to load config"):
        load_config(config_path)
