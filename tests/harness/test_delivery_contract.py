"""Delivery-acknowledgement contract tests.

Audit-derived: cron (and the session mirror) must not claim a user received a
message just because it was queued on the in-memory bus. Only a channel
acknowledgement is "delivered"; unavailable/definite rejection is "failed";
ambiguous timeouts are "unknown"; intentional non-sends are "suppressed".
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from nanobot.bus.events import DeliveryResult, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.manager import ChannelManager
from nanobot.config.schema import Config
from nanobot.cron.bound_runner import run_isolated_cron_job
from nanobot.cron.types import CronJob, CronPayload, CronSchedule


class AckChannel(BaseChannel):
    """Channel whose send returns a scripted DeliveryResult."""

    name = "mock"
    display_name = "Mock"

    def __init__(self, config: Any, bus: MessageBus, result: DeliveryResult | None):
        super().__init__(config, bus)
        self.result = result
        self.send_mock = AsyncMock(return_value=result)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, msg: OutboundMessage) -> DeliveryResult | None:
        return await self.send_mock(msg)


def _manager(bus: MessageBus, channel: BaseChannel) -> ChannelManager:
    manager = ChannelManager(Config(), bus)
    manager.channels["mock"] = channel
    return manager


async def _dispatch_one(manager: ChannelManager, bus: MessageBus) -> None:
    msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
    channel = manager.channels.get(msg.channel)
    if channel:
        result = await manager._send_with_retry(channel, msg)
        if msg.delivery is not None and result is not None:
            manager._acknowledge(msg, result)
    else:
        manager._acknowledge(msg, DeliveryResult("failed", f"Unknown channel: {msg.channel}"))


@pytest.mark.asyncio
async def test_channel_acknowledgement_resolves_delivery_future() -> None:
    bus = MessageBus()
    manager = _manager(bus, AckChannel({}, bus, DeliveryResult("delivered")))
    msg = OutboundMessage(channel="mock", chat_id="c1", content="hi")
    future = await bus.publish_outbound_tracked(msg)
    await _dispatch_one(manager, bus)
    assert await asyncio.wait_for(future, timeout=1.0) == DeliveryResult("delivered")


@pytest.mark.asyncio
async def test_channel_failure_fails_the_future() -> None:
    bus = MessageBus()
    manager = _manager(bus, AckChannel({}, bus, DeliveryResult("failed", "bot down")))
    msg = OutboundMessage(channel="mock", chat_id="c1", content="hi")
    future = await bus.publish_outbound_tracked(msg)
    await _dispatch_one(manager, bus)
    result = await asyncio.wait_for(future, timeout=1.0)
    assert result.status == "failed"
    assert result.error == "bot down"


@pytest.mark.asyncio
async def test_unknown_channel_fails_instead_of_claiming_delivery() -> None:
    bus = MessageBus()
    manager = ChannelManager(Config(), bus)  # no channels at all
    msg = OutboundMessage(channel="ghost", chat_id="c1", content="hi")
    future = await bus.publish_outbound_tracked(msg)
    await _dispatch_one(manager, bus)
    result = await asyncio.wait_for(future, timeout=1.0)
    assert result.status == "failed"


@pytest.mark.asyncio
async def test_wait_delivery_timeout_is_unknown_not_success() -> None:
    bus = MessageBus()
    msg = OutboundMessage(channel="mock", chat_id="c1", content="hi")
    await bus.publish_outbound_tracked(msg)
    result = await bus.wait_delivery(msg, timeout=0.05)
    assert result.status == "unknown"


def _job() -> CronJob:
    return CronJob(
        id="job1",
        name="reminder",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        payload=CronPayload(
            message="ping",
            session_key="telegram:42",
            origin_channel="telegram",
            origin_chat_id="42",
        ),
    )


class _FakeAgent:
    tools = type("T", (), {"get": staticmethod(lambda name: None)})()
    provider = type("Prov", (), {})()
    model = "test-model"
    last_usage: dict[str, int] = {}

    def cron_run_snapshot(self) -> dict[str, Any] | None:
        return None

    def cron_run_snapshot_for_preset(self, name: str) -> dict[str, Any] | None:
        return None

    async def process_direct(self, content: str, **kwargs: Any) -> OutboundMessage:
        return OutboundMessage(channel="telegram", chat_id="42", content="reply body")


class _FakeRecorder:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def write_run_record(self, run_id: str, record: dict[str, Any]) -> None:
        self.records.append((run_id, record))


@pytest.mark.asyncio
async def test_isolated_run_records_unknown_delivery_without_success_claim() -> None:
    agent, recorder = _FakeAgent(), _FakeRecorder()

    async def ambiguous_deliver(*args: Any, **kwargs: Any) -> DeliveryResult:
        return DeliveryResult("unknown", "send timed out")

    await run_isolated_cron_job(
        _job(), agent=agent, cron=recorder, deliver=ambiguous_deliver
    )
    record = recorder.records[-1][1]
    # The agent turn succeeded, but the record must not claim the user saw it.
    assert record["status"] == "ok"
    assert record["delivery"]["status"] == "unknown"
    assert record["delivery"]["error"] == "send timed out"


@pytest.mark.asyncio
async def test_isolated_run_raises_on_failed_acknowledgement() -> None:
    agent, recorder = _FakeAgent(), _FakeRecorder()
    job = _job()

    async def failing_deliver(*args: Any, **kwargs: Any) -> DeliveryResult:
        return DeliveryResult("failed", "bot blocked")

    with pytest.raises(RuntimeError, match="delivery failed"):
        await run_isolated_cron_job(
            job, agent=agent, cron=recorder, deliver=failing_deliver
        )
    record = recorder.records[-1][1]
    assert record["status"] == "error"
    assert record["delivery"]["status"] == "failed"
    assert job.state.last_delivery_status == "failed"


@pytest.mark.asyncio
async def test_isolated_suppresses_exact_silent_marker() -> None:
    class SilentAgent(_FakeAgent):
        async def process_direct(self, content: str, **kwargs: Any) -> OutboundMessage:
            return OutboundMessage(
                channel="telegram", chat_id="42", content="[SILENT]"
            )

    agent, recorder = SilentAgent(), _FakeRecorder()
    sent: list[OutboundMessage] = []

    async def deliver(msg: OutboundMessage, **kwargs: Any) -> DeliveryResult:
        sent.append(msg)
        return DeliveryResult("delivered")

    await run_isolated_cron_job(_job(), agent=agent, cron=recorder, deliver=deliver)
    assert sent == []
    assert recorder.records[-1][1]["delivery"]["status"] == "suppressed"
