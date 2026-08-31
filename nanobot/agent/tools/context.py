"""Runtime context for tool construction."""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nanobot.agent.tools.approval import ApprovalStore

_CURRENT_REQUEST_CONTEXT: ContextVar["RequestContext | None"] = ContextVar(
    "nanobot_tool_request_context",
    default=None,
)


@dataclass(frozen=True)
class RequestContext:
    """Per-request context injected into tools at message-processing time."""
    channel: str
    chat_id: str
    message_id: str | None = None
    session_key: str | None = None
    sender_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Session-scoped interactive approval store consulted by the policy engine
    # for ask outcomes. None outside interactive agent turns (e.g. standalone
    # registry use), where ask keeps blocking without an approval surface.
    approvals: "ApprovalStore | None" = None


@runtime_checkable
class ContextAware(Protocol):
    def set_context(self, ctx: RequestContext) -> None:
        ...


def bind_request_context(ctx: RequestContext) -> Token[RequestContext | None]:
    return _CURRENT_REQUEST_CONTEXT.set(ctx)


def reset_request_context(token: Token[RequestContext | None]) -> None:
    _CURRENT_REQUEST_CONTEXT.reset(token)


def current_request_context() -> RequestContext | None:
    return _CURRENT_REQUEST_CONTEXT.get()


def current_request_session_key() -> str | None:
    ctx = current_request_context()
    return ctx.session_key if ctx else None


@dataclass
class ToolContext:
    config: Any
    workspace: str
    bus: Any | None = None
    subagent_manager: Any | None = None
    cron_service: Any | None = None
    sessions: Any | None = None
    file_state_store: Any = field(default=None)
    provider_snapshot_loader: Callable[[], Any] | None = None
    model_presets: dict[str, Any] | None = None
    image_generation_provider_configs: dict[str, Any] | None = None
    timezone: str = "UTC"
    workspace_sandbox: Any | None = None
    runtime_events: Any | None = None
    # Late-bound Discord runtime handle; created by the gateway before
    # AgentLoop construction and bound to the live DiscordChannel after
    # ChannelManager starts. ``DiscordHistoryTool`` resolves the connected
    # client through it at execution time. None outside the gateway.
    discord_runtime_handle: Any | None = None
