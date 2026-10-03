"""Tests for ChannelManager delta coalescing to reduce streaming latency."""
import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock

import pytest

from nanobot.bus.events import DeliveryResult, InboundMessage, OutboundMessage
from nanobot.bus.outbound_events import TurnEndEvent
from nanobot.bus.progress import build_bus_progress_callback
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.manager import ChannelManager
from nanobot.config.schema import Config


class MockChannel(BaseChannel):
    """Mock channel for testing."""

    name = "mock"
    display_name = "Mock"

    def __init__(self, config, bus):
        super().__init__(config, bus)
        self._send_delta_mock = AsyncMock()
        self._send_reasoning_delta_mock = AsyncMock()
        self._send_reasoning_end_mock = AsyncMock()
        self._send_mock = AsyncMock()

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send(self, msg):
        """Implement abstract method."""
        return await self._send_mock(msg)

    async def send_delta(self, chat_id, delta, metadata=None):
        """Override send_delta for testing."""
        return await self._send_delta_mock(chat_id, delta, metadata)

    async def send_reasoning_delta(self, chat_id, delta, metadata=None):
        await self._send_reasoning_delta_mock(chat_id, delta, metadata)

    async def send_reasoning_end(self, chat_id, metadata=None):
        await self._send_reasoning_end_mock(chat_id, metadata)


@pytest.fixture
def config():
    """Create a minimal config for testing."""
    return Config()


@pytest.fixture
def bus():
    """Create a message bus for testing."""
    return MessageBus()


@pytest.fixture
def manager(config, bus):
    """Create a channel manager with a mock channel."""
    manager = ChannelManager(config, bus)
    manager.channels["mock"] = MockChannel({}, bus)
    return manager


