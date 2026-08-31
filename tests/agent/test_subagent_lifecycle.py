"""Tests for SubagentManager lifecycle — spawn, run, announce, cancel."""

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.hook import AgentHookContext
from nanobot.agent.runner import AgentRunResult
from nanobot.agent.subagent import (
    SubagentManager,
    SubagentStatus,
    _SubagentHook,
)
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ToolPolicyRuleConfig, ToolsConfig
from nanobot.providers.base import LLMProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _manager(tmp_path: Path, **kw) -> SubagentManager:
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    defaults = dict(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        model="test-model",
        max_tool_result_chars=16_000,
    )
    defaults.update(kw)
    return SubagentManager(**defaults)


def _make_hook_context(**overrides) -> AgentHookContext:
    defaults = dict(
        iteration=1,
        tool_calls=[],
        tool_events=[],
        messages=[],
        usage={},
        error=None,
        stop_reason="completed",
        final_content="ok",
    )
    defaults.update(overrides)
    return AgentHookContext(**defaults)


async def _drain_subagent_tasks(sm: SubagentManager) -> None:
    tasks = list(sm._running_tasks.values())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# SubagentStatus defaults
# ---------------------------------------------------------------------------


class TestSubagentStatus:
    def test_defaults(self):
        s = SubagentStatus(
            task_id="abc", label="test", task_description="do stuff",
            started_at=time.monotonic(),
        )
        assert s.phase == "queued"
        assert s.activity == "waiting_for_capacity"
        assert s.iteration == 0
        assert s.tool_events == []
        assert s.usage == {}
        assert s.stop_reason is None
        assert s.error is None


# ---------------------------------------------------------------------------
# set_provider
# ---------------------------------------------------------------------------


class TestSetProvider:
    def test_updates_provider_model_runner(self, tmp_path):
        sm = _manager(tmp_path)
        new_provider = MagicMock(spec=LLMProvider)
        sm.set_provider(new_provider, "new-model")
        assert sm.provider is new_provider
        assert sm.model == "new-model"
        assert sm.runner.provider is new_provider


# ---------------------------------------------------------------------------
# spawn
# ---------------------------------------------------------------------------


