"""Subagent run records (Phase 3 observability).

One record per subagent run lands under <workspace>/subagents/{task_id}.json,
covering all outcomes (success / tool_error / error / exception). Same writer
+ schema as cron run records; `kind` distinguishes them. Captures the model
that ran, token usage, iterations, and tool events — everything previously
lost when a subagent finished.
"""

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from nanobot.agent.hook import AgentHookContext
from nanobot.agent.runner import AgentRunResult
from nanobot.agent.subagent import (
    SubagentManager,
    SubagentStatus,
)
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider


def _manager(tmp_path: Path, **kw) -> SubagentManager:
    provider = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "deepseek-v4-pro"
    defaults = dict(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        model="deepseek-v4-pro",
        max_tool_result_chars=16_000,
    )
    defaults.update(kw)
    return SubagentManager(**defaults)


def _status(task_id: str = "t1") -> SubagentStatus:
    return SubagentStatus(
        task_id=task_id,
        label="label",
        task_description="do task",
        started_at=time.monotonic(),
    )


def _read_record(tmp_path: Path, task_id: str) -> dict:
    path = tmp_path / "subagents" / f"{task_id}.json"
    assert path.exists(), f"no record at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


class TestSubagentRunRecord:
    @pytest.mark.asyncio
    async def test_success_records_model_usage_and_result(self, tmp_path):
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(return_value=AgentRunResult(
            final_content="Task done!",
            messages=[],
            stop_reason="completed",
            usage={"prompt_tokens": 500, "completion_tokens": 80},
        ))
        with patch.object(sm, "_announce_result", new_callable=AsyncMock):
            await sm._run_subagent(
                "t1", "do task", "label",
                {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"},
                _status(),
            )

        rec = _read_record(tmp_path, "t1")
        assert rec["kind"] == "subagent"
        assert rec["model"] == "deepseek-v4-pro"
        assert rec["result"] == "Task done!"
        assert rec["usage"]["prompt_tokens"] == 500
        assert rec["usage"]["completion_tokens"] == 80
        assert rec["usage"]["model"] == "deepseek-v4-pro"

    @pytest.mark.asyncio
    async def test_tool_error_records_tool_events(self, tmp_path):
        sm = _manager(tmp_path)
        events = [{"name": "read_file", "status": "error", "detail": "not found"}]
        sm.runner.run = AsyncMock(return_value=AgentRunResult(
            final_content=None,
            messages=[],
            stop_reason="tool_error",
            tool_events=events,
        ))
        status = _status()
        with patch.object(sm, "_announce_result", new_callable=AsyncMock):
            await sm._run_subagent(
                "t1", "do task", "label",
                {"channel": "cli", "chat_id": "direct"}, status,
            )

        rec = _read_record(tmp_path, "t1")
        assert rec["stop_reason"] == "tool_error"
        assert rec["tool_events"] == events

    @pytest.mark.asyncio
    async def test_exception_records_error(self, tmp_path):
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(side_effect=RuntimeError("LLM down"))
        status = _status()
        with patch.object(sm, "_announce_result", new_callable=AsyncMock):
            await sm._run_subagent(
                "t1", "do task", "label",
                {"channel": "cli", "chat_id": "direct"}, status,
            )

        rec = _read_record(tmp_path, "t1")
        assert rec["phase"] == "error"
        assert "LLM down" in rec["error"]
        assert "LLM down" in rec["result"]

    @pytest.mark.asyncio
    async def test_record_captures_iterations_and_params(self, tmp_path):
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(return_value=AgentRunResult(
            final_content="ok", messages=[], stop_reason="completed",
        ))

        async def _drain_hook(status: SubagentStatus):
            # Simulate the runner's hook firing with iteration progress.
            ctx = AgentHookContext(
                iteration=4, tool_calls=[], tool_events=[],
                messages=[], usage={}, error=None,
                stop_reason="completed", final_content="ok",
            )
            from nanobot.agent.subagent import _SubagentHook
            hook = _SubagentHook("t1", status)
            await hook.after_iteration(ctx)

        status = _status()
        await _drain_hook(status)
        with patch.object(sm, "_announce_result", new_callable=AsyncMock):
            await sm._run_subagent(
                "t1", "do task", "label",
                {"channel": "cli", "chat_id": "direct"}, status,
                temperature=0.4,
            )

        rec = _read_record(tmp_path, "t1")
        assert rec["iterations"] == 4
        assert rec["params"]["temperature"] == 0.4
        assert rec["params"]["max_iterations"] == sm.max_iterations

    @pytest.mark.asyncio
    async def test_record_write_failure_does_not_break_run(self, tmp_path):
        """Observability must never break the announce path."""
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(return_value=AgentRunResult(
            final_content="ok", messages=[], stop_reason="completed",
        ))
        with patch(
            "nanobot.agent.subagent.write_run_record",
            side_effect=OSError("disk full"),
        ), patch.object(sm, "_announce_result", new_callable=AsyncMock) as announce:
            await sm._run_subagent(
                "t1", "do task", "label",
                {"channel": "cli", "chat_id": "direct"}, _status(),
            )
            announce.assert_called_once()  # announce still happened