class TestDeltaCoalescing:
    """Tests for _stream_delta message coalescing."""

    @pytest.mark.asyncio
    async def test_single_delta_not_coalesced(self, manager, bus):
        """A single delta should be sent as-is."""
        msg = OutboundMessage(
            channel="mock",
            chat_id="chat1",
            content="Hello",
            metadata={"_stream_delta": True},
        )
        await bus.publish_outbound(msg)

        # Process one message
        async def process_one():
            try:
                m = await asyncio.wait_for(bus.consume_outbound(), timeout=0.1)
                if m.metadata.get("_stream_delta"):
                    m, pending = manager._coalesce_stream_deltas(m)
                    # Put pending back (none expected)
                    for p in pending:
                        await bus.publish_outbound(p)
                channel = manager.channels.get(m.channel)
                if channel:
                    await channel.send_delta(m.chat_id, m.content, m.metadata)
            except asyncio.TimeoutError:
                pass

        await process_one()

        manager.channels["mock"]._send_delta_mock.assert_called_once_with(
            "chat1", "Hello", {"_stream_delta": True}
        )

    @pytest.mark.asyncio
    async def test_multiple_deltas_coalesced(self, manager, bus):
        """Multiple consecutive deltas for same chat should be merged."""
        # Put multiple deltas in queue
        for text in ["Hello", " ", "world", "!"]:
            await bus.publish_outbound(OutboundMessage(
                channel="mock",
                chat_id="chat1",
                content=text,
                metadata={"_stream_delta": True},
            ))

        # Process using coalescing logic
        first_msg = await bus.consume_outbound()
        merged, pending = manager._coalesce_stream_deltas(first_msg)

        # Should have merged all deltas
        assert merged.content == "Hello world!"
        assert merged.metadata.get("_stream_delta") is True
        # No pending messages (all were coalesced)
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_deltas_different_chats_not_coalesced(self, manager, bus):
        """Deltas for different chats should not be merged."""
        # Put deltas for different chats
        await bus.publish_outbound(OutboundMessage(
            channel="mock",
            chat_id="chat1",
            content="Hello",
            metadata={"_stream_delta": True},
        ))
        await bus.publish_outbound(OutboundMessage(
            channel="mock",
            chat_id="chat2",
            content="World",
            metadata={"_stream_delta": True},
        ))

        first_msg = await bus.consume_outbound()
        merged, pending = manager._coalesce_stream_deltas(first_msg)

        # First chat should not include second chat's content
        assert merged.content == "Hello"
        assert merged.chat_id == "chat1"
        # Second chat should be in pending
        assert len(pending) == 1
        assert pending[0].chat_id == "chat2"
        assert pending[0].content == "World"

    @pytest.mark.asyncio
    async def test_stream_end_terminates_coalescing(self, manager, bus):
        """_stream_end should stop coalescing and be included in final message."""
        # Put deltas with stream_end at the end
        await bus.publish_outbound(OutboundMessage(
            channel="mock",
            chat_id="chat1",
            content="Hello",
            metadata={"_stream_delta": True},
        ))
        await bus.publish_outbound(OutboundMessage(
            channel="mock",
            chat_id="chat1",
            content=" world",
            metadata={"_stream_delta": True, "_stream_end": True},
        ))

        first_msg = await bus.consume_outbound()
        merged, pending = manager._coalesce_stream_deltas(first_msg)

        # Should have merged content
        assert merged.content == "Hello world"
        # Should have stream_end flag
        assert merged.metadata.get("_stream_end") is True
        # No pending
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_coalescing_stops_at_first_non_matching_boundary(self, manager, bus):
        """Only consecutive deltas should be merged; later deltas stay queued."""
        await bus.publish_outbound(OutboundMessage(
            channel="mock",
            chat_id="chat1",
            content="Hello",
            metadata={"_stream_delta": True, "_stream_id": "seg-1"},
        ))
        await bus.publish_outbound(OutboundMessage(
            channel="mock",
            chat_id="chat1",
            content="",
            metadata={"_stream_end": True, "_stream_id": "seg-1"},
        ))
        await bus.publish_outbound(OutboundMessage(
            channel="mock",
            chat_id="chat1",
            content="world",
            metadata={"_stream_delta": True, "_stream_id": "seg-2"},
        ))

        first_msg = await bus.consume_outbound()
        merged, pending = manager._coalesce_stream_deltas(first_msg)

        assert merged.content == "Hello"
        assert merged.metadata.get("_stream_end") is None
        assert len(pending) == 1
        assert pending[0].metadata.get("_stream_end") is True
        assert pending[0].metadata.get("_stream_id") == "seg-1"

        # The next stream segment must remain in queue order for later dispatch.
        remaining = await bus.consume_outbound()
        assert remaining.content == "world"
        assert remaining.metadata.get("_stream_id") == "seg-2"

    @pytest.mark.asyncio
    async def test_non_delta_message_preserved(self, manager, bus):
        """Non-delta messages should be preserved in pending list."""
        await bus.publish_outbound(OutboundMessage(
            channel="mock",
            chat_id="chat1",
            content="Delta",
            metadata={"_stream_delta": True},
        ))
        await bus.publish_outbound(OutboundMessage(
            channel="mock",
            chat_id="chat1",
            content="Final message",
            metadata={},  # Not a delta
        ))

        first_msg = await bus.consume_outbound()
        merged, pending = manager._coalesce_stream_deltas(first_msg)

        assert merged.content == "Delta"
        assert len(pending) == 1
        assert pending[0].content == "Final message"
        assert pending[0].metadata.get("_stream_delta") is None

    @pytest.mark.asyncio
    async def test_empty_queue_stops_coalescing(self, manager, bus):
        """Coalescing should stop when queue is empty."""
        await bus.publish_outbound(OutboundMessage(
            channel="mock",
            chat_id="chat1",
            content="Only message",
            metadata={"_stream_delta": True},
        ))

        first_msg = await bus.consume_outbound()
        merged, pending = manager._coalesce_stream_deltas(first_msg)

        assert merged.content == "Only message"
        assert len(pending) == 0


