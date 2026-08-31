"""Config watcher: handler behavior and gateway wiring."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from nanobot.agent.tools.runtime_inspector import fingerprint_file
from nanobot.cli import commands
from nanobot.cli.commands import _run_gateway, build_config_change_handler
from nanobot.config.loader import load_config, set_config_path


def _write_config(path: Path, *, model_preset: str | None = None, presets: dict | None = None, extra: dict | None = None) -> None:
    data: dict = {"agents": {"defaults": {"workspace": "/tmp/nanobot-test"}}}
    if model_preset is not None:
        data["agents"]["defaults"]["model_preset"] = model_preset
    if presets is not None:
        data["modelPresets"] = presets
    if extra:
        data.update(extra)
    path.write_text(json.dumps(data), encoding="utf-8")


class _Recorder:
    """Stand-in for the loguru logger capturing calls made by the handler."""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.infos: list[str] = []
        self.exceptions: list[str] = []

    def warning(self, fmt: str, *args) -> None:
        self.warnings.append(fmt.format(*args) if args else fmt)

    def info(self, fmt: str, *args) -> None:
        self.infos.append(fmt.format(*args) if args else fmt)

    def exception(self, fmt: str, *args) -> None:
        self.exceptions.append(fmt.format(*args) if args else fmt)


class _FakeAgent:
    """Minimal AgentLoop stand-in recording preset switches."""

    def __init__(self, presets: dict, current_preset: str | None, fingerprint: str | None = "old-fp") -> None:
        self.model_presets = dict(presets)
        self._preset = current_preset
        self.set_calls: list[str] = []
        self.loaded_config_fingerprint = fingerprint
        self.warning_messages: list[str] = []

    @property
    def model_preset(self) -> str | None:
        return self._preset

    def set_model_preset(self, name: str, *, publish_update: bool = True) -> None:
        self.set_calls.append(name)
        self._preset = name


_PRESETS = {
    "fast": {"model": "fast-model", "provider": "custom"},
    "slow": {"model": "slow-model", "provider": "custom"},
}
_LOOP_PRESETS = {**_PRESETS, "default": {"model": "main-model", "provider": "custom"}}


class TestConfigChangeHandler:

    def test_model_preset_change_is_hot_applied(self, tmp_path: Path, monkeypatch) -> None:
        config_path = tmp_path / "config.json"
        _write_config(config_path, model_preset="fast", presets=_PRESETS)
        recorder = _Recorder()
        monkeypatch.setattr(commands, "logger", recorder)
        agent = _FakeAgent(_LOOP_PRESETS, current_preset="fast")

        _write_config(config_path, model_preset="slow", presets=_PRESETS)
        build_config_change_handler(agent, config_path)()

        assert agent.set_calls == ["slow"]
        assert agent.model_preset == "slow"
        assert agent.loaded_config_fingerprint == fingerprint_file(config_path)
        assert not recorder.warnings

    def test_removed_preset_falls_back_to_default(self, tmp_path: Path, monkeypatch) -> None:
        config_path = tmp_path / "config.json"
        _write_config(config_path, model_preset="fast", presets=_PRESETS)
        recorder = _Recorder()
        monkeypatch.setattr(commands, "logger", recorder)
        agent = _FakeAgent(_LOOP_PRESETS, current_preset="fast")

        _write_config(config_path, presets=_PRESETS)
        build_config_change_handler(agent, config_path)()

        assert agent.set_calls == ["default"]
        assert agent.loaded_config_fingerprint == fingerprint_file(config_path)

    def test_unknown_preset_warns_and_keeps_drift(self, tmp_path: Path, monkeypatch) -> None:
        config_path = tmp_path / "config.json"
        _write_config(config_path, model_preset="fast", presets=_PRESETS)
        recorder = _Recorder()
        monkeypatch.setattr(commands, "logger", recorder)
        agent = _FakeAgent(_LOOP_PRESETS, current_preset="fast")

        # The file gains a brand-new preset and activates it; the running
        # loop's preset set is frozen at startup, so the name is unknown there.
        _write_config(
            config_path,
            model_preset="brand-new",
            presets={**_PRESETS, "brand-new": {"model": "new-model", "provider": "custom"}},
        )
        build_config_change_handler(agent, config_path)()

        assert agent.set_calls == []
        assert agent.loaded_config_fingerprint == "old-fp"
        assert any("manual restart" in w for w in recorder.warnings)

    def test_provider_only_change_warns_manual_restart(self, tmp_path: Path, monkeypatch) -> None:
        config_path = tmp_path / "config.json"
        _write_config(config_path, model_preset="fast", presets=_PRESETS)
        recorder = _Recorder()
        monkeypatch.setattr(commands, "logger", recorder)
        agent = _FakeAgent(_LOOP_PRESETS, current_preset="fast")

        _write_config(
            config_path,
            model_preset="fast",
            presets=_PRESETS,
            extra={"providers": {"custom": {"apiKey": "NEW-KEY"}}},
        )
        build_config_change_handler(agent, config_path)()

        assert agent.set_calls == []
        assert agent.loaded_config_fingerprint == "old-fp"
        assert any("manual restart" in w for w in recorder.warnings)

    def test_invalid_config_after_change_does_not_crash(self, tmp_path: Path, monkeypatch) -> None:
        config_path = tmp_path / "config.json"
        _write_config(config_path, model_preset="fast", presets=_PRESETS)
        recorder = _Recorder()
        monkeypatch.setattr(commands, "logger", recorder)
        agent = _FakeAgent(_LOOP_PRESETS, current_preset="fast")

        config_path.write_text("{broken json", encoding="utf-8")
        build_config_change_handler(agent, config_path)()  # must not raise

        assert agent.set_calls == []
        assert agent.loaded_config_fingerprint == "old-fp"
        assert recorder.exceptions


class _FakeLoop:
    instances: list["_FakeLoop"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.model = kwargs.get("model", "test-model")
        self.provider = object()
        self.tools: dict = {}
        self.sessions = MagicMock()
        self.sessions.flush_all.return_value = 0

    @classmethod
    def from_config(cls, config, bus, **extra) -> "_FakeLoop":
        agent = cls(**extra)
        cls.instances.append(agent)
        return agent

    async def run(self) -> None:
        return None

    async def close_mcp(self) -> None:
        return None

    def stop(self) -> None:
        return None


class _FakeChannels:
    def __init__(self, *args, **kwargs) -> None:
        self.enabled_channels: list[str] = []

    def get_channel(self, _name):
        return None

    async def start_all(self) -> None:
        return None

    async def stop_all(self) -> None:
        return None


class _FakeCron:
    def __init__(self, *args, **kwargs) -> None:
        self.on_job = None
        self.jobs = []

    async def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def register_system_job(self, job) -> None:
        self.jobs.append(job)

    def status(self) -> dict:
        return {"jobs": 0}


def _fake_provider_snapshot(config, **kwargs):
    provider = MagicMock()
    return SimpleNamespace(
        provider=provider,
        model="test-model",
        context_window_tokens=1000,
        signature=("test",),
    )


class TestGatewayWiring:

    def test_gateway_starts_config_watcher_with_handler(self, tmp_path: Path, monkeypatch) -> None:
        config_path = tmp_path / "config.json"
        _write_config(config_path, model_preset="fast", presets=_PRESETS)
        set_config_path(config_path)
        config = load_config(config_path)
        config.agents.defaults.workspace = str(tmp_path / "ws")

        seen: dict = {}
        sentinel_handler = lambda: None  # noqa: E731

        async def fake_watch(config_path, on_change):
            seen["config_path"] = config_path
            seen["handler"] = on_change

        _FakeLoop.instances.clear()
        with patch("nanobot.cli.commands.AgentLoop", _FakeLoop), \
             patch("nanobot.channels.manager.ChannelManager", _FakeChannels), \
             patch("nanobot.cron.service.CronService", _FakeCron), \
             patch("nanobot.cli.commands.sync_workspace_templates"), \
             patch("nanobot.cli.commands.build_config_change_handler", return_value=sentinel_handler) as build_handler, \
             patch("nanobot.providers.factory.build_provider_snapshot", side_effect=_fake_provider_snapshot), \
             patch("nanobot.providers.factory.load_provider_snapshot", side_effect=_fake_provider_snapshot), \
             patch("nanobot.config.watcher.watch_config_file", side_effect=fake_watch):
            result = {}

            def _run() -> None:
                try:
                    _run_gateway(config, port=19999, health_server_enabled=False)
                    result["ok"] = True
                except BaseException as exc:  # pragma: no cover - failure probe
                    result["error"] = exc

            thread = threading.Thread(target=_run)
            thread.start()
            thread.join(timeout=15)
            assert not thread.is_alive(), "gateway run did not finish"
            assert "error" not in result, f"gateway crashed: {result.get('error')!r}"

        assert seen["config_path"] == config_path
        assert seen["handler"] is sentinel_handler
        assert len(_FakeLoop.instances) == 1
        build_handler.assert_called_once()
        assert build_handler.call_args.args[0] is _FakeLoop.instances[0]
        assert build_handler.call_args.args[1] == config_path