class TestSpawn:
    @pytest.mark.asyncio
    async def test_returns_string_with_task_id(self, tmp_path):
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(return_value=AgentRunResult(
            final_content="done", messages=[], stop_reason="completed",
        ))
        result = await sm.spawn("do something")
        assert "started" in result
        assert "id:" in result

    @pytest.mark.asyncio
    async def test_creates_task_in_running_tasks(self, tmp_path):
        sm = _manager(tmp_path)
        block = asyncio.Event()
        async def _slow_run(spec):
            await block.wait()
            return AgentRunResult(final_content="done", messages=[], stop_reason="completed")
        sm.runner.run = _slow_run

        await sm.spawn("task", session_key="s1")
        assert len(sm._running_tasks) == 1

        block.set()
        await _drain_subagent_tasks(sm)
        assert len(sm._running_tasks) == 0

    @pytest.mark.asyncio
    async def test_creates_status(self, tmp_path):
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(return_value=AgentRunResult(
            final_content="done", messages=[], stop_reason="completed",
        ))
        await sm.spawn("my task")
        await _drain_subagent_tasks(sm)
        # Status cleaned up after task completes
        assert len(sm._task_statuses) == 0

    @pytest.mark.asyncio
    async def test_registers_in_session_tasks(self, tmp_path):
        sm = _manager(tmp_path)
        block = asyncio.Event()
        async def _slow_run(spec):
            await block.wait()
            return AgentRunResult(final_content="done", messages=[], stop_reason="completed")
        sm.runner.run = _slow_run

        await sm.spawn("task", session_key="s1")
        assert "s1" in sm._session_tasks
        assert len(sm._session_tasks["s1"]) == 1

        block.set()
        await _drain_subagent_tasks(sm)
        assert "s1" not in sm._session_tasks

    @pytest.mark.asyncio
    async def test_no_session_key_no_registration(self, tmp_path):
        sm = _manager(tmp_path)
        block = asyncio.Event()
        async def _slow_run(spec):
            await block.wait()
            return AgentRunResult(final_content="done", messages=[], stop_reason="completed")
        sm.runner.run = _slow_run

        await sm.spawn("task")
        assert len(sm._session_tasks) == 0

        block.set()
        await _drain_subagent_tasks(sm)

    @pytest.mark.asyncio
    async def test_label_defaults_to_truncated_task(self, tmp_path):
        sm = _manager(tmp_path)
        block = asyncio.Event()
        async def _slow_run(spec):
            await block.wait()
            return AgentRunResult(final_content="done", messages=[], stop_reason="completed")
        sm.runner.run = _slow_run

        long_task = "A" * 50
        await sm.spawn(long_task, session_key="s1")
        status = next(iter(sm._task_statuses.values()))
        assert status.label == long_task[:30] + "..."

        block.set()
        await _drain_subagent_tasks(sm)

    @pytest.mark.asyncio
    async def test_custom_label(self, tmp_path):
        sm = _manager(tmp_path)
        block = asyncio.Event()
        async def _slow_run(spec):
            await block.wait()
            return AgentRunResult(final_content="done", messages=[], stop_reason="completed")
        sm.runner.run = _slow_run

        await sm.spawn("task", label="Custom Label", session_key="s1")
        status = next(iter(sm._task_statuses.values()))
        assert status.label == "Custom Label"

        block.set()
        await _drain_subagent_tasks(sm)

    @pytest.mark.asyncio
    async def test_cleanup_callback_removes_all_entries(self, tmp_path):
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(return_value=AgentRunResult(
            final_content="done", messages=[], stop_reason="completed",
        ))
        await sm.spawn("task", session_key="s1")
        await _drain_subagent_tasks(sm)
        assert len(sm._running_tasks) == 0
        assert len(sm._task_statuses) == 0
        assert len(sm._session_tasks) == 0

    @pytest.mark.asyncio
    async def test_queued_run_uses_spawn_time_model_and_budgets(self, tmp_path):
        sm = _manager(tmp_path, max_concurrent_subagents=1)
        initial_max_iterations = sm.max_iterations
        initial_max_tool_result_chars = sm.max_tool_result_chars
        first_started = asyncio.Event()
        release = asyncio.Event()
        specs = []

        async def capture(spec):
            specs.append(spec)
            if len(specs) == 1:
                first_started.set()
                await release.wait()
            return AgentRunResult(final_content="done", messages=[], stop_reason="completed")

        sm.runner.run = capture
        await sm.spawn("first")
        await asyncio.wait_for(first_started.wait(), timeout=1)
        await sm.spawn("queued")

        sm.model = "changed-model"
        sm.run_model = "changed-run-model"
        sm.max_iterations = 99
        sm.max_tool_result_chars = 99
        release.set()
        await _drain_subagent_tasks(sm)

        assert [spec.model for spec in specs] == ["test-model", "test-model"]
        assert [spec.max_iterations for spec in specs] == [
            initial_max_iterations,
            initial_max_iterations,
        ]
        assert [spec.max_tool_result_chars for spec in specs] == [
            initial_max_tool_result_chars,
            initial_max_tool_result_chars,
        ]

    @pytest.mark.asyncio
    async def test_terminal_status_remains_inspectable(self, tmp_path):
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(return_value=AgentRunResult(
            final_content="done", messages=[], stop_reason="completed",
        ))

        response = await sm.spawn("task")
        task_id = response.split("id: ", 1)[1].split(")", 1)[0]
        await _drain_subagent_tasks(sm)

        status = sm.runtime_statuses()[task_id]
        assert status.phase == "completed"
        assert status.effective_budgets["model"] == "test-model"

    @pytest.mark.asyncio
    async def test_retry_wait_still_holds_execution_capacity(self, tmp_path):
        sm = _manager(tmp_path, max_concurrent_subagents=1)
        waiting = asyncio.Event()
        release = asyncio.Event()

        async def capture(spec):
            await spec.retry_wait_callback("retrying")
            waiting.set()
            await release.wait()
            return AgentRunResult(final_content="done", messages=[], stop_reason="completed")

        sm.runner.run = capture
        await sm.spawn("task")
        await asyncio.wait_for(waiting.wait(), timeout=1)

        assert next(iter(sm._task_statuses.values())).phase == "waiting"
        assert sm.get_executing_count() == 1
        release.set()
        await _drain_subagent_tasks(sm)


