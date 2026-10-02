"""Event types for the message bus."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from nanobot.events import AgentEvent

# Optional ``OutboundMessage.metadata`` key for structured, channel-agnostic UI
# payloads. Value is JSON-serializable with at least ``kind``; rich clients may
# render it and other channels may ignore unknown keys.
OUTBOUND_META_AGENT_UI = "_agent_ui"
OUTBOUND_META_REACTION = "_reaction"

# Internal-only inbound metadata used by in-process channels to ask the agent
# loop to update runtime state without going through a user session.
INBOUND_META_RUNTIME_CONTROL = "_runtime_control"
RUNTIME_CONTROL_ACK = "_ack"
RUNTIME_CONTROL_MCP_RELOAD = "mcp_reload"
# Fork additions (WebUI integration, pinned upstream bus/events.py d0d0a44e):
# user-shell turns bypass the chat composer path; the loop answers them like
# ordinary user input but tags the turn so session history stays attributed.
INBOUND_META_USER_SHELL = "_user_shell"
RUNTIME_CONTROL_SESSION_DISCARD = "session_discard"
RUNTIME_CONTROL_IMAGE_GENERATION_RELOAD = "image_generation_reload"


# A button is either a legacy plain label string (rendered with the label as
# callback value where the channel supports it) or a structured spec with a
# display label and an opaque callback_value (used by ask_user so the visible
# label is never the callback payload).
ButtonSpec = str | dict[str, str]


@dataclass
class InboundMessage:
    """Message received from a chat channel."""

    channel: str  # telegram, discord, slack, whatsapp
    sender_id: str  # User identifier
    chat_id: str  # Chat/channel identifier
    content: str  # Message text
    timestamp: datetime = field(default_factory=datetime.now)
    media: list[str] = field(default_factory=list)  # Media URLs
    metadata: dict[str, Any] = field(default_factory=dict)  # Channel-specific data
    session_key_override: str | None = None  # Optional override for thread-scoped sessions
    # Fork additions (WebUI integration, pinned upstream bus/events.py d0d0a44e).
    # Defaulted; the fork loop does not enforce them yet — ported WebUI code
    # sets them and temporary-chat operations refuse until semantics exist.
    require_existing_session: bool = False
    input_role: Literal["user", "system"] | None = None

    @property
    def session_key(self) -> str:
        """Unique key for session identification."""
        return self.session_key_override or f"{self.channel}:{self.chat_id}"

    @property
    def is_user_input(self) -> bool:
        """Whether this message should enter the conversation as user input."""
        if self.input_role is not None:
            return self.input_role == "user"
        return self.channel != "system"

@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Transport acknowledgement, not a claim that a person read the message."""

    status: Literal["queued", "delivered", "failed", "unknown", "suppressed"]
    error: str | None = None



@dataclass
class OutboundMessage:
    """Message to send to a chat channel.

    ``metadata`` can carry routing (``message_id``, …), trace flags (``_progress``),
    and optional ``OUTBOUND_META_AGENT_UI`` blobs for rich clients; non-WebUI
    channels may ignore unknown keys.
    """

    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    buttons: list[list[ButtonSpec]] = field(default_factory=list)
    # Typed outbound event carried for event-aware channels (WebUI). Defaulted
    # so every existing constructor and consumer is unaffected; fork turns
    # leave it None and keep their metadata-flag dialect.
    event: "AgentEvent | None" = None
    # In-process acknowledgement; never copied into wire metadata or history.
    delivery: asyncio.Future[DeliveryResult] | None = field(default=None, repr=False, compare=False)
