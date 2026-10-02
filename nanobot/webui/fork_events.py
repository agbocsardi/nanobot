"""WebUI-local runtime event types for the ported turn coordinator.

This fork publishes turn/run/model/goal state on its own
:class:`~nanobot.bus.runtime_events.RuntimeEventBus` with fork dataclasses.
The WebUI turn coordinator (ported from the pinned WebUI surface) subscribes
to AgentEvent-shaped runtime events on the :class:`~nanobot.bus.queue.MessageBus`.
These dataclasses are the explicit, coherent translation targets: they mirror
the pinned upstream shapes (source commit d0d0a44e, ``nanobot/bus/runtime_events.py``)
so the coordinator code works unchanged, while the fork's own
``nanobot/bus/runtime_events.py`` stays untouched.

``nanobot.webui.runtime_event_bridge`` is the only publisher of these types;
nothing here is emitted by the fork loop itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from nanobot.events import AgentEvent

if TYPE_CHECKING:
    from nanobot.providers.base import LLMUsage
    from nanobot.utils.llm_runtime import LLMRuntime


@dataclass(frozen=True)
class RuntimeEventContext:
    """Routing context common to turn-scoped runtime events."""

    channel: str
    chat_id: str
    session_key: str
    metadata: dict[str, Any] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionTurnStarted(AgentEvent):
    """A user/system turn has loaded its session and is about to build context."""

    context: RuntimeEventContext


@dataclass(frozen=True)
class UserInputAccepted(AgentEvent):
    """User input was accepted for dispatch or injection into a session.

    The fork loop never emits this; the coordinator's handler stays wired for
    upstream parity but is dormant here.
    """

    context: RuntimeEventContext
    content: str


@dataclass(frozen=True)
class TurnRuntimeAdmitted(AgentEvent):
    """The model runtime selected for one admitted turn.

    The fork loop never emits this; per-turn model chips fall back to
    ``RuntimeModelChanged`` (rendered as ``RuntimeModelUpdatedEvent``).
    """

    context: RuntimeEventContext
    runtime: LLMRuntime


@dataclass(frozen=True)
class TurnRunStatusChanged(AgentEvent):
    """Visible run status changed for a turn."""

    context: RuntimeEventContext
    status: str
    started_at: float | None = None


@dataclass(frozen=True)
class TurnCompleted(AgentEvent):
    """A turn has delivered its final user-visible response.

    The fork emits this on both delivered and errored turns without an
    outcome distinction, so ``usage``/``outcome`` evidence is simply absent;
    the bridge never fabricates it.
    """

    context: RuntimeEventContext
    latency_ms: int | None = None
    runtime: LLMRuntime | None = None
    usage: LLMUsage | None = None
    # Logical model rounds in display order; recovery dispatches are aggregated.
    round_usages: tuple[LLMUsage, ...] = ()
    outcome: str = "completed"
    failure_kind: str | None = None
    failure_error_kind: str | None = None
    failure_attempts: int | None = None


@dataclass(frozen=True)
class SessionTurnPersisted(AgentEvent):
    """A completed turn has been written to local session storage.

    The fork loop never emits this; kept for coordinator import parity.
    """

    context: RuntimeEventContext
    turn_id: str
    sender_id: str


@dataclass(frozen=True)
class GoalStateChanged(AgentEvent):
    """A session's sustained-goal state changed."""

    context: RuntimeEventContext
    session_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeModelChanged(AgentEvent):
    """The active runtime model/preset changed."""

    model: str
    model_preset: str | None