# ---------------------------------------------------------------------------
# queue-full structured rejection
# ---------------------------------------------------------------------------


class TestQueueFullRejection:
    @pytest.mark.asyncio
    async def test_full_queue_raises_structured_error(self, tmp_path):
        from nanobot.agent.subagent import QueueFullError

        sm = _manager(
            tmp_path,
            max_concurrent_subagents=1,
            max_queued_subagents=2,
        )
        release = asyncio.Event()

        async def _slow_run(spec):
            await release.wait()
            return AgentRunResult(final_content="done", messages=[], stop_reason="completed")

        sm.runner.run = _slow_run
        await sm.spawn("first", session_key="s1")
        await sm.spawn("second", session_key="s1")
        await sm.spawn("third", session_key="s1")

        with pytest.raises(QueueFullError) as excinfo:
            await sm.spawn("fourth", session_key="s1")

        err = excinfo.value
        assert err.queue_length == 2
        assert err.position == 3  # would occupy the (full) third queue slot
        assert err.capacity == 2
        assert err.would_wait is True
        assert "queue is full" in str(err)
        assert "2/2" in str(err)

        release.set()
        await _drain_subagent_tasks(sm)

    @pytest.mark.asyncio
    async def test_normal_saturation_queues_instead_of_raising(self, tmp_path):
        """Below the queue bound, saturation queues rather than rejecting."""
        sm = _manager(
            tmp_path,
            max_concurrent_subagents=1,
            max_queued_subagents=2,
        )
        release = asyncio.Event()

        async def _slow_run(spec):
            await release.wait()
            return AgentRunResult(final_content="done", messages=[], stop_reason="completed")

        sm.runner.run = _slow_run
        await sm.spawn("running", session_key="s1")
        await sm.spawn("queued-1", session_key="s1")
        await sm.spawn("queued-2", session_key="s1")  # still within the bound
        await asyncio.sleep(0)  # let the first task acquire the execution slot

        assert sm.get_executing_count() == 1
        assert sm.get_queued_count() == 2
        release.set()
        await _drain_subagent_tasks(sm)
        assert sm.get_running_count() == 0


# ---------------------------------------------------------------------------
# _run_subagent
# ---------------------------------------------------------------------------


