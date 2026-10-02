"""Synthetic no-LLM seam test: fork RuntimeEventBus -> bridge -> coordinator.

Proves the narrow compatibility seam: the fork's RuntimeEventBus publications
are translated by the runtime event bridge into WebUI-local AgentEvents that
the ported WebuiTurnCoordinator consumes on the MessageBus, producing the
expected typed outbound events — with no fabricated usage/outcome evidence and
no edits to the fork's runtime-event module.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.outbound_events import (
    GoalStateSyncEvent,
    GoalStatusEvent,
    RuntimeModelUpdatedEvent,
    TurnEndEvent,
)
from nanobot.bus.queue import MessageBus
from nanobot.bus.runtime_events import (
    GoalStateChanged,
    RuntimeEventBus,
    RuntimeEventContext,
    RuntimeModelChanged,
    SessionTurnStarted,
    TurnCompleted,
    TurnRunStatusChanged,
)
from nanobot.session.manager import SessionManager
from nanobot.session.webui_turns import WebuiTurnCoordinator
from nanobot.utils.llm_runtime import LLMRuntime
from nanobot.webui.fork_events import TurnCompleted as LocalTurnCompleted
from nanobot.webui.outbound_wire import encode_turn_end
from nanobot.webui.runtime_event_bridge import RuntimeEventBridge


def _ctx(session_key: str = "webui:c1", metadata: dict[str, Any] | None = None) -> RuntimeEventContext:
    return RuntimeEventContext(
        channel="websocket",
        chat_id="c1",
        session_key=session_key,
        metadata={"webui": True} if metadata is None else metadata,
    )


async def _drain_outbound(bus: MessageBus) -> list[OutboundMessage]:
    out: list[OutboundMessage] = []
    while True:
        try:
            out.append(bus.outbound.get_nowait())
        except asyncio.QueueEmpty:
            return out


def _wire(tmp_path: Any) -> tuple[RuntimeEventBus, MessageBus]:
    """Fork runtime bus + bridge + subscribed coordinator on one MessageBus."""
    bus = MessageBus()
    sessions = SessionManager(workspace=tmp_path)
    coordinator = WebuiTurnCoordinator(
        bus=bus,
        sessions=sessions,
        schedule_background=lambda task: asyncio.get_running_loop().create_task(task),
    )
    coordinator.subscribe()
    runtime_bus = RuntimeEventBus()
    RuntimeEventBridge(runtime_bus, bus).attach()
    return runtime_bus, bus


@pytest.mark.asyncio
async def test_bridge_translates_fork_turn_lifecycle(tmp_path) -> None:
    runtime_bus, bus = _wire(tmp_path)

    await runtime_bus.publish(SessionTurnStarted(context=_ctx()))
    await runtime_bus.publish(TurnRunStatusChanged(context=_ctx(), status="running", started_at=123.0))
    await runtime_bus.publish(TurnCompleted(context=_ctx(), latency_ms=42, runtime=None))

    events = [m.event for m in await _drain_outbound(bus)]
    assert any(isinstance(e, GoalStatusEvent) and e.status == "running" for e in events)
    turn_ends = [e for e in events if isinstance(e, TurnEndEvent)]
    assert len(turn_ends) == 1
    # No usage/failure evidence exists on the fork emitter; none may be invented.
    assert turn_ends[0].usage is None
    assert turn_ends[0].round_usages == ()
    assert turn_ends[0].failure_kind is None
    payload = encode_turn_end("c1", turn_ends[0], {})
    assert "outcome" not in payload
    assert "usage" not in payload
    assert payload["latency_ms"] == 42


@pytest.mark.asyncio
async def test_bridge_goal_state_and_model_changed(tmp_path) -> None:
    runtime_bus, bus = _wire(tmp_path)

    await runtime_bus.publish(
        GoalStateChanged(
            context=_ctx(),
            session_metadata={"goal_state": {"status": "active", "objective": "write tests"}},
        )
    )
    await runtime_bus.publish(RuntimeModelChanged(model="m1", model_preset=None))

    events = [m.event for m in await _drain_outbound(bus)]
    sync = [e for e in events if isinstance(e, GoalStateSyncEvent)]
    assert sync and sync[0].goal_state["active"] is True
    assert sync[0].goal_state["objective"] == "write tests"
    model = [e for e in events if isinstance(e, RuntimeModelUpdatedEvent)]
    assert model and model[0].model == "m1" and model[0].model_preset is None


@pytest.mark.asyncio
async def test_coordinator_accepts_fork_shaped_runtime(tmp_path) -> None:
    """The bridge's local TurnCompleted tolerates the fork LLMRuntime shape."""
    bus = MessageBus()
    sessions = SessionManager(workspace=tmp_path)
    coordinator = WebuiTurnCoordinator(
        bus=bus,
        sessions=sessions,
        schedule_background=lambda task: asyncio.get_running_loop().create_task(task),
    )
    coordinator.subscribe()

    # Fork LLMRuntime carries only provider/model — no model_preset/window.
    fork_runtime = LLMRuntime(provider=object(), model="fork-model")  # type: ignore[arg-type]
    # metadata without webui=True keeps the title-generation path dormant.
    event = LocalTurnCompleted(context=_ctx(metadata={}), latency_ms=7, runtime=fork_runtime)
    await bus.publish(event)

    events = [m.event for m in await _drain_outbound(bus)]
    turn_ends = [e for e in events if isinstance(e, TurnEndEvent)]
    assert len(turn_ends) == 1
    assert turn_ends[0].context_window_tokens is None  # fork runtime has no window field


@pytest.mark.asyncio
async def test_legacy_websocket_turns_do_not_emit_webui_frames(tmp_path) -> None:
    """Legacy websocket: turns share the channel but must stay out of WebUI state."""
    runtime_bus, bus = _wire(tmp_path)

    legacy_ctx = RuntimeEventContext(
        channel="websocket",
        chat_id="legacy-1",
        session_key="websocket:legacy-1",
        metadata={},
    )
    await runtime_bus.publish(SessionTurnStarted(context=legacy_ctx))
    await runtime_bus.publish(
        TurnRunStatusChanged(context=legacy_ctx, status="running", started_at=1.0)
    )
    await runtime_bus.publish(TurnCompleted(context=legacy_ctx, latency_ms=9, runtime=None))

    assert await _drain_outbound(bus) == []


@pytest.mark.asyncio
async def test_bridge_detach_stops_translation(tmp_path) -> None:
    runtime_bus, bus = _wire(tmp_path)
    # Re-attach to get the detach handle (the _wire attach is anonymous).
    runtime_bus2 = RuntimeEventBus()
    detach = RuntimeEventBridge(runtime_bus2, bus).attach()
    detach()

    await runtime_bus2.publish(SessionTurnStarted(context=_ctx()))
    await asyncio.sleep(0)
    assert await _drain_outbound(bus) == []
