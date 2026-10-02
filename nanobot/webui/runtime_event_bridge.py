"""Translate fork RuntimeEventBus publications into MessageBus AgentEvents.

The fork's turn machinery emits state on the dedicated
:class:`~nanobot.bus.runtime_events.RuntimeEventBus` (fork dataclasses); the
ported WebUI coordinator consumes AgentEvent-shaped runtime events on the
:class:`~nanobot.bus.queue.MessageBus`. This bridge is the single translation
point: the fork's emitters (agent loop, long-task tools) and
``nanobot/bus/runtime_events.py`` stay untouched, and the coordinator works
against the WebUI-local target types in ``nanobot.webui.fork_events``.

Honesty note: the fork's ``TurnCompleted`` fires for both delivered and
errored turns with no outcome evidence and no usage payload. The bridge
forwards exactly what exists — latency, recorded runtime, context — and
leaves ``usage`` unset and ``outcome`` at its neutral default, so wire
``turn_end`` frames carry no fabricated success/failure distinction.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nanobot.bus.queue import MessageBus
from nanobot.bus.runtime_events import (
    GoalStateChanged as ForkGoalStateChanged,
)
from nanobot.bus.runtime_events import (
    RuntimeEventBus,
)
from nanobot.bus.runtime_events import (
    RuntimeModelChanged as ForkRuntimeModelChanged,
)
from nanobot.bus.runtime_events import (
    SessionTurnStarted as ForkSessionTurnStarted,
)
from nanobot.bus.runtime_events import (
    TurnCompleted as ForkTurnCompleted,
)
from nanobot.bus.runtime_events import (
    TurnRunStatusChanged as ForkTurnRunStatusChanged,
)
from nanobot.webui.fork_events import (
    GoalStateChanged,
    RuntimeEventContext,
    RuntimeModelChanged,
    SessionTurnStarted,
    TurnCompleted,
    TurnRunStatusChanged,
)


def _context(ctx: Any) -> RuntimeEventContext:
    """Map a fork RuntimeEventContext onto the WebUI-local target context."""
    return RuntimeEventContext(
        channel=ctx.channel,
        chat_id=ctx.chat_id,
        session_key=ctx.session_key,
        metadata=dict(ctx.metadata or {}),
    )


def _runtime_fields(runtime: Any) -> dict[str, Any]:
    """Forward only the runtime fields that actually exist on the fork shape.

    The fork ``LLMRuntime`` carries ``provider``/``model``; upstream builds add
    ``model_preset``/``context_window_tokens``. Read both defensively so the
    bridge works with either shape without inventing values.
    """
    if runtime is None:
        return {"runtime": None}
    return {
        "runtime": runtime,
        "model_preset": getattr(runtime, "model_preset", None),
        "context_window_tokens": getattr(runtime, "context_window_tokens", None),
    }


class RuntimeEventBridge:
    """Republish fork runtime events as WebUI-local AgentEvents on the bus."""

    def __init__(self, runtime_bus: RuntimeEventBus, bus: MessageBus) -> None:
        self._runtime_bus = runtime_bus
        self._bus = bus

    def attach(self) -> Callable[[], None]:
        """Subscribe on the fork bus; return an idempotent detach."""
        unsubscribes = [
            self._runtime_bus.subscribe(self._on_turn_started, ForkSessionTurnStarted),
            self._runtime_bus.subscribe(self._on_run_status, ForkTurnRunStatusChanged),
            self._runtime_bus.subscribe(self._on_turn_completed, ForkTurnCompleted),
            self._runtime_bus.subscribe(self._on_goal_state, ForkGoalStateChanged),
            self._runtime_bus.subscribe(self._on_model_changed, ForkRuntimeModelChanged),
        ]

        def _detach() -> None:
            for unsubscribe in reversed(unsubscribes):
                unsubscribe()

        return _detach

    async def _on_turn_started(self, event: ForkSessionTurnStarted) -> None:
        await self._bus.publish(SessionTurnStarted(context=_context(event.context)))

    async def _on_run_status(self, event: ForkTurnRunStatusChanged) -> None:
        await self._bus.publish(TurnRunStatusChanged(
            context=_context(event.context),
            status=event.status,
            started_at=event.started_at,
        ))

    async def _on_turn_completed(self, event: ForkTurnCompleted) -> None:
        await self._bus.publish(TurnCompleted(
            context=_context(event.context),
            latency_ms=event.latency_ms,
            **_runtime_fields(event.runtime),
        ))

    async def _on_goal_state(self, event: ForkGoalStateChanged) -> None:
        await self._bus.publish(GoalStateChanged(
            context=_context(event.context),
            session_metadata=dict(event.session_metadata or {}),
        ))

    async def _on_model_changed(self, event: ForkRuntimeModelChanged) -> None:
        await self._bus.publish(RuntimeModelChanged(
            model=event.model,
            model_preset=event.model_preset,
        ))