class TestRunSubagent:
    @pytest.mark.asyncio
    async def test_run_reserves_finalization_and_allows_tool_recovery(self, tmp_path):
        sm = _manager(tmp_path)
        captured = []

        async def capture(spec):
            captured.append(spec)
            return AgentRunResult(
                final_content="done",
                messages=[],
                stop_reason="completed",
            )

        sm.runner.run = capture
        with patch.object(sm, "_announce_result", new_callable=AsyncMock):
            await sm._run_subagent(
                "t1", "do task", "label",
                {"channel": "cli", "chat_id": "direct"},
                SubagentStatus(
                    task_id="t1",
                    label="label",
                    task_description="do task",
                    started_at=time.monotonic(),
                ),
            )

        assert captured[0].fail_on_tool_error is False
        assert captured[0].finalize_on_max_iterations is True

    @pytest.mark.asyncio
    async def test_provider_retry_wait_is_inspectable(self, tmp_path):
        sm = _manager(tmp_path)
        status = SubagentStatus(
            task_id="t1",
            label="label",
            task_description="do task",
            started_at=time.monotonic(),
        )

        async def capture(spec):
            await spec.retry_wait_callback("retrying")
            assert status.phase == "waiting"
            assert status.activity == "provider_retry"
            return AgentRunResult(
                final_content="done",
                messages=[],
                stop_reason="completed",
            )

        sm.runner.run = capture
        with patch.object(sm, "_announce_result", new_callable=AsyncMock):
            await sm._run_subagent(
                "t1", "do task", "label",
                {"channel": "cli", "chat_id": "direct"}, status,
            )

        assert status.phase == "completed"

    @pytest.mark.asyncio
    async def test_model_preset_is_available_to_tool_policy(self, tmp_path):
        tools_config = ToolsConfig(policies=[ToolPolicyRuleConfig(
            id="deny-expensive-preset",
            outcome="deny",
            tool="write_file",
            preset="expensive",
        )])
        sm = _manager(tmp_path, tools_config=tools_config)
        decisions = []

        async def capture(spec):
            tool = spec.tools.get("write_file")
            decisions.append(spec.tools.evaluate_policy(tool, {"path": "result.txt"}))
            return AgentRunResult(final_content="done", messages=[], stop_reason="completed")

        sm.runner.run = capture
        with patch.object(sm, "_announce_result", new_callable=AsyncMock):
            await sm._run_subagent(
                "t1",
                "do task",
                "label",
                {"channel": "cli", "chat_id": "direct"},
                SubagentStatus(
                    task_id="t1",
                    label="label",
                    task_description="do task",
                    started_at=time.monotonic(),
                ),
                model_override="preset-model",
                model_preset="expensive",
            )

        assert decisions[0].outcome == "deny"
        assert decisions[0].rule_id == "deny-expensive-preset"

    @pytest.mark.asyncio
    async def test_successful_run(self, tmp_path):
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(return_value=AgentRunResult(
            final_content="Task done!", messages=[], stop_reason="completed",
        ))
        with patch.object(sm, "_announce_result", new_callable=AsyncMock) as mock_announce:
            await sm._run_subagent(
                "t1", "do task", "label",
                {"channel": "cli", "chat_id": "direct"},
                SubagentStatus(task_id="t1", label="label", task_description="do task", started_at=time.monotonic()),
            )
            mock_announce.assert_called_once()
            assert mock_announce.call_args.args[-2] == "ok"

    @pytest.mark.asyncio
    async def test_tool_error_run(self, tmp_path):
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(return_value=AgentRunResult(
            final_content=None, messages=[], stop_reason="tool_error",
            tool_events=[{"name": "read_file", "status": "error", "detail": "not found"}],
        ))
        status = SubagentStatus(task_id="t1", label="label", task_description="do task", started_at=time.monotonic())
        with patch.object(sm, "_announce_result", new_callable=AsyncMock) as mock_announce:
            await sm._run_subagent(
                "t1", "do task", "label",
                {"channel": "cli", "chat_id": "direct"}, status,
            )
            assert mock_announce.call_args.args[-2] == "error"

    @pytest.mark.asyncio
    async def test_exception_run(self, tmp_path):
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(side_effect=RuntimeError("LLM down"))
        status = SubagentStatus(task_id="t1", label="label", task_description="do task", started_at=time.monotonic())
        with patch.object(sm, "_announce_result", new_callable=AsyncMock) as mock_announce:
            await sm._run_subagent(
                "t1", "do task", "label",
                {"channel": "cli", "chat_id": "direct"}, status,
            )
            assert status.phase == "failed"
            assert "LLM down" in status.error
            assert mock_announce.call_args.args[-2] == "error"

    @pytest.mark.asyncio
    async def test_status_updated_on_success(self, tmp_path):
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(return_value=AgentRunResult(
            final_content="ok", messages=[], stop_reason="completed",
        ))
        status = SubagentStatus(task_id="t1", label="label", task_description="do task", started_at=time.monotonic())
        with patch.object(sm, "_announce_result", new_callable=AsyncMock):
            await sm._run_subagent(
                "t1", "do task", "label",
                {"channel": "cli", "chat_id": "direct"}, status,
            )
            assert status.phase == "completed"
            assert status.stop_reason == "completed"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stop_reason", ["max_iterations", "empty_final_response"])
    async def test_missing_or_budget_exhausted_final_is_incomplete(
        self,
        tmp_path,
        stop_reason,
    ):
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(return_value=AgentRunResult(
            final_content="Task ended without a verified final synthesis.",
            messages=[],
            stop_reason=stop_reason,
        ))
        status = SubagentStatus(
            task_id="t1",
            label="label",
            task_description="do task",
            started_at=time.monotonic(),
        )

        with patch.object(sm, "_announce_result", new_callable=AsyncMock) as announce:
            await sm._run_subagent(
                "t1", "do task", "label",
                {"channel": "cli", "chat_id": "direct"}, status,
            )

        assert status.phase == "incomplete"
        assert status.stop_reason == stop_reason
        assert announce.call_args.args[-2] == "error"
        assert announce.call_args.kwargs["stop_reason"] == stop_reason
        record = json.loads((tmp_path / "subagents" / "t1.json").read_text(encoding="utf-8"))
        assert record["phase"] == "incomplete"
        assert record["stop_reason"] == stop_reason

    @pytest.mark.asyncio
    async def test_budget_exhausted_but_finalized_settles_completed(self, tmp_path):
        """A run that hit max_iterations but finalized with a real answer settles
        as completed: phase, announcement, and persisted record all agree."""
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(return_value=AgentRunResult(
            final_content="Final synthesis after exhausting the budget.",
            messages=[],
            stop_reason="completed",
        ))
        status = SubagentStatus(
            task_id="t1",
            label="label",
            task_description="do task",
            started_at=time.monotonic(),
        )

        with patch.object(sm, "_announce_result", new_callable=AsyncMock) as announce:
            await sm._run_subagent(
                "t1", "do task", "label",
                {"channel": "cli", "chat_id": "direct"}, status,
            )

        assert status.phase == "completed"
        assert status.stop_reason == "completed"
        assert announce.call_args.args[-2] == "ok"
        assert announce.call_args.kwargs["stop_reason"] == "completed"
        record = json.loads((tmp_path / "subagents" / "t1.json").read_text(encoding="utf-8"))
        assert record["phase"] == "completed"
        assert record["stop_reason"] == "completed"
        assert record["result"] == "Final synthesis after exhausting the budget."


