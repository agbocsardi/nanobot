"""Deterministic regressions: on-disk config that drifts from the live/loaded
snapshot must be reported restart-required, never promoted as current.

Incident-derived: a runtime kept running against a stale in-memory config
while operators assumed the on-disk change was live; the inspector surface
(MyTool action=status, the existing used surface) must surface the drift.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.runtime_inspector import RuntimeInspector, render_snapshot
from nanobot.agent.tools.self import MyTool

_ORIGINAL_CONFIG = '{"agents":{"defaults":{}}}'
_DRIFTED_CONFIG = '{"agents":{"defaults":{"maxToolIterations": 9}}}'

_DETACHED_REPOSITORY = {"available": False}


def _runtime(tmp_path: Path, *, drift: bool) -> SimpleNamespace:
    """Fake AgentLoop-like runtime: loaded config fingerprint pinned at boot."""
    config_path = tmp_path / "config.json"
    config_path.write_text(_ORIGINAL_CONFIG, encoding="utf-8")
    loaded_fingerprint = hashlib.sha256(config_path.read_bytes()).hexdigest()
    if drift:
        # The file changes after the runtime loaded it (the drift incident).
        config_path.write_text(_DRIFTED_CONFIG, encoding="utf-8")
    provider = SimpleNamespace(generation=SimpleNamespace(max_tokens=4096))
    return SimpleNamespace(
        provider=provider,
        model="fixture-model",
        model_preset=None,
        context_window_tokens=128_000,
        max_iterations=40,
        max_tool_result_chars=16_000,
        provider_retry_mode="standard",
        workspace=tmp_path,
        tools=SimpleNamespace(tool_names=["exec", "my"]),
        context=SimpleNamespace(skills=None),
        exec_config=SimpleNamespace(allowed_env_keys=["PATH"]),
        sessions=SimpleNamespace(_cache={}),
        subagents=None,
        cron_service=None,
        _loaded_config_path=config_path,
        _loaded_config_fingerprint=loaded_fingerprint,
    )


def test_config_drift_is_reported_restart_required_not_current(tmp_path) -> None:
    runtime = _runtime(tmp_path, drift=True)

    with patch.object(RuntimeInspector, "_repository", return_value=_DETACHED_REPOSITORY):
        snapshot = RuntimeInspector(runtime).snapshot()

    config = snapshot["config"]
    assert config["available"] is True
    assert config["drift_status"] == "changed"
    assert config["restart_required"] is True
    assert config["loaded_fingerprint"] != config["on_disk_fingerprint"]
    rendered = render_snapshot(snapshot)
    assert '"drift_status": "changed"' in rendered
    assert '"restart_required": true' in rendered
    assert '"drift_status": "current"' not in rendered


def test_unchanged_config_stays_current(tmp_path) -> None:
    runtime = _runtime(tmp_path, drift=False)

    with patch.object(RuntimeInspector, "_repository", return_value=_DETACHED_REPOSITORY):
        snapshot = RuntimeInspector(runtime).snapshot()

    assert snapshot["config"]["drift_status"] == "current"
    assert snapshot["config"]["restart_required"] is False
    assert snapshot["config"]["loaded_fingerprint"] == snapshot["config"]["on_disk_fingerprint"]


@pytest.mark.asyncio
async def test_my_status_surface_does_not_promote_drifted_config_as_current(tmp_path) -> None:
    runtime = _runtime(tmp_path, drift=True)
    tool = MyTool(runtime_state=runtime)
    tool.set_context(RequestContext(
        channel="cli",
        chat_id="direct",
        session_key="cli:direct",
    ))

    with patch.object(RuntimeInspector, "_repository", return_value=_DETACHED_REPOSITORY):
        result = await tool.execute(action="status")

    # The used surface (MyTool action=status) is the inspector's consumer; it
    # must carry the drift in both its structured data and its rendered text.
    assert isinstance(result, ToolResult)
    assert result.status == "success"
    assert result.data["config"]["drift_status"] == "changed"
    assert result.data["config"]["restart_required"] is True
    assert '"drift_status": "changed"' in str(result)
    assert '"restart_required": true' in str(result)
    assert '"drift_status": "current"' not in str(result)