class TestDispatchOutboundWithCoalescing:
    """Tests for the full _dispatch_outbound flow with coalescing."""

    @pytest.mark.asyncio
    async def test_dispatch_coalesces_and_processes_pending(self, manager, bus):
        """_dispatch_outbound should coalesce deltas and process pending messages."""
        # Put multiple deltas followed by a regular message
        await bus.publish_outbound(OutboundMessage(
            channel="mock",
            chat_id="chat1",
            content="A",
            metadata={"_stream_delta": True},
        ))
        await bus.publish_outbound(OutboundMessage(
            channel="mock",
            chat_id="chat1",
            content="B",
            metadata={"_stream_delta": True},
        ))
        await bus.publish_outbound(OutboundMessage(
            channel="mock",
            chat_id="chat1",
            content="Final",
            metadata={},  # Regular message
        ))

        # Run one iteration of dispatch logic manually
        pending = []
        processed = []

        # First iteration: should coalesce A+B
        if pending:
            msg = pending.pop(0)
        else:
            msg = await bus.consume_outbound()

        if msg.metadata.get("_stream_delta") and not msg.metadata.get("_stream_end"):
            msg, extra_pending = manager._coalesce_stream_deltas(msg)
            pending.extend(extra_pending)

        channel = manager.channels.get(msg.channel)
        if channel:
            await channel.send_delta(msg.chat_id, msg.content, msg.metadata)
            processed.append(("delta", msg.content))

        # Should have sent coalesced delta
        assert processed == [("delta", "AB")]
        # Should have pending regular message
        assert len(pending) == 1
        assert pending[0].content == "Final"


