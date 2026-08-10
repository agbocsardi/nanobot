from unittest.mock import AsyncMock

import pytest

from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.reaction import ReactionTool
from nanobot.bus.events import OUTBOUND_META_REACTION


@pytest.mark.asyncio
async def test_reaction_tool_targets_current_message() -> None:
    send = AsyncMock()
    tool = ReactionTool(send_callback=send)
    tool.set_context(RequestContext(channel="telegram", chat_id="123", message_id="42"))

    result = await tool.execute(emoji="👍")

    assert result == "Queued reaction action (set 👍) for message 42"
    msg = send.await_args.args[0]
    assert msg.channel == "telegram"
    assert msg.chat_id == "123"
    assert msg.content == ""
    assert msg.metadata[OUTBOUND_META_REACTION] == {"message_id": "42", "emoji": "👍"}


@pytest.mark.asyncio
async def test_reaction_tool_can_clear_explicit_message() -> None:
    send = AsyncMock()
    tool = ReactionTool(send_callback=send)
    tool.set_context(RequestContext(channel="telegram", chat_id="123", message_id="42"))

    result = await tool.execute(emoji="", message_id="99")

    assert result == "Queued reaction action (clear) for message 99"
    assert send.await_args.args[0].metadata[OUTBOUND_META_REACTION] == {
        "message_id": "99",
        "emoji": "",
    }


@pytest.mark.asyncio
async def test_reaction_tool_requires_message_id() -> None:
    tool = ReactionTool(send_callback=AsyncMock())
    tool.set_context(RequestContext(channel="telegram", chat_id="123"))

    assert await tool.execute(emoji="👍") == "Error: No target message ID for reaction"


@pytest.mark.asyncio
async def test_reaction_tool_rejects_unsupported_channel() -> None:
    send = AsyncMock()
    tool = ReactionTool(send_callback=send)
    tool.set_context(RequestContext(channel="discord", chat_id="123", message_id="42"))

    result = await tool.execute(emoji="👍")

    assert result == "Error: Reactions are currently supported only on Telegram"
    send.assert_not_awaited()
