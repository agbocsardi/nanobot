"""Tool for reacting to chat messages."""

from contextvars import ContextVar
from typing import Any, Awaitable, Callable

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import ContextAware, RequestContext
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.bus.events import OUTBOUND_META_REACTION, OutboundMessage


@tool_parameters(
    tool_parameters_schema(
        emoji=StringSchema(
            "Emoji reaction to set. Use an empty string to clear the bot's reaction."
        ),
        message_id=StringSchema(
            "Optional target message ID. Omit it to react to the current message."
        ),
        required=["emoji"],
    )
)
class ReactionTool(Tool, ContextAware):
    """Set or clear a reaction on a message in the active chat."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
    ) -> None:
        self._send_callback = send_callback
        self._channel: ContextVar[str] = ContextVar("reaction_channel", default="")
        self._chat_id: ContextVar[str] = ContextVar("reaction_chat_id", default="")
        self._message_id: ContextVar[str | None] = ContextVar(
            "reaction_message_id", default=None
        )

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(send_callback=ctx.bus.publish_outbound if ctx.bus else None)

    @property
    def name(self) -> str:
        return "react"

    @property
    def description(self) -> str:
        return (
            "Set or clear an emoji reaction on a message in the active chat. "
            "Use this sparingly for lightweight acknowledgement or emotional feedback. "
            "Omit message_id to target the message that triggered the current turn. "
            "Currently supported by Telegram."
        )

    def set_context(self, ctx: RequestContext) -> None:
        self._channel.set(ctx.channel)
        self._chat_id.set(ctx.chat_id)
        self._message_id.set(ctx.message_id)

    async def execute(
        self,
        emoji: str,
        message_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        channel = self._channel.get()
        chat_id = self._chat_id.get()
        target_message_id = message_id or self._message_id.get()
        if not channel or not chat_id:
            return "Error: No active channel/chat for reaction"
        if channel != "telegram":
            return "Error: Reactions are currently supported only on Telegram"
        if not target_message_id:
            return "Error: No target message ID for reaction"
        if not self._send_callback:
            return "Error: Reaction sending not configured"

        await self._send_callback(
            OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content="",
                metadata={
                    OUTBOUND_META_REACTION: {
                        "message_id": str(target_message_id),
                        "emoji": emoji,
                    }
                },
            )
        )
        action = "clear" if not emoji else f"set {emoji}"
        return f"Queued reaction action ({action}) for message {target_message_id}"