class TestReasoningDispatch:
    """Reasoning shares delta batching, not ordinary progress filtering."""

    @pytest.mark.asyncio
    async def test_long_reasoning_is_sent_once_and_flushed_in_order(self, manager, bus):
        channel = manager.channels["mock"]
        channel.send_progress = False
        channel.send_tool_hints = True
        sent = []
        channel._send_reasoning_delta_mock.side_effect = (
            lambda chat, text, meta: sent.append(("reasoning_delta", text, meta))
        )
        channel._send_reasoning_end_mock.side_effect = (
            lambda chat, meta: sent.append(("reasoning_end", "", meta))
        )
        channel._send_delta_mock.side_effect = (
            lambda chat, text, meta: sent.append(("assistant", text, meta))
        )

        def send(msg):
            kind = "turn_end" if isinstance(msg.event, TurnEndEvent) else "message"
            sent.append((kind, msg.content, msg.metadata))
            return DeliveryResult("delivered")

        channel._send_mock.side_effect = send
        metadata = {"webui_turn_id": "turn-1", "message_id": "source-1"}
        progress = build_bus_progress_callback(
            bus, InboundMessage("mock", "user", "chat1", "question", metadata=metadata),
        )
        for _ in range(1000):
            await progress("think ", reasoning=True)
        await progress("", reasoning_end=True)
        await progress("tool", tool_hint=True)
        await progress("next", reasoning=True)
        await progress("", reasoning_end=True)
        for text in ("An", "swer"):
            await bus.publish_outbound(OutboundMessage(
                "mock", "chat1", text,
                metadata={"_stream_delta": True, "_stream_id": "answer-1"},
            ))
        await bus.publish_outbound(OutboundMessage(
            "mock", "chat1", "", metadata={"_stream_end": True, "_stream_id": "answer-1"},
        ))
        await bus.publish_outbound(OutboundMessage("mock", "chat1", "", event=TurnEndEvent()))
        barrier = await bus.publish_outbound_tracked(OutboundMessage("mock", "chat1", "done"))

        task = asyncio.create_task(manager._dispatch_outbound())
        try:
            assert (await asyncio.wait_for(barrier, 1)).status == "delivered"
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        assert [(kind, text) for kind, text, _ in sent] == [
            ("reasoning_delta", "think " * 1000),
            ("reasoning_end", ""),
            ("message", "tool"),
            ("reasoning_delta", "next"),
            ("reasoning_end", ""),
            ("assistant", "Answer"),
            ("assistant", ""),
            ("turn_end", ""),
            ("message", "done"),
        ]
        assert sent[0][2] == {**metadata, "_progress": True, "_tool_hint": False,
                              "_reasoning_delta": True}
        assert sent[-3][2]["_stream_end"] is True
        assert bus.outbound.empty()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("delta_flag", ["_stream_delta", "_reasoning_delta"])
    @pytest.mark.parametrize("boundary", ["chat", "channel", "stream", "turn", "kind", "end", "tool"])
    async def test_coalescing_preserves_boundaries(self, manager, bus, delta_flag, boundary):
        first = OutboundMessage("mock", "chat1", "A", metadata={delta_flag: True, "_stream_id": "s1"})
        next_msg = OutboundMessage("mock", "chat1", "B", metadata=dict(first.metadata))
        if boundary == "chat":
            next_msg.chat_id = "chat2"
        elif boundary == "channel":
            next_msg.channel = "other"
        elif boundary == "stream":
            next_msg.metadata["_stream_id"] = "s2"
        elif boundary == "turn":
            next_msg.metadata["webui_turn_id"] = "turn-2"
        elif boundary == "kind":
            other_flag = "_reasoning_delta" if delta_flag == "_stream_delta" else "_stream_delta"
            next_msg.metadata = {other_flag: True}
        elif boundary == "end":
            end_flag = "_reasoning_end" if delta_flag == "_reasoning_delta" else "_stream_end"
            next_msg.metadata = {end_flag: True}
        else:
            next_msg.metadata = {"_progress": True, "_tool_hint": True}
        later = OutboundMessage("mock", "chat1", "C", metadata=dict(first.metadata))
        await bus.publish_outbound(next_msg)
        await bus.publish_outbound(later)

        merged, pending = manager._coalesce_stream_deltas(first)

        assert merged.content == "A"
        assert pending == [next_msg]
        assert await bus.consume_outbound() is later

    @pytest.mark.asyncio
    @pytest.mark.parametrize("delta_flag", ["_stream_delta", "_reasoning_delta"])
    @pytest.mark.parametrize("fails", [False, True])
    async def test_tracked_deltas_keep_acknowledgements(self, manager, bus, delta_flag, fails):
        channel = manager.channels["mock"]
        manager.config.channels.send_max_retries = 1
        send_mock = (channel._send_reasoning_delta_mock if delta_flag == "_reasoning_delta"
                     else channel._send_delta_mock)
        if fails:
            send_mock.side_effect = RuntimeError("transport failed")
        futures = []
        for text, tracked in [("A", False), ("B", True), ("C", True), ("D", False)]:
            msg = OutboundMessage("mock", "chat1", text, metadata={delta_flag: True})
            if tracked:
                futures.append(await bus.publish_outbound_tracked(msg))
            else:
                await bus.publish_outbound(msg)
        barrier = await bus.publish_outbound_tracked(OutboundMessage("mock", "chat1", "done"))

        task = asyncio.create_task(manager._dispatch_outbound())
        try:
            await asyncio.wait_for(barrier, 1)
            assert all(future.done() for future in futures)
            assert [future.result().status for future in futures] == [
                "failed" if fails else "unknown",
            ] * 2
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        assert [call.args[1] for call in send_mock.await_args_list] == ["A", "B", "C", "D"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("flag", ["_reasoning_delta", "_reasoning_end", "_reasoning"])
    @pytest.mark.parametrize("enabled", [False, True])
    async def test_reasoning_visibility_and_tracked_ack(self, manager, bus, flag, enabled):
        channel = manager.channels["mock"]
        channel.show_reasoning = enabled
        future = await bus.publish_outbound_tracked(OutboundMessage(
            "mock", "chat1", "think", metadata={"_progress": True, flag: True},
        ))
        barrier = await bus.publish_outbound_tracked(OutboundMessage("mock", "chat1", "done"))
        task = asyncio.create_task(manager._dispatch_outbound())
        try:
            await asyncio.wait_for(barrier, 1)
            assert future.done()
            assert future.result().status == ("unknown" if enabled else "suppressed")
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        assert channel._send_reasoning_delta_mock.await_count == int(enabled and flag != "_reasoning_end")
        assert channel._send_reasoning_end_mock.await_count == int(enabled and flag != "_reasoning_delta")
        channel._send_mock.assert_awaited_once()


class TestProgressFiltering:
    """Progress filtering should honor per-channel settings."""

    def test_progress_visibility_uses_global_defaults(self, manager):
        assert manager._should_send_progress("mock", tool_hint=False) is True
        assert manager._should_send_progress("mock", tool_hint=True) is False

    def test_progress_visibility_uses_channel_overrides(self, manager):
        manager.channels["mock"].send_progress = False
        manager.channels["mock"].send_tool_hints = True

        assert manager._should_send_progress("mock", tool_hint=False) is False
        assert manager._should_send_progress("mock", tool_hint=True) is True

    def test_progress_visibility_returns_false_for_missing_channel(self, manager):
        assert manager._should_send_progress("nonexistent", tool_hint=False) is False
        assert manager._should_send_progress("nonexistent", tool_hint=True) is False

    def test_resolve_bool_override_dict(self, manager):
        assert manager._resolve_bool_override({}, "send_progress", True) is True
        assert manager._resolve_bool_override({"send_progress": False}, "send_progress", True) is False
        assert manager._resolve_bool_override({"sendProgress": False}, "send_progress", True) is False
        assert manager._resolve_bool_override({"send_progress": "false"}, "send_progress", True) is True

    def test_resolve_bool_override_model(self, manager):
        class FakeSection:
            send_progress = False
            send_tool_hints = True

        assert manager._resolve_bool_override(FakeSection(), "send_progress", True) is False
        assert manager._resolve_bool_override(FakeSection(), "send_tool_hints", False) is True
        # Missing attribute falls back to default
        assert manager._resolve_bool_override(FakeSection(), "unknown_key", True) is True

    @pytest.mark.asyncio
    async def test_channel_override_can_drop_progress_message(self, manager, bus):
        manager.channels["mock"].send_progress = False
        await bus.publish_outbound(OutboundMessage(
            channel="mock",
            chat_id="chat1",
            content="thinking",
            metadata={"_progress": True},
        ))
        await bus.publish_outbound(OutboundMessage(
            channel="mock",
            chat_id="chat1",
            content="final answer",
            metadata={},
        ))

        task = asyncio.create_task(manager._dispatch_outbound())
        try:
            for _ in range(30):
                if manager.channels["mock"]._send_mock.await_count >= 1:
                    break
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        send_mock = manager.channels["mock"]._send_mock
        assert send_mock.await_count == 1
        assert send_mock.await_args_list[0].args[0].content == "final answer"

    @pytest.mark.asyncio
    async def test_channel_override_can_enable_tool_hints(self, manager, bus):
        manager.channels["mock"].send_tool_hints = True
        await bus.publish_outbound(OutboundMessage(
            channel="mock",
            chat_id="chat1",
            content="read_file(foo.py)",
            metadata={"_progress": True, "_tool_hint": True},
        ))

        task = asyncio.create_task(manager._dispatch_outbound())
        try:
            for _ in range(30):
                if manager.channels["mock"]._send_mock.await_count >= 1:
                    break
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        send_mock = manager.channels["mock"]._send_mock
        assert send_mock.await_count == 1
        assert send_mock.await_args_list[0].args[0].content == "read_file(foo.py)"


class TestRetryWaitFiltering:
    """Internal provider retry heartbeats must never reach channels."""

    @pytest.mark.asyncio
    async def test_retry_wait_message_dropped(self, manager, bus):
        """A ``_retry_wait`` message must be filtered before channel dispatch.

        Regression: provider retry diagnostics like
        ``Model request failed, retry in 1s (attempt 1).`` were being
        delivered to end-user channels because the runner bound
        ``on_retry_wait`` to the progress callback.
        """
        retry_msg = OutboundMessage(
            channel="mock",
            chat_id="chat1",
            content="Model request failed, retry in 1s (attempt 1).",
            metadata={"_retry_wait": True},
        )
        real_msg = OutboundMessage(
            channel="mock",
            chat_id="chat1",
            content="final answer",
            metadata={},
        )
        await bus.publish_outbound(retry_msg)
        await bus.publish_outbound(real_msg)

        task = asyncio.create_task(manager._dispatch_outbound())
        try:
            for _ in range(30):
                if manager.channels["mock"]._send_mock.await_count >= 1:
                    break
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        send_mock = manager.channels["mock"]._send_mock
        assert send_mock.await_count == 1
        sent = send_mock.await_args_list[0].args[0]
        assert sent.content == "final answer"
        assert not sent.metadata.get("_retry_wait")