# ---------------------------------------------------------------------------
# _announce_result
# ---------------------------------------------------------------------------


class TestAnnounceResult:
    @pytest.mark.asyncio
    async def test_publishes_inbound_message(self, tmp_path):
        sm = _manager(tmp_path)
        published = []
        sm.bus.publish_inbound = AsyncMock(side_effect=lambda msg: published.append(msg))

        await sm._announce_result(
            "t1", "label", "task", "result text",
            {"channel": "cli", "chat_id": "direct"}, "ok",
        )

        assert len(published) == 1
        msg = published[0]
        assert msg.channel == "system"
        assert msg.sender_id == "subagent"
        assert msg.metadata["injected_event"] == "subagent_result"
        assert msg.metadata["subagent_task_id"] == "t1"
        assert msg.metadata["delivery_policy"] == "parent"
        assert msg.metadata["subagent_result"] == {
            "task_id": "t1",
            "status": "ok",
            "stop_reason": None,
            "result": "result text",
            "record_path": str(sm.records_dir / "t1.json"),
        }

    @pytest.mark.asyncio
    async def test_session_key_override(self, tmp_path):
        sm = _manager(tmp_path)
        published = []
        sm.bus.publish_inbound = AsyncMock(side_effect=lambda msg: published.append(msg))

        await sm._announce_result(
            "t1", "label", "task", "result",
            {"channel": "telegram", "chat_id": "123", "session_key": "s1"}, "ok",
        )

        assert published[0].session_key_override == "s1"

    @pytest.mark.asyncio
    async def test_session_key_override_fallback(self, tmp_path):
        sm = _manager(tmp_path)
        published = []
        sm.bus.publish_inbound = AsyncMock(side_effect=lambda msg: published.append(msg))

        await sm._announce_result(
            "t1", "label", "task", "result",
            {"channel": "telegram", "chat_id": "123"}, "ok",
        )

        assert published[0].session_key_override == "telegram:123"

    @pytest.mark.asyncio
    async def test_ok_status_text(self, tmp_path):
        sm = _manager(tmp_path)
        published = []
        sm.bus.publish_inbound = AsyncMock(side_effect=lambda msg: published.append(msg))

        await sm._announce_result(
            "t1", "label", "task", "result",
            {"channel": "cli", "chat_id": "direct"}, "ok",
        )

        assert "completed successfully" in published[0].content

    @pytest.mark.asyncio
    async def test_error_status_text(self, tmp_path):
        sm = _manager(tmp_path)
        published = []
        sm.bus.publish_inbound = AsyncMock(side_effect=lambda msg: published.append(msg))

        await sm._announce_result(
            "t1", "label", "task", "error details",
            {"channel": "cli", "chat_id": "direct"}, "error",
        )

        assert "failed" in published[0].content

    @pytest.mark.asyncio
    async def test_origin_message_id_in_metadata(self, tmp_path):
        sm = _manager(tmp_path)
        published = []
        sm.bus.publish_inbound = AsyncMock(side_effect=lambda msg: published.append(msg))

        await sm._announce_result(
            "t1", "label", "task", "result",
            {"channel": "cli", "chat_id": "direct"}, "ok",
            origin_message_id="msg-123",
        )

        assert published[0].metadata["origin_message_id"] == "msg-123"


