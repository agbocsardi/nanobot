"""Command-surface tests for /tasks and /task (issue #27)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.bus.events import InboundMessage
from nanobot.command.builtin import (
    BUILTIN_COMMAND_SPECS,
    cmd_task,
    cmd_tasks,
    register_builtin_commands,
)
from nanobot.command.router import CommandContext, CommandRouter


def _record(task_id: str, *, session_key: str = "telegram:1", sender: str | None = "u1",
            phase: str = "completed", stop_reason: str = "completed", label: str = "L",
            result: str = "final result") -> dict:
    return {
        "task_id": task_id,
        "label": label,
        "task": "prompt",
        "origin": {"channel": "telegram", "chat_id": "1", "session_key": session_key,
                   "sender_id": sender},
        "params": {"max_iterations": 5, "max_tokens": 256, "model_preset": "fast"},
        "phase": phase,
        "stop_reason": stop_reason,
        "activity": "",
        "iterations": 3,
        "error": None,
        "result": result,
        "created_at_ms": 1000,
        "updated_at_ms": 2000,
    }


def _subagents(records: list[dict], *, read_extra: dict | None = None):
    by_id = {r["task_id"]: dict(r) for r in records}
    if read_extra:
        by_id.update(read_extra)
    def owned_record(task_id, *, session_key, sender_id=None):
        record = by_id.get(task_id)
        if record is None:
            return None, "not_found"
        origin = record.get("origin") or {}
        if origin.get("session_key") != session_key:
            return None, "not_owned"
        rec_sender = origin.get("sender_id")
        if sender_id is not None and rec_sender and str(rec_sender) != str(sender_id):
            return None, "not_owned"
        return record, None

    return SimpleNamespace(
        list_session_task_records=MagicMock(return_value=records),
        read_run_record=MagicMock(side_effect=lambda tid: by_id.get(tid)),
        owned_record=MagicMock(side_effect=owned_record),
        cancel_task=AsyncMock(return_value="cancelled"),
        retry_task=AsyncMock(return_value=("created", "new9ab")),
        task_status_vocabulary=__import__(
            "nanobot.agent.subagent", fromlist=["SubagentManager"]
        ).SubagentManager.task_status_vocabulary,
    )


def _ctx(loop, *, raw="/tasks", args="", sender="u1") -> CommandContext:
    msg = InboundMessage(channel="telegram", sender_id=sender, chat_id="1", content=raw)
    return CommandContext(msg=msg, session=None, key=msg.session_key, raw=raw, args=args, loop=loop)


@pytest.mark.asyncio
async def test_tasks_lists_unified_newest_first() -> None:
    # Live tasks now have durable records from spawn time, so /tasks is one
    # flat newest-first list; the record order from the manager is preserved.
    subagents = _subagents([
        _record("t1", phase="queued", stop_reason=None, label="waiting job"),
        _record("t2", phase="completed", stop_reason="completed", label="done job"),
        _record("t3", phase="running", stop_reason=None, label="live job"),
    ])
    out = await cmd_tasks(_ctx(SimpleNamespace(subagents=subagents)))

    assert "## Tasks" in out.content
    assert "t1" in out.content and "t2" in out.content and "t3" in out.content
    assert out.content.index("t1") < out.content.index("t2") < out.content.index("t3")
    assert "recent:" not in out.content


@pytest.mark.asyncio
async def test_tasks_empty_and_unavailable() -> None:
    out = await cmd_tasks(_ctx(SimpleNamespace(subagents=SimpleNamespace(
        list_session_task_records=MagicMock(return_value=[]),
    ))))
    assert "No background tasks" in out.content

    out2 = await cmd_tasks(_ctx(SimpleNamespace()))
    assert "not available" in out2.content


@pytest.mark.asyncio
async def test_task_detail_renders_bounded_summary() -> None:
    record = _record("t9", result="x" * 5000)
    subagents = _subagents([record])
    out = await cmd_task(_ctx(SimpleNamespace(subagents=subagents), raw="/task t9", args="t9"))

    assert "task: t9" in out.content
    assert "status: completed" in out.content
    assert "budgets: iterations=5 max_tokens=256 preset=fast" in out.content
    assert "x" * 100 in out.content
    assert "x" * 5000 not in out.content  # redacted/truncated


@pytest.mark.asyncio
async def test_task_cross_session_returns_generic_not_found() -> None:
    record = _record("t9", session_key="telegram:OTHER")
    subagents = _subagents([record])
    out = await cmd_task(_ctx(SimpleNamespace(subagents=subagents), raw="/task t9", args="t9"))
    assert "not found" in out.content


@pytest.mark.asyncio
async def test_task_stop_and_retry_route_through_manager() -> None:
    subagents = _subagents([_record("t5")])
    stop = await cmd_task(_ctx(SimpleNamespace(subagents=subagents), args="stop t5"))
    assert "Cancelled task t5." in stop.content
    subagents.cancel_task.assert_awaited_once_with("t5", session_key="telegram:1")

    retry = await cmd_task(_ctx(SimpleNamespace(subagents=subagents), args="retry t5"))
    assert "Retried t5 as new9ab" in retry.content
    subagents.retry_task.assert_awaited_once()


@pytest.mark.asyncio
async def test_task_result_returns_bounded_terminal_output() -> None:
    subagents = _subagents([_record("t6", result="the answer is 42")])
    out = await cmd_task(_ctx(SimpleNamespace(subagents=subagents), args="result t6"))
    assert "the answer is 42" in out.content


@pytest.mark.asyncio
async def test_commands_registered_and_dispatchable() -> None:
    specs = {spec.command: spec for spec in BUILTIN_COMMAND_SPECS}
    assert "/tasks" in specs
    assert "/task" in specs

    router = CommandRouter()
    register_builtin_commands(router)
    subagents = _subagents([])
    ctx = _ctx(SimpleNamespace(subagents=subagents), raw="/tasks", args="")
    out = await router.dispatch(ctx)
    assert out is not None
    assert "No background tasks" in out.content
