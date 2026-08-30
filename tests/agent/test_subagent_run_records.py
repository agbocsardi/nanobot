"""Subagent run records (Phase 3 observability).

One record per subagent run lands under <workspace>/subagents/{task_id}.json,
covering all outcomes (success / tool_error / error / exception). Same writer
+ schema as cron run records; `kind` distinguishes them. Captures the model
that ran, token usage, iterations, and tool events — everything previously
lost when a subagent finished.
"""

import asyncio
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
from nanobot.config.schema import ContextRetrievalConfig
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
    async def test_partial_outcome_is_persisted_and_not_announced_as_success(self, tmp_path):
        sm = _manager(tmp_path)
        events = [{
            "name": "exec",
            "status": "retryable_error",
            "detail": "not found",
            "execution_succeeded": True,
            "operational_success": False,
            "verified": False,
            "retryable": True,
            "postcondition": None,
            "exit_code": 127,
        }]
        sm.runner.run = AsyncMock(return_value=AgentRunResult(
            final_content="Incomplete: command failed.",
            messages=[],
            stop_reason="partial_completion",
            tool_events=events,
        ))
        status = _status()

        with patch.object(sm, "_announce_result", new_callable=AsyncMock) as announce:
            await sm._run_subagent(
                "t1", "do task", "label",
                {"channel": "cli", "chat_id": "direct"}, status,
            )

        assert announce.call_args.args[-2] == "error"
        rec = _read_record(tmp_path, "t1")
        assert rec["stop_reason"] == "partial_completion"
        assert rec["tool_events"] == events
        assert rec["result"] == "Incomplete: command failed."

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
        assert rec["phase"] == "failed"
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
    async def test_record_uses_effective_per_spawn_budgets(self, tmp_path):
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(return_value=AgentRunResult(
            final_content="ok", messages=[], stop_reason="completed",
        ))

        with patch.object(sm, "_announce_result", new_callable=AsyncMock):
            await sm._run_subagent(
                "t1", "do task", "label",
                {"channel": "cli", "chat_id": "direct"},
                _status(),
                max_iterations=7,
                context_window_tokens=32_000,
                model_preset="careful",
            )

        rec = _read_record(tmp_path, "t1")
        assert rec["params"]["max_iterations"] == 7
        assert rec["params"]["context_window_tokens"] == 32_000
        assert rec["params"]["max_tool_result_chars"] == 16_000
        assert rec["params"]["model_preset"] == "careful"
        assert rec["model_preset"] == "careful"

    @pytest.mark.asyncio
    async def test_record_persists_per_spawn_max_tokens_override(self, tmp_path):
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(return_value=AgentRunResult(
            final_content="ok", messages=[], stop_reason="completed",
        ))

        with patch.object(sm, "_announce_result", new_callable=AsyncMock):
            await sm._run_subagent(
                "t1", "do task", "label",
                {"channel": "cli", "chat_id": "direct"},
                _status(),
                max_tokens=4096,
            )

        rec = _read_record(tmp_path, "t1")
        assert rec["params"]["max_tokens"] == 4096

    @pytest.mark.asyncio
    async def test_spawn_max_tokens_override_appears_in_record_exactly(self, tmp_path):
        """max_tokens passed through spawn() must land verbatim in the record."""
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(return_value=AgentRunResult(
            final_content="done", messages=[], stop_reason="completed",
        ))
        response = await sm.spawn("do task", session_key="cli:record", max_tokens=2048)
        task_id = response.split("id: ", 1)[1].split(")", 1)[0]
        await asyncio.sleep(0)
        await asyncio.gather(*sm._running_tasks.values(), return_exceptions=True)

        rec = _read_record(tmp_path, task_id)
        assert rec["params"]["max_tokens"] == 2048

    @pytest.mark.asyncio
    async def test_budget_exhausted_but_finalized_record_shows_completed(self, tmp_path):
        """A run whose finalization produced a final answer persists as completed."""
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(return_value=AgentRunResult(
            final_content="Final synthesis.",
            messages=[],
            stop_reason="completed",
        ))

        with patch.object(sm, "_announce_result", new_callable=AsyncMock):
            await sm._run_subagent(
                "t1", "do task", "label",
                {"channel": "cli", "chat_id": "direct"}, _status(),
            )

        rec = _read_record(tmp_path, "t1")
        assert rec["stop_reason"] == "completed"
        assert rec["phase"] == "completed"
        assert rec["result"] == "Final synthesis."

    @pytest.mark.asyncio
    async def test_record_persists_task_relevant_context_decisions(self, tmp_path):
        (tmp_path / "rules.md").write_text("nanobot repository rules", encoding="utf-8")
        (tmp_path / "context-manifest.json").write_text(
            '{"retrieved":[{"path":"rules.md","owners":["repo:nanobot"]}]}',
            encoding="utf-8",
        )
        sm = _manager(
            tmp_path,
            context_retrieval=ContextRetrievalConfig(mode="manifest"),
        )
        sm.runner.run = AsyncMock(return_value=AgentRunResult(
            final_content="ok", messages=[], stop_reason="completed",
        ))

        with patch.object(sm, "_announce_result", new_callable=AsyncMock):
            await sm._run_subagent(
                "t1", "fix the nanobot repository", "label",
                {"channel": "cli", "chat_id": "direct"},
                _status(),
            )

        rec = _read_record(tmp_path, "t1")
        assert rec["context"]["mode"] == "manifest"
        selected = [
            source["path"]
            for source in rec["context"]["sources"]
            if source["selected"]
        ]
        assert selected == ["rules.md"]

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

    @pytest.mark.asyncio
    async def test_spawned_cancellation_persists_real_terminal_record(self, tmp_path):
        sm = _manager(tmp_path)
        started = asyncio.Event()

        async def block(_spec):
            started.set()
            await asyncio.sleep(60)

        sm.runner.run = block
        response = await sm.spawn("cancel me", session_key="cli:cancel")
        task_id = response.split("id: ", 1)[1].split(")", 1)[0]
        await asyncio.wait_for(started.wait(), timeout=1)

        assert await sm.cancel_by_session("cli:cancel") == 1
        await asyncio.sleep(0)

        record = _read_record(tmp_path, task_id)
        assert record["phase"] == "cancelled"
        assert record["stop_reason"] == "cancelled"
        assert record["result"] == "Task was cancelled before completion."
        assert sm.runtime_statuses()[task_id].phase == "cancelled"

    @pytest.mark.asyncio
    async def test_immediate_cancellation_awaits_fallback_announcement(self, tmp_path):
        sm = _manager(tmp_path)
        response = await sm.spawn("cancel before start", session_key="cli:immediate")
        task_id = response.split("id: ", 1)[1].split(")", 1)[0]

        assert await sm.cancel_by_session("cli:immediate") == 1

        announcement = await asyncio.wait_for(sm.bus.consume_inbound(), timeout=1)
        record = _read_record(tmp_path, task_id)
        assert announcement.metadata["subagent_result"]["stop_reason"] == "cancelled"
        assert record["phase"] == "cancelled"
        assert sm._finalizer_tasks == set()
