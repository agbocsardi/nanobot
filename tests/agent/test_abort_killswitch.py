"""Tests for the /abort killswitch: cancel tasks, drop queue, end sustained goals."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.bus.events import InboundMessage


def _make_loop(*, goal_state=None):
    """Create a minimal AgentLoop with mocked dependencies and a real session."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    workspace = MagicMock()
    workspace.__truediv__ = MagicMock(return_value=MagicMock())

    metadata: dict = {}
    if goal_state is not None:
        metadata["goal_state"] = goal_state
    session = SimpleNamespace(metadata=metadata)

    with patch("nanobot.agent.loop.ContextBuilder"), \
         patch("nanobot.agent.loop.SessionManager") as mock_sm, \
         patch("nanobot.agent.loop.SubagentManager") as mock_sub_mgr:
        mock_sm.return_value.get_or_create.return_value = session
        mock_sub_mgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=bus, provider=provider, workspace=workspace)
    return loop, bus, session


async def _start_hanging_dispatch(loop, key: str):
    """Start a real _dispatch task that hangs inside _process_message."""
    started = asyncio.Event()

    async def hang(msg, **kwargs):
        started.set()
        await asyncio.sleep(60)

    loop._process_message = hang
    msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="go")
    assert msg.session_key == key
    task = asyncio.create_task(loop._dispatch(msg))
    # run() normally registers dispatch tasks here; do it for direct starts.
    loop._active_tasks.setdefault(key, []).append(task)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    return task


class TestAbortSession:
    @pytest.mark.asyncio
    async def test_abort_no_active_task_deactivates_goal(self):
        loop, _bus, session = _make_loop(
            goal_state={"status": "active", "objective": "do stuff"}
        )
        cancelled, goal_ended = await loop.abort_session("test:c1")

        assert cancelled == 0
        assert goal_ended is True
        assert session.metadata["goal_state"]["status"] == "aborted"
        assert "test:c1" not in loop._abort_flags

    @pytest.mark.asyncio
    async def test_abort_no_goal_no_task_is_noop(self):
        loop, _bus, _session = _make_loop()
        cancelled, goal_ended = await loop.abort_session("test:c1")

        assert cancelled == 0
        assert goal_ended is False

    @pytest.mark.asyncio
    async def test_abort_drops_pending_queue_and_followups(self):
        loop, bus, session = _make_loop(
            goal_state={"status": "active", "objective": "marathon"}
        )
        task = await _start_hanging_dispatch(loop, "test:c1")
        key = "test:c1"

        loop._pending_queues[key].put_nowait(
            InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="held")
        )
        loop._followup_queues[key] = [
            {"channel": "test", "sender_id": "u1", "chat_id": "c1", "content": "after"}
        ]

        cancelled, goal_ended = await loop.abort_session(key)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)

        assert cancelled == 1
        assert goal_ended is True
        assert session.metadata["goal_state"]["status"] == "aborted"
        assert key not in loop._abort_flags
        # Killswitch: nothing re-published to the bus.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(bus.consume_inbound(), timeout=0.2)
        assert loop._pending_queues.get(key) is None
        assert loop._followup_queues.get(key) is None

    @pytest.mark.asyncio
    async def test_stop_still_republishes_leftovers(self):
        loop, bus, _session = _make_loop()
        task = await _start_hanging_dispatch(loop, "test:c1")
        key = "test:c1"

        loop._pending_queues[key].put_nowait(
            InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="held")
        )

        cancelled = await loop._cancel_active_tasks(key)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)

        assert cancelled == 1
        # /stop semantics preserved: parked message is re-published.
        republished = await asyncio.wait_for(bus.consume_inbound(), timeout=1.0)
        assert republished.content == "held"


class TestAbortCommand:
    @pytest.mark.asyncio
    async def test_cmd_abort_reports_killswitch(self):
        from nanobot.command.builtin import cmd_abort
        from nanobot.command.router import CommandContext

        loop, _bus, _session = _make_loop(
            goal_state={"status": "active", "objective": "marathon"}
        )
        task = await _start_hanging_dispatch(loop, "test:c1")

        msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="/abort")
        ctx = CommandContext(msg=msg, session=None, key=msg.session_key, raw="/abort", loop=loop)
        out = await cmd_abort(ctx)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)

        assert "Aborted 1 task" in out.content
        assert "goal" in out.content.lower()
        assert "dropped" in out.content.lower()


class TestAbortRegistration:
    def test_abort_and_interrupt_are_priority(self):
        from nanobot.command.builtin import register_builtin_commands
        from nanobot.command.router import CommandRouter

        router = CommandRouter()
        register_builtin_commands(router)
        assert router.is_priority("/abort")
        assert router.is_priority("/stop")

        # /interrupt is special-cased in AgentLoop.run() before priority
        # dispatch; it must NOT be shadowed by a priority registration.
        assert not router.is_priority("/interrupt")
