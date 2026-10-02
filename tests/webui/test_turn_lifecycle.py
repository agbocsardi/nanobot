"""The gateway must compose the WebUI turn lifecycle itself.

Regression guard for the graft gap that left the WebUI without ``turn_end``:
the bridge and the turn coordinator both existed and were unit-tested, but
nothing attached them at gateway startup, so the runtime event bus had no
subscribers and every WebUI turn stayed in the streaming state forever.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.outbound_events import GoalStatusEvent, TurnEndEvent
from nanobot.bus.queue import MessageBus
from nanobot.bus.runtime_events import (
    RuntimeEventBus,
    RuntimeEventContext,
    SessionTurnStarted,
    TurnCompleted,
    TurnRunStatusChanged,
)
from nanobot.session.manager import SessionManager
from nanobot.webui.turn_lifecycle import attach_webui_turn_lifecycle


def _ctx(session_key: str = "webui:c1") -> RuntimeEventContext:
    return RuntimeEventContext(
        channel="websocket",
        chat_id="c1",
        session_key=session_key,
        metadata={"webui": True},
    )


async def _drain_outbound(bus: MessageBus) -> list[OutboundMessage]:
    out: list[OutboundMessage] = []
    while True:
        try:
            out.append(bus.outbound.get_nowait())
        except asyncio.QueueEmpty:
            return out


@pytest.mark.asyncio
async def test_attach_webui_turn_lifecycle_emits_turn_end(tmp_path: Any) -> None:
    bus = MessageBus()
    sessions = SessionManager(workspace=tmp_path)
    runtime_bus = RuntimeEventBus()

    detach = attach_webui_turn_lifecycle(bus, runtime_bus, sessions)

    await runtime_bus.publish(SessionTurnStarted(context=_ctx()))
    await runtime_bus.publish(
        TurnRunStatusChanged(context=_ctx(), status="running", started_at=1.0)
    )
    await runtime_bus.publish(TurnCompleted(context=_ctx(), latency_ms=42, runtime=None))

    events = [m.event for m in await _drain_outbound(bus)]
    assert any(isinstance(e, GoalStatusEvent) and e.status == "running" for e in events)
    turn_ends = [e for e in events if isinstance(e, TurnEndEvent)]
    assert len(turn_ends) == 1
    assert turn_ends[0].latency_ms == 42

    detach()


@pytest.mark.asyncio
async def test_detach_unsubscribes_both_halves(tmp_path: Any) -> None:
    bus = MessageBus()
    sessions = SessionManager(workspace=tmp_path)
    runtime_bus = RuntimeEventBus()

    detach = attach_webui_turn_lifecycle(bus, runtime_bus, sessions)
    detach()

    await runtime_bus.publish(SessionTurnStarted(context=_ctx()))
    await runtime_bus.publish(TurnCompleted(context=_ctx(), latency_ms=1, runtime=None))
    await asyncio.sleep(0)

    assert await _drain_outbound(bus) == []


@pytest.mark.asyncio
async def test_legacy_websocket_turns_stay_silent(tmp_path: Any) -> None:
    """Composing the lifecycle must not leak frames into legacy websocket turns."""
    bus = MessageBus()
    sessions = SessionManager(workspace=tmp_path)
    runtime_bus = RuntimeEventBus()
    attach_webui_turn_lifecycle(bus, runtime_bus, sessions)

    legacy = RuntimeEventContext(
        channel="websocket",
        chat_id="legacy-1",
        session_key="websocket:legacy-1",
        metadata={},
    )
    await runtime_bus.publish(SessionTurnStarted(context=legacy))
    await runtime_bus.publish(TurnCompleted(context=legacy, latency_ms=9, runtime=None))

    assert await _drain_outbound(bus) == []