# ---------------------------------------------------------------------------
# _format_partial_progress
# ---------------------------------------------------------------------------


class TestFormatPartialProgress:
    def _make_result(self, tool_events=None, error=None):
        return MagicMock(tool_events=tool_events or [], error=error)

    def test_completed_only(self):
        result = self._make_result(tool_events=[
            {"name": "read_file", "status": "ok", "detail": "file content"},
            {"name": "exec", "status": "ok", "detail": "output"},
        ])
        text = SubagentManager._format_partial_progress(result)
        assert "Completed steps:" in text
        assert "read_file" in text
        assert "exec" in text

    def test_failure_only(self):
        result = self._make_result(tool_events=[
            {"name": "read_file", "status": "error", "detail": "not found"},
        ])
        text = SubagentManager._format_partial_progress(result)
        assert "Failure:" in text
        assert "not found" in text

    def test_completed_and_failure(self):
        result = self._make_result(tool_events=[
            {"name": "read_file", "status": "ok", "detail": "content"},
            {"name": "exec", "status": "error", "detail": "timeout"},
        ])
        text = SubagentManager._format_partial_progress(result)
        assert "Completed steps:" in text
        assert "Failure:" in text

    def test_limited_to_last_three(self):
        result = self._make_result(tool_events=[
            {"name": f"tool_{i}", "status": "ok", "detail": f"result_{i}"}
            for i in range(5)
        ])
        text = SubagentManager._format_partial_progress(result)
        assert "tool_2" in text
        assert "tool_3" in text
        assert "tool_4" in text
        assert "tool_0" not in text
        assert "tool_1" not in text

    def test_error_without_failure_event(self):
        result = self._make_result(
            tool_events=[{"name": "read_file", "status": "ok", "detail": "ok"}],
            error="Something went wrong",
        )
        text = SubagentManager._format_partial_progress(result)
        assert "Something went wrong" in text

    def test_empty_events_with_error(self):
        result = self._make_result(error="Total failure")
        text = SubagentManager._format_partial_progress(result)
        assert "Total failure" in text

    def test_empty_no_error_returns_fallback(self):
        result = self._make_result()
        text = SubagentManager._format_partial_progress(result)
        assert "Error" in text


# ---------------------------------------------------------------------------
# cancel_by_session
# ---------------------------------------------------------------------------


class TestCancelBySession:
    @pytest.mark.asyncio
    async def test_cancels_running_tasks(self, tmp_path):
        sm = _manager(tmp_path)
        block = asyncio.Event()
        async def _slow_run(spec):
            await block.wait()
            return AgentRunResult(final_content="done", messages=[], stop_reason="completed")
        sm.runner.run = _slow_run

        await sm.spawn("task1", session_key="s1")
        await sm.spawn("task2", session_key="s1")
        assert len(sm._session_tasks.get("s1", set())) == 2

        count = await sm.cancel_by_session("s1")
        assert count == 2
        block.set()
        await _drain_subagent_tasks(sm)

    @pytest.mark.asyncio
    async def test_no_tasks_returns_zero(self, tmp_path):
        sm = _manager(tmp_path)
        count = await sm.cancel_by_session("nonexistent")
        assert count == 0

    @pytest.mark.asyncio
    async def test_already_done_not_counted(self, tmp_path):
        sm = _manager(tmp_path)
        sm.runner.run = AsyncMock(return_value=AgentRunResult(
            final_content="done", messages=[], stop_reason="completed",
        ))
        await sm.spawn("task1", session_key="s1")
        await _drain_subagent_tasks(sm)

        count = await sm.cancel_by_session("s1")
        assert count == 0

    @pytest.mark.asyncio
    async def test_queued_and_running_cancellation_persist_terminal_state(self, tmp_path):
        sm = _manager(
            tmp_path,
            max_concurrent_subagents=1,
            max_queued_subagents=1,
        )
        release = asyncio.Event()

        async def _slow_run(spec):
            await release.wait()
            return AgentRunResult(final_content="done", messages=[], stop_reason="completed")

        sm.runner.run = _slow_run
        sm._announce_result = AsyncMock()
        # Snapshot phases at call time: the status object is mutated in place
        # afterwards, so inspecting the captured args later would show the
        # terminal phase for every call.
        phases: list[str] = []

        def _capture(_tid, _task, _label, _origin, _temp, _scope, _result, status, **_kw):
            phases.append(status.phase)

        with patch.object(sm, "_write_run_record", side_effect=_capture):
            await sm.spawn("running", session_key="s1")
            await sm.spawn("queued", session_key="s1")
            await asyncio.sleep(0)

            assert sm.get_executing_count() == 1
            assert sm.get_queued_count() == 1
            assert await sm.cancel_by_session("s1") == 2

        # Durable records exist from spawn time (queued/running) and are
        # overwritten with terminal state; cancellation lands as cancelled.
        assert len(phases) == 4  # 2 spawn-time + 2 terminal
        assert sorted(phases) == ["cancelled", "cancelled", "queued", "running"]


