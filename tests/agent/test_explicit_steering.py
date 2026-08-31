"""Tests for /steer, /after, /interrupt explicit run controls (issue #32)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus


def _make_loop(tmp_path: Path):
    from nanobot.agent.loop import AgentLoop

    loop = AgentLoop(
        bus=MessageBus(),
        provider=MagicMock(),
        workspace=tmp_path,
        model="test-model",
    )
    return loop



def _recorder(bucket: list):
    async def _cap(m):
        bucket.append(m)
    return _cap


def _msg(text: str, *, sender: str = "u1") -> InboundMessage:
    return InboundMessage(channel="telegram", sender_id=sender, chat_id="11", content=text)


@pytest.mark.asyncio
async def test_steer_requires_active_and_queues_tagged_guidance(tmp_path) -> None:
    loop = _make_loop(tmp_path)
    outbound = []
    loop.bus.publish_outbound = _recorder(outbound)

    # No active run -> deterministic ack.
    await loop._handle_steer(_msg("/steer hi"), "telegram:11")
    assert "No active run" in outbound[-1].content

    # Active run: guidance lands in the existing safe-boundary queue, tagged.
    loop._pending_queues["telegram:11"] = asyncio.Queue(maxsize=2)
    await loop._handle_steer(_msg("/steer focus on tests"), "telegram:11")
    item = loop._pending_queues["telegram:11"].get_nowait()
    assert item.content.startswith("[steer]")
    assert item.metadata.get("control") == "steer"
    assert "Steering" in outbound[-1].content

    # Empty -> usage.
    await loop._handle_steer(_msg("/steer"), "telegram:11")
    assert "Usage: `/steer" in outbound[-1].content


@pytest.mark.asyncio
async def test_after_queues_fifo_and_bounds(tmp_path) -> None:
    loop = _make_loop(tmp_path)
    outbound = []
    loop.bus.publish_outbound = _recorder(outbound)

    loop._pending_queues["telegram:11"] = asyncio.Queue()
    await loop._handle_after(_msg("/after first thing"), "telegram:11")
    await loop._handle_after(_msg("/after second thing"), "telegram:11")
    assert len(loop._followup_queues["telegram:11"]) == 2
    drained = loop._drain_followups("telegram:11")
    assert [d["text"] for d in drained] == ["first thing", "second thing"]  # FIFO
    assert "queued" in outbound[-1].content

    # Bound.
    await loop._handle_after(_msg("/after a"), "telegram:11")
    for _ in range(loop._followup_limit):
        await loop._handle_after(_msg("/after x"), "telegram:11")
    await loop._handle_after(_msg("/after overflow"), "telegram:11")
    await asyncio.sleep(0)
    assert "full" in outbound[-1].content


@pytest.mark.asyncio
async def test_after_without_active_publishes_now(tmp_path) -> None:
    loop = _make_loop(tmp_path)
    inbound = []
    loop.bus.publish_inbound = _recorder(inbound)
    await loop._handle_after(_msg("/after note: remember milk"), "telegram:11")
    assert inbound and inbound[0].content == "note: remember milk"


@pytest.mark.asyncio
async def test_completed_run_drains_after_queue_before_idle(tmp_path, monkeypatch) -> None:
    loop = _make_loop(tmp_path)
    key = "telegram:11"
    loop._followup_queues[key] = [
        {"content": "next", "sender_id": "11", "chat_id": "11", "channel": "telegram"},
    ]
    monkeypatch.setattr(loop, "_process_message", AsyncMock(return_value=None))

    msg = InboundMessage(channel="telegram", sender_id="11", chat_id="11", content="hello")
    await loop._dispatch(msg)

    assert key not in loop._followup_queues
    queued = await loop.bus.consume_inbound()
    assert queued.content == "next"
    assert queued.session_key_override == key


@pytest.mark.asyncio
async def test_interrupt_cancels_then_replaces(tmp_path) -> None:
    loop = _make_loop(tmp_path)
    outbound = []
    loop.bus.publish_outbound = _recorder(outbound)
    inbound = []
    loop.bus.publish_inbound = _recorder(inbound)
    loop._pending_queues["telegram:11"] = asyncio.Queue()
    loop._cancel_active_tasks = AsyncMock(return_value=2)

    await loop._handle_interrupt(_msg("/interrupt new plan"), "telegram:11")
    assert inbound and inbound[0].content == "new plan"
    assert "replacement" in outbound[-1].content
    loop._cancel_active_tasks.assert_awaited_once_with("telegram:11")

    # No active run -> ack, no replacement.
    loop._cancel_active_tasks = AsyncMock(return_value=0)
    loop._pending_queues.clear()
    loop._active_tasks.clear()
    await loop._handle_interrupt(_msg("/interrupt x"), "telegram:11")
    assert "No active run" in outbound[-1].content


@pytest.mark.asyncio
async def test_interrupt_timeout_blocks_replacement(tmp_path) -> None:
    loop = _make_loop(tmp_path)
    outbound = []
    loop.bus.publish_outbound = _recorder(outbound)
    inbound = []
    loop.bus.publish_inbound = _recorder(inbound)

    async def hang(_key):
        await asyncio.sleep(0)
        raise asyncio.TimeoutError

    # Make wait_for time out quickly by stubbing the cancel coroutine.
    loop._pending_queues["telegram:11"] = asyncio.Queue()

    async def cancel_stub(key):
        raise asyncio.TimeoutError()

    loop._cancel_active_tasks = cancel_stub
    await loop._handle_interrupt(_msg("/interrupt z"), "telegram:11")
    assert inbound == []
    assert "timed out" in outbound[-1].content


def test_control_specs_present() -> None:
    from nanobot.command.builtin import BUILTIN_COMMAND_SPECS
    specs = {s.command: s for s in BUILTIN_COMMAND_SPECS}
    assert "/steer" in specs and "/after" in specs and "/interrupt" in specs
