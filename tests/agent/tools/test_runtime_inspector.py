from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.runtime_inspector import RuntimeInspector
from nanobot.agent.tools.self import MyTool


def _runtime(tmp_path: Path) -> SimpleNamespace:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"agents":{"defaults":{}}}', encoding="utf-8")
    fingerprint = hashlib.sha256(config_path.read_bytes()).hexdigest()
    provider = SimpleNamespace(generation=SimpleNamespace(max_tokens=4096))
    tools = SimpleNamespace(tool_names=["exec", "my"])
    skills = MagicMock()
    skills.list_skills.return_value = [{"name": "github"}, {"name": "memory"}]
    return SimpleNamespace(
        provider=provider,
        model="openai/gpt-test",
        model_preset="coding",
        context_window_tokens=128_000,
        max_iterations=40,
        max_tool_result_chars=16_000,
        provider_retry_mode="standard",
        workspace=tmp_path,
        tools=tools,
        context=SimpleNamespace(skills=skills),
        exec_config=SimpleNamespace(allowed_env_keys=["PATH", "HOME"]),
        sessions=SimpleNamespace(_cache={}),
        subagents=None,
        cron_service=None,
        _loaded_config_path=config_path,
        _loaded_config_fingerprint=fingerprint,
        _runtime_vars={},
        _last_usage={},
        _current_iteration=0,
    )


def test_snapshot_represents_absent_subsystems_and_repository_identity(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    git_values = {
        ("rev-parse", "--show-toplevel"): str(tmp_path),
        ("rev-parse", "--abbrev-ref", "@{upstream}"): "origin/main",
        ("rev-list", "--left-right", "--count", "@{upstream}...HEAD"): "2 3",
        ("status", "--porcelain"): " M file.py",
        ("branch", "--show-current"): "feat/status",
        ("rev-parse", "HEAD"): "abc123",
    }

    with patch.object(
        RuntimeInspector,
        "_git",
        side_effect=lambda _cwd, *args: git_values.get(args),
    ):
        snapshot = RuntimeInspector(runtime).snapshot(session_key="telegram:1")

    assert set(snapshot) == {
        "runtime",
        "config",
        "session",
        "delegated_work",
        "cron",
        "environment",
        "repository",
        "capabilities",
    }
    assert snapshot["runtime"]["budgets"]["max_iterations"] == 40
    assert snapshot["config"]["drift_status"] == "current"
    assert snapshot["config"]["restart_required"] is False
    assert snapshot["delegated_work"]["available"] is False
    assert snapshot["delegated_work"]["queue"]["available"] is False
    assert snapshot["cron"] == {"available": False, "jobs": []}
    assert snapshot["repository"] == {
        "available": True,
        "path": str(tmp_path),
        "branch": "feat/status",
        "commit": "abc123",
        "dirty": True,
        "upstream": "origin/main",
        "ahead": 3,
        "behind": 2,
    }
    assert snapshot["environment"]["allowed_names"] == ["HOME", "PATH"]
    assert snapshot["capabilities"] == {
        "tools": ["exec", "my"],
        "skills": ["github", "memory"],
    }


def test_snapshot_detects_restart_required_config_drift(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime._loaded_config_path.write_text('{"changed":true}', encoding="utf-8")

    snapshot = RuntimeInspector(runtime).snapshot()

    assert snapshot["config"]["drift_status"] == "changed"
    assert snapshot["config"]["restart_required"] is True
    assert set(snapshot["config"]) == {
        "available",
        "path",
        "loaded_fingerprint",
        "on_disk_fingerprint",
        "restart_required",
        "drift_status",
    }


@pytest.mark.asyncio
async def test_my_status_returns_typed_read_only_snapshot(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    tool = MyTool(runtime_state=runtime)
    tool.set_context(RequestContext(
        channel="telegram",
        chat_id="1",
        session_key="telegram:1",
    ))

    with patch.object(RuntimeInspector, "_repository", return_value={"available": False}):
        result = await tool.execute(action="status")

    assert isinstance(result, ToolResult)
    assert result.status == "success"
    assert result.data["session"]["key"] == "telegram:1"
    assert result.data["config"]["loaded_fingerprint"]
    assert result.data["environment"]["allowed_names"] == ["HOME", "PATH"]
