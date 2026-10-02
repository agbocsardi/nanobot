"""Compose the WebUI turn lifecycle on the running gateway.

The fork splits the WebUI turn lifecycle across two buses:

* the agent loop publishes ``RuntimeEventBus`` records (session turn started,
  turn run status, turn completed, goal/model changes), and
* the WebUI client consumes typed outbound frames (``turn_end``,
  ``goal_status``, model updates) projected onto the websocket wire.

``RuntimeEventBridge`` and ``WebuiTurnCoordinator`` implement both halves, but
neither installs itself. A gateway that skips this composition leaves the
runtime event bus without subscribers: replies still arrive, yet ``turn_end``
never does, so the client stays in the streaming state forever.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from nanobot.bus.queue import MessageBus
from nanobot.bus.runtime_events import RuntimeEventBus
from nanobot.session.webui_turns import WebuiTurnCoordinator
from nanobot.webui.runtime_event_bridge import RuntimeEventBridge

__all__ = ["attach_webui_turn_lifecycle"]


def attach_webui_turn_lifecycle(
    bus: MessageBus,
    runtime_events: RuntimeEventBus,
    sessions: Any,
    *,
    schedule_background: Callable[[Awaitable[None]], None] | None = None,
) -> Callable[[], None]:
    """Wire the WebUI turn lifecycle and return a teardown callable.

    Call from inside the running event loop. The returned callable detaches the
    bridge and unsubscribes the coordinator; call it when the gateway stops.
    """
    schedule: Callable[[Awaitable[None]], None]
    if schedule_background is None:

        def schedule(task: Awaitable[None]) -> None:
            asyncio.get_running_loop().create_task(task)

    else:
        schedule = schedule_background

    coordinator = WebuiTurnCoordinator(
        bus=bus,
        sessions=sessions,
        schedule_background=schedule,
    )
    unsubscribe = coordinator.subscribe()
    detach_bridge = RuntimeEventBridge(runtime_events, bus).attach()

    def detach() -> None:
        detach_bridge()
        unsubscribe()

    return detach
