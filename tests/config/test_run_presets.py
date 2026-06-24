"""Run preset routing for background work.

One abstraction chooses model presets for chat/subagent/cron/dream. These tests
pin the contract before wiring callers.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nanobot.agent.model_presets import build_run_provider_snapshot, resolve_run_preset_name
from nanobot.config.schema import Config


def _config(**defaults) -> Config:
    return Config(
        agents={"defaults": defaults},
        modelPresets={
            "cheap": {"model": "cheap-model", "provider": "custom"},
            "memory": {"model": "memory-model", "provider": "custom"},
        },
    )


def test_run_presets_accept_known_preset_names() -> None:
    cfg = _config(runPresets={"subagent": "cheap", "cron": "cheap", "dream": "memory"})
    assert cfg.agents.defaults.run_presets == {
        "subagent": "cheap",
        "cron": "cheap",
        "dream": "memory",
    }


def test_run_presets_reject_unknown_preset_name() -> None:
    with pytest.raises(ValueError, match="unknown model_preset 'missing'"):
        _config(runPresets={"cron": "missing"})


def test_resolve_run_preset_prefers_explicit_override() -> None:
    cfg = _config(modelPreset="cheap", runPresets={"dream": "memory"})
    assert resolve_run_preset_name(cfg, "dream", override="default") == "default"


def test_resolve_run_preset_uses_kind_specific_mapping() -> None:
    cfg = _config(modelPreset="cheap", runPresets={"dream": "memory"})
    assert resolve_run_preset_name(cfg, "dream") == "memory"


def test_resolve_run_preset_falls_back_to_active_preset() -> None:
    cfg = _config(modelPreset="cheap")
    assert resolve_run_preset_name(cfg, "subagent") == "cheap"


def test_resolve_run_preset_falls_back_to_default() -> None:
    cfg = _config()
    assert resolve_run_preset_name(cfg, "cron") == "default"


def test_build_run_provider_snapshot_uses_kind_preset() -> None:
    cfg = _config(runPresets={"cron": "cheap"})
    provider = MagicMock()
    with patch("nanobot.agent.model_presets.build_provider_snapshot") as build:
        build.return_value = provider
        assert build_run_provider_snapshot(cfg, "cron") is provider
        build.assert_called_once_with(cfg, preset_name="cheap")


def test_build_run_provider_snapshot_keeps_legacy_raw_dream_model_override() -> None:
    cfg = _config()
    provider = MagicMock()
    with patch("nanobot.agent.model_presets.make_provider", return_value=provider) as make:
        snap = build_run_provider_snapshot(
            cfg,
            "dream",
            override="raw-model-id",
            allow_raw_model=True,
        )
    assert snap.provider is provider
    assert snap.model == "raw-model-id"
    make.assert_called_once()
