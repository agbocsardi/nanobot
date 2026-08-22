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
        ("remote", "get-url", "origin"): "git@github.com:agbocsardi/nanobot.git",
    }

    with patch.object(
        RuntimeInspector,
        "_git",
        side_effect=lambda _cwd, *args: git_values.get(args),
    ):
        snapshot = RuntimeInspector(runtime).snapshot(session_key="telegram:1")

    assert set(snapshot) == {
        "version",
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
        "origin": "git@github.com:agbocsardi/nanobot.git",
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


def test_snapshot_uses_live_goal_delegated_and_cron_state(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime.sessions._cache["telegram:1"] = SimpleNamespace(metadata={
        "goal_state": {"status": "active", "objective": "Finish the migration"},
    })
    status = SimpleNamespace(
        label="worker",
        phase="running",
        iteration=3,
        stop_reason=None,
        error=None,
    )
    runtime.subagents = SimpleNamespace(
        runtime_statuses=lambda: {"task-1": status},
        get_queued_count=lambda: 1,
        max_iterations=20,
        max_concurrent_subagents=2,
        max_queued_subagents=8,
        max_tool_result_chars=8000,
    )
    recent = SimpleNamespace(
        run_at_ms=100,
        status="error",
        duration_ms=25,
        error="delivery failed",
    )
    job = SimpleNamespace(
        id="job-1",
        name="daily",
        enabled=True,
        state=SimpleNamespace(
            last_status="error",
            last_error="delivery failed",
            last_run_at_ms=100,
            next_run_at_ms=200,
            run_history=[recent],
        ),
    )
    runtime.cron_service = SimpleNamespace(list_jobs=lambda: [job])

    with patch.object(RuntimeInspector, "_repository", return_value={"available": False}):
        snapshot = RuntimeInspector(runtime).snapshot(session_key="telegram:1")

    assert snapshot["session"]["active_goal"] == {
        "active": True,
        "objective": "Finish the migration",
    }
    delegated = snapshot["delegated_work"]
    assert delegated["runs"][0]["phase"] == "running"
    assert delegated["runs"][0]["effective_budgets"]["available"] is False
    assert delegated["manager_budgets"]["max_concurrent"] == 2
    assert delegated["queue"] == {"available": True, "queued": 1, "capacity": 8}
    cron = snapshot["cron"]["jobs"][0]
    assert cron["recent_terminal"]["status"] == "error"
    assert cron["delivery"]["available"] is False


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
