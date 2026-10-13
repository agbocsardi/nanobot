"""Ingress TTL and send attribution regressions for issue #35."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram import (
    TELEGRAM_INGRESS_IDENTITY_TTL_S,
    TelegramChannel,
    TelegramConfig,
)


def test_callback_identity_ttl(monkeypatch):
    channel = TelegramChannel(TelegramConfig(enabled=True, token="123:abc"), MessageBus())
    now = 100.0
    monkeypatch.setattr("nanobot.channels.telegram.time.monotonic", lambda: now)
    update = SimpleNamespace(update_id=1, callback_query=SimpleNamespace(id="callback1"))
    assert channel._claim_update(update)
    assert not channel._claim_update(update)
    update.update_id = 2
    assert not channel._claim_update(update)
    now += TELEGRAM_INGRESS_IDENTITY_TTL_S
    assert channel._claim_update(update)


def test_successive_edits_have_distinct_identities():
    channel = TelegramChannel(TelegramConfig(enabled=True, token="123:abc"), MessageBus())
    update = SimpleNamespace(
        update_id=1, edited_message=SimpleNamespace(chat_id=123, message_id=7)
    )
    assert channel._claim_update(update)
    assert not channel._claim_update(update)
    update.update_id = 2
    assert channel._claim_update(update)


@pytest.mark.asyncio
@pytest.mark.parametrize("rich", [False, True])
async def test_send_logs_platform_message_id(rich):
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", rich_messages=rich), MessageBus()
    )
    channel.logger = Mock()
    bot = SimpleNamespace(
        do_api_request=AsyncMock(return_value={"message_id": 456}),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=456)),
    )
    channel._app = SimpleNamespace(bot=bot)
    result = await channel.send(
        OutboundMessage(channel="telegram", chat_id="123", content="hello")
    )
    assert result.status == "delivered"
    logs = [call.args[0].format(*call.args[1:]) for call in channel.logger.info.call_args_list]
    assert any("chat_id=123 message_id=456" in line for line in logs)
    if rich:
        bot.do_api_request.assert_awaited_once()
        bot.send_message.assert_not_awaited()
    else:
        bot.send_message.assert_awaited_once()