# ---------------------------------------------------------------------------
# get_running_count / get_running_count_by_session
# ---------------------------------------------------------------------------


class TestRunningCounts:
    @pytest.mark.asyncio
    async def test_running_count_zero(self, tmp_path):
        sm = _manager(tmp_path)
        assert sm.get_running_count() == 0

    @pytest.mark.asyncio
    async def test_running_count_tracks_tasks(self, tmp_path):
        sm = _manager(tmp_path)
        block = asyncio.Event()
        async def _slow_run(spec):
            await block.wait()
            return AgentRunResult(final_content="done", messages=[], stop_reason="completed")
        sm.runner.run = _slow_run

        await sm.spawn("t1", session_key="s1")
        await sm.spawn("t2", session_key="s1")
        assert sm.get_running_count() == 2
        assert sm.get_running_count_by_session("s1") == 2

        block.set()
        await _drain_subagent_tasks(sm)
        assert sm.get_running_count() == 0

    @pytest.mark.asyncio
    async def test_running_count_by_session_nonexistent(self, tmp_path):
        sm = _manager(tmp_path)
        assert sm.get_running_count_by_session("nonexistent") == 0


# ---------------------------------------------------------------------------
# _SubagentHook
# ---------------------------------------------------------------------------


class TestSubagentHook:
    @pytest.mark.asyncio
    async def test_before_execute_tools_logs(self, tmp_path):
        hook = _SubagentHook("t1")
        tool_call = MagicMock()
        tool_call.name = "read_file"
        tool_call.arguments = {"path": "/tmp/test"}
        ctx = _make_hook_context(tool_calls=[tool_call])
        result = await hook.before_execute_tools(ctx)
        assert result is None
        assert ctx.tool_calls == [tool_call]

    @pytest.mark.asyncio
    async def test_after_iteration_updates_status(self):
        status = SubagentStatus(
            task_id="t1", label="test", task_description="do", started_at=time.monotonic(),
        )
        hook = _SubagentHook("t1", status)
        ctx = _make_hook_context(
            iteration=3,
            tool_events=[{"name": "read_file", "status": "ok", "detail": ""}],
            usage={"prompt_tokens": 100},
        )
        await hook.after_iteration(ctx)
        assert status.iteration == 3
        assert len(status.tool_events) == 1
        assert status.usage == {"prompt_tokens": 100}

    @pytest.mark.asyncio
    async def test_after_iteration_no_status_noop(self):
        hook = _SubagentHook("t1", status=None)
        ctx = _make_hook_context(iteration=5)
        result = await hook.after_iteration(ctx)
        assert result is None
        assert ctx.iteration == 5

    @pytest.mark.asyncio
    async def test_after_iteration_sets_error(self):
        status = SubagentStatus(
            task_id="t1", label="test", task_description="do", started_at=time.monotonic(),
        )
        hook = _SubagentHook("t1", status)
        ctx = _make_hook_context(error="something broke")
        await hook.after_iteration(ctx)
        assert status.error == "something broke"
