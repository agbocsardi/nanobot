"""Connection-scoped chat ownership for the WebSocket channel.

Chat-ID syntax checks are not ownership: a connection may only attach to and
message chats it registered itself, and a reconnect cannot reclaim arbitrary
IDs.
"""

from __future__ import annotations

import json

import pytest

from nanobot.bus.events import DeliveryResult
from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket import WebSocketChannel


class FakeConnection:
    """Minimal stand-in for a websockets ServerConnection."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.sent: list[dict] = []
        self.remote_address = ("127.0.0.1", 12345)
        self._fail_with = fail_with

    async def send(self, raw: str) -> None:
        if self._fail_with is not None:
            raise self._fail_with
        self.sent.append(json.loads(raw))



def _make_channel() -> WebSocketChannel:
    return WebSocketChannel(WebSocketChannel.default_config(), MessageBus())


async def _connect(channel: WebSocketChannel, connection: FakeConnection) -> str:
    """Simulate the ready handshake: a fresh default chat owned by the connection."""
    default_chat = f"default-{id(connection):x}"
    channel._conn_default[connection] = default_chat
    channel._attach(connection, default_chat)
    await connection.send(json.dumps({
        "event": "ready", "chat_id": default_chat, "client_id": "tester",
    }))
    return default_chat


async def _dispatch(channel: WebSocketChannel, connection: FakeConnection, envelope: dict) -> None:
    await channel._dispatch_envelope(connection, "tester", envelope)


@pytest.mark.asyncio
async def test_new_chat_creates_owned_chat():
    channel = _make_channel()
    conn = FakeConnection()
    default_chat = await _connect(channel, conn)

    await _dispatch(channel, conn, {"type": "new_chat"})

    attached = [e for e in conn.sent if e.get("event") == "attached"]
    assert len(attached) == 1
    new_chat = attached[0]["chat_id"]
    assert new_chat != default_chat
    assert new_chat in channel._conn_chats[conn]
    assert conn in channel._subs[new_chat]


@pytest.mark.asyncio
async def test_attach_to_foreign_chat_is_rejected():
    channel = _make_channel()
    conn_a, conn_b = FakeConnection(), FakeConnection()
    await _connect(channel, conn_a)
    await _connect(channel, conn_b)

    await _dispatch(channel, conn_a, {"type": "new_chat"})
    foreign_chat = [
        e for e in conn_a.sent if e.get("event") == "attached"
    ][0]["chat_id"]

    await _dispatch(channel, conn_b, {"type": "attach", "chat_id": foreign_chat})

    errors = [e for e in conn_b.sent if e.get("event") == "error"]
    assert len(errors) == 1
    assert errors[0]["detail"] == "unknown chat_id"
    assert foreign_chat not in channel._conn_chats[conn_b]
    # Ownership is untouched: only conn_a receives for that chat.
    assert channel._subs[foreign_chat] == {conn_a}


@pytest.mark.asyncio
async def test_message_to_foreign_chat_is_not_published():
    channel = _make_channel()
    conn_a, conn_b = FakeConnection(), FakeConnection()
    await _connect(channel, conn_a)
    await _connect(channel, conn_b)

    await _dispatch(channel, conn_a, {"type": "new_chat"})
    foreign_chat = [
        e for e in conn_a.sent if e.get("event") == "attached"
    ][0]["chat_id"]

    await _dispatch(channel, conn_b, {
        "type": "message", "chat_id": foreign_chat, "content": "intrusion",
    })

    errors = [e for e in conn_b.sent if e.get("event") == "error"]
    assert errors and errors[0]["detail"] == "unknown chat_id"
    assert channel.bus.inbound.qsize() == 0


@pytest.mark.asyncio
async def test_message_to_own_chat_is_published():
    channel = _make_channel()
    conn = FakeConnection()
    await _connect(channel, conn)

    await _dispatch(channel, conn, {"type": "new_chat"})
    chat_id = [e for e in conn.sent if e.get("event") == "attached"][0]["chat_id"]
    await _dispatch(channel, conn, {
        "type": "message", "chat_id": chat_id, "content": "hello",
    })

    msg = channel.bus.inbound.get_nowait()
    assert msg.chat_id == chat_id
    assert msg.content == "hello"


@pytest.mark.asyncio
async def test_invalid_chat_id_still_rejected_on_syntax():
    channel = _make_channel()
    conn = FakeConnection()
    await _connect(channel, conn)

    await _dispatch(channel, conn, {"type": "message", "chat_id": "bad id!", "content": "x"})

    errors = [e for e in conn.sent if e.get("event") == "error"]
    assert errors and errors[0]["detail"] == "invalid chat_id"
    assert channel.bus.inbound.qsize() == 0


@pytest.mark.asyncio
async def test_reconnect_cannot_reclaim_abandoned_chat():
    channel = _make_channel()
    conn_a = FakeConnection()
    await _connect(channel, conn_a)
    await _dispatch(channel, conn_a, {"type": "new_chat"})
    chat_id = [e for e in conn_a.sent if e.get("event") == "attached"][0]["chat_id"]

    # The connection drops; its registrations are cleaned up.
    channel._cleanup_connection(conn_a)

    conn_a2 = FakeConnection()
    await _connect(channel, conn_a2)
    await _dispatch(channel, conn_a2, {"type": "attach", "chat_id": chat_id})

    errors = [e for e in conn_a2.sent if e.get("event") == "error"]
    assert errors and errors[0]["detail"] == "unknown chat_id"
    assert chat_id not in channel._subs
    assert channel.bus.inbound.qsize() == 0


@pytest.mark.asyncio
async def test_attach_to_own_chat_is_idempotent():
    channel = _make_channel()
    conn = FakeConnection()
    default_chat = await _connect(channel, conn)

    await _dispatch(channel, conn, {"type": "attach", "chat_id": default_chat})

    attached = [e for e in conn.sent if e.get("event") == "attached"]
    assert len(attached) == 1
    assert attached[0]["chat_id"] == default_chat


# ---------------------------------------------------------------------------
# send() — truthful DeliveryResult for websocket transport
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_without_subscriber_fails():
    channel = _make_channel()
    from nanobot.bus.events import OutboundMessage

    result = await channel.send(OutboundMessage(
        channel="websocket", chat_id="nobody", content="hi",
    ))
    assert result.status == "failed"


@pytest.mark.asyncio
async def test_send_delivered_on_successful_frame_write():
    channel = _make_channel()
    conn = FakeConnection()
    channel._attach(conn, "chat-1")
    from nanobot.bus.events import OutboundMessage

    result = await channel.send(OutboundMessage(
        channel="websocket", chat_id="chat-1", content="hi",
    ))
    assert result.status == "delivered"
    assert conn.sent[0]["text"] == "hi"


@pytest.mark.asyncio
async def test_send_dead_connection_reports_failed():
    from websockets.exceptions import ConnectionClosed

    channel = _make_channel()
    conn = FakeConnection(fail_with=ConnectionClosed(None, None))
    channel._attach(conn, "chat-1")
    from nanobot.bus.events import OutboundMessage

    result = await channel.send(OutboundMessage(
        channel="websocket", chat_id="chat-1", content="hi",
    ))
    assert result.status == "failed"
    assert "chat-1" not in channel._subs


@pytest.mark.asyncio
async def test_send_partial_failure_is_unknown_not_retryable_duplicate():
    channel = _make_channel()
    ok_conn = FakeConnection()
    bad_conn = FakeConnection(fail_with=RuntimeError("socket exploded"))
    channel._attach(ok_conn, "chat-1")
    channel._attach(bad_conn, "chat-1")
    from nanobot.bus.events import OutboundMessage

    result = await channel.send(OutboundMessage(
        channel="websocket", chat_id="chat-1", content="hi",
    ))
    assert isinstance(result, DeliveryResult)
    assert result.status == "unknown"
    # The successfully-served subscriber got exactly one copy.
    assert len(ok_conn.sent) == 1
