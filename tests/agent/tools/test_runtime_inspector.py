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
        "telegram",
        "delegated_work",
        "cron",
        "environment",
        "repository",
        "capabilities",
    }
    assert snapshot["telegram"] == {
        "available": False,
        "reason": "telegram channel not active",
    }
    assert snapshot["runtime"]["budgets"]["max_iterations"] == 40
    assert snapshot["config"]["drift_status"] == "current"
    assert snapshot["config"]["restart_required"] is False
    assert snapshot["delegated_work"]["available"] is False
    assert snapshot["delegated_work"]["queue"]["available"] is False
    assert snapshot["cron"] == {"available": False, "jobs": [], "errors": []}
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
        "workspace": {
            "available": True,
            "path": str(tmp_path),
            "branch": "feat/status",
            "commit": "abc123",
            "dirty": True,
            "upstream": "origin/main",
            "origin": "git@github.com:agbocsardi/nanobot.git",
            "ahead": 3,
            "behind": 2,
        },
    }
    assert snapshot["environment"]["allowed_names"] == ["HOME", "PATH"]
    assert snapshot["capabilities"] == {
        "tools": ["exec", "my"],
        "skills": ["github", "memory"],
        "context": {
            "mode": "all_pinned",
            "constitutional_budget_chars": None,
            "current_budget_chars": None,
            "retrieved_budget_chars": None,
        },
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
        get_executing_count=lambda: 2,
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
    assert delegated["queue"] == {
        "available": True,
        "queued": 1,
        "capacity": 8,
        "available_slots": 7,
    }
    assert delegated["execution_capacity"] == {
        "available": True,
        "in_use": 2,
        "capacity": 2,
        "available_slots": 0,
    }
    cron = snapshot["cron"]["jobs"][0]
    assert cron["recent_terminal"]["status"] == "error"
    assert cron["delivery"] == {"available": True, "status": None, "error": None}


def _telegram_channel_with_observations(entries, *, total_seen, limit=100):
    return SimpleNamespace(
        reply_context_observations=lambda: {
            "total_seen": total_seen,
            "limit": limit,
            "entries": entries,
        }
    )


def test_snapshot_telegram_diagnostics_renders_when_channel_active(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    entries = [
        {
            "ts": 100.0,
            "chat_id": "-100123",
            "message_id": 1,
            "has_reply_source": False,
            "reply_to_message_id": None,
            "reply_id_present": False,
            "replied_to_bot": None,
            "context_attached": False,
            "text_len": 0,
            "caption_len": 0,
            "quote_len": 0,
            "media_count": 0,
            "media_file_id_present": False,
            "content_unavailable": False,
        },
        {
            "ts": 200.0,
            "chat_id": "-100123",
            "message_id": 2,
            "has_reply_source": True,
            "reply_to_message_id": 42,
            "reply_id_present": True,
            "replied_to_bot": False,
            "context_attached": True,
            "text_len": 11,
            "caption_len": 0,
            "quote_len": 7,
            "media_count": 1,
            "media_file_id_present": True,
            "content_unavailable": False,
        },
    ]
    runtime.channel_manager = SimpleNamespace(
        channels={
            "telegram": _telegram_channel_with_observations(
                entries, total_seen=2
            )
        }
    )

    snapshot = RuntimeInspector(runtime).snapshot()

    telegram = snapshot["telegram"]
    assert telegram["available"] is True
    assert telegram["buffer_entries"] == 2
    assert telegram["buffer_limit"] == 100
    assert telegram["replies_seen"] == 1
    assert telegram["replies_seen_total"] == 2
    assert telegram["last_reply"] == {
        "ts": 200.0,
        "chat_id": "-100123",
        "message_id": 2,
        "reply_to_message_id": 42,
        "reply_id_present": True,
        "replied_to_bot": False,
        "has_reply_source": True,
        "context_attached": True,
        "text_len": 11,
        "caption_len": 0,
        "quote_len": 7,
        "media_count": 1,
        "media_file_id_present": True,
        "content_unavailable": False,
    }


def test_snapshot_telegram_diagnostics_unavailable_without_accessor(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime.channel_manager = SimpleNamespace(
        channels={"telegram": SimpleNamespace(name="telegram")}
    )

    snapshot = RuntimeInspector(runtime).snapshot()

    assert snapshot["telegram"] == {
        "available": False,
        "reason": "telegram channel lacks observations accessor",
    }


def test_snapshot_telegram_diagnostics_empty_buffer_reports_no_last_reply(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime.channel_manager = SimpleNamespace(
        channels={
            "telegram": _telegram_channel_with_observations([], total_seen=0)
        }
    )

    snapshot = RuntimeInspector(runtime).snapshot()

    telegram = snapshot["telegram"]
    assert telegram["available"] is True
    assert telegram["buffer_entries"] == 0
    assert telegram["replies_seen"] == 0
    assert telegram["replies_seen_total"] == 0
    assert telegram["last_reply"] is None


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
