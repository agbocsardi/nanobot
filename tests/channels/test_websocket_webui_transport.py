"""WebSocket transport tests for the issue #38 WebUI integration.

Two tiers:

- Legacy preservation (no gate): the gateway-less channel must keep the
  fork's wire behavior — metadata-marker streaming for the fork manager's
  positional calls, truthful DeliveryResult, chat_id='*' runtime-model
  broadcast, and a real listener round-trip on an ephemeral port.
- Audience/binding invariants (gated on the backend WebUI foundation):
  per-connection audience dispatch, single-audience chat binding at every
  transport bind/mint path, and explicit ``require_existing_session``
  refusal. The fake gateway here only stands in for the transport's own
  seams (endpoint webui_connections, side-effect-free session_exists); the
  real router/projector modules are still constructed and exercised.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import uuid

import pytest

from nanobot.bus.events import DeliveryResult, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket import WebSocketChannel, WebSocketConfig
from nanobot.channels.websocket import runtime as websocket_runtime


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


def _make_channel(**config_overrides) -> WebSocketChannel:
    config = WebSocketConfig(**{"websocket_requires_token": False, **config_overrides})
    return WebSocketChannel(config, MessageBus())


async def _attach_default(channel: WebSocketChannel, connection: FakeConnection) -> str:
    chat = f"chat-{id(connection):x}"
    channel._conn_default[connection] = chat
    channel._attach(connection, chat)
    return chat


async def _drain() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Legacy preservation: metadata-marker streaming for the fork manager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_markers_in_metadata_drive_kwargs_signature():
    """Fork manager calls send_delta(chat_id, delta, metadata) positionally;
    _stream_id/_stream_end in metadata must not be swallowed by the
    upstream kwargs (which default to None/False)."""
    channel = _make_channel()
    conn = FakeConnection()
    channel._attach(conn, "c1")

    await channel.send_delta("c1", "hel", {"_stream_id": "s1"})
    await channel.send_delta("c1", "lo", {"_stream_id": "s1"})
    await channel.send_delta("c1", "", {"_stream_id": "s1", "_stream_end": True})

    assert [e["event"] for e in conn.sent] == ["delta", "delta", "stream_end"]
    assert conn.sent[0]["stream_id"] == "s1"
    assert conn.sent[1]["text"] == "lo"
    # stream_end carries the buffered text and clears the buffer.
    assert conn.sent[2]["text"] == "hello"
    assert channel._stream_text_buffers == {}


@pytest.mark.asyncio
async def test_stream_kwargs_take_precedence_over_metadata():
    channel = _make_channel()
    conn = FakeConnection()
    channel._attach(conn, "c1")

    await channel.send_delta("c1", "x", {"_stream_id": "meta"}, stream_id="kw")
    await channel.send_delta("c1", "x", {}, stream_end=True)

    assert conn.sent[0]["stream_id"] == "kw"
    assert conn.sent[1]["event"] == "stream_end"


@pytest.mark.asyncio
async def test_reasoning_markers_flow_via_metadata():
    channel = _make_channel()
    conn = FakeConnection()
    channel._attach(conn, "c1")

    await channel.send_reasoning_delta("c1", "think", {"_stream_id": "r1"})
    await channel.send_reasoning_end("c1", {"_stream_id": "r1"})

    assert conn.sent[0]["event"] == "reasoning_delta"
    assert conn.sent[0]["stream_id"] == "r1"
    assert conn.sent[1]["event"] == "reasoning_end"
    assert conn.sent[1]["stream_id"] == "r1"


@pytest.mark.asyncio
async def test_stream_end_without_delivered_text_emits_no_text_field():
    channel = _make_channel()
    conn = FakeConnection()
    channel._attach(conn, "c1")

    await channel.send_delta("c1", "solo", {"_stream_id": "s9"})
    await channel.send_delta("c1", "", {"_stream_id": "s9", "_stream_end": True})

    assert conn.sent[1]["text"] == "solo"
    # A stream_end for a stream that never buffered anything has no text.
    await channel.send_delta("c1", "", {"_stream_id": "s10", "_stream_end": True})
    assert "text" not in conn.sent[2]


# ---------------------------------------------------------------------------
# Legacy preservation: DeliveryResult and chat_id='*' broadcast
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_model_broadcast_reaches_every_connection():
    """chat_id='*' runtime-model updates fan out to all open connections of
    both audiences (content-free, pre-existing fork behavior)."""
    channel = _make_channel()
    conn_a, conn_b = FakeConnection(), FakeConnection()
    channel._attach(conn_a, "c1")
    channel._attach(conn_b, "c2")

    result = await channel.send(OutboundMessage(
        channel="websocket",
        chat_id="*",
        content="",
        metadata={"_runtime_model_updated": True, "model": "m1", "model_preset": None},
    ))

    assert isinstance(result, DeliveryResult)
    assert result.status == "delivered"
    expected = {"event": "runtime_model_updated", "model_name": "m1"}
    assert conn_a.sent == [expected]
    assert conn_b.sent == [expected]


def test_publish_runtime_model_update_helper_unchanged():
    bus = MessageBus()
    websocket_runtime.publish_runtime_model_update(bus, "gpt-x", "fast")
    msg = bus.outbound.get_nowait()
    assert msg.chat_id == "*"
    assert msg.metadata["_runtime_model_updated"] is True
    assert msg.metadata["model"] == "gpt-x"


# ---------------------------------------------------------------------------
# Legacy preservation: discovery and import purity
# ---------------------------------------------------------------------------


def test_package_reexports_keep_channel_discovery_working():
    from nanobot.channels.registry import load_channel_class

    assert load_channel_class("websocket") is WebSocketChannel
    # Fork default: the channel is disabled unless configured on.
    assert WebSocketConfig().enabled is False
    assert WebSocketChannel.default_config()["enabled"] is False


def test_legacy_mode_imports_without_webui_package():
    """The channel module must not pull nanobot.webui at import time so the
    gateway-less channel keeps working without the WebUI stack installed."""
    code = (
        "import sys, nanobot.channels.websocket as m; "
        "assert not any(k.startswith('nanobot.webui') for k in sys.modules), "
        "sorted(k for k in sys.modules if k.startswith('nanobot.webui')); "
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_gatewayless_binding_guards_are_inert():
    channel = _make_channel()
    assert channel._chat_is_webui_bound("anything") is False
    assert channel._chat_is_legacy_bound("anything") is False
    # Without a gateway the session lookup is absent and the strict wrapper
    # falls back to the caller's default in both directions.
    assert channel._session_exists_strict("k", default=True) is True
    assert channel._session_exists_strict("k", default=False) is False


# ---------------------------------------------------------------------------
# Legacy preservation: real listener round-trip on an ephemeral port
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_listener_round_trip_on_ephemeral_port():
    import websockets

    channel = _make_channel(host="127.0.0.1", port=0)
    task = asyncio.create_task(channel.start())
    try:
        for _ in range(100):
            if channel._server is not None and channel._server.is_serving():
                break
            await asyncio.sleep(0.05)
        assert channel._server is not None and channel._server.is_serving()
        port = channel._server.sockets[0].getsockname()[1]

        async with websockets.connect(f"ws://127.0.0.1:{port}/?client_id=tester") as ws:
            ready = json.loads(await ws.recv())
            assert ready["event"] == "ready"
            assert ready["client_id"] == "tester"

            await ws.send(json.dumps({"type": "new_chat"}))
            attached = json.loads(await ws.recv())
            assert attached["event"] == "attached"
            chat_id = attached["chat_id"]

            await ws.send(json.dumps({"type": "message", "chat_id": chat_id, "content": "hi"}))
            for _ in range(100):
                if channel.bus.inbound.qsize():
                    break
                await asyncio.sleep(0.05)
            msg = channel.bus.inbound.get_nowait()
            assert msg.chat_id == chat_id
            assert msg.content == "hi"
            assert msg.session_key == f"websocket:{chat_id}"

            # Fork manager dialect: metadata-marker streaming reaches the client.
            await channel.send_delta(chat_id, "he", {"_stream_id": "s1"})
            await channel.send_delta(chat_id, "y", {"_stream_id": "s1", "_stream_end": True})
            delta = json.loads(await ws.recv())
            stream_end = json.loads(await ws.recv())
            assert delta["event"] == "delta" and delta["text"] == "he"
            assert stream_end["event"] == "stream_end" and stream_end["text"] == "hey"

            result = await channel.send(OutboundMessage(
                channel="websocket", chat_id=chat_id, content="hey",
            ))
            assert result.status == "delivered"
            final = json.loads(await ws.recv())
            assert final["event"] == "message" and final["text"] == "hey"
    finally:
        await channel.stop()
        await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# WebUI audience: gated on the backend foundation (router/projector/identity)
# ---------------------------------------------------------------------------


class _FakeTranscripts:
    """Records canonical turn events like the real WebUITranscriptRecorder."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict, str]] = []

    def prepare_event(self, chat_id, event, *, metadata=None, phase="", include_source=False,
                      transcript_overrides=None) -> bool:
        self.events.append((chat_id, event, phase))
        return True

    def prepare_and_append(self, chat_id, event, *, metadata=None, phase="", include_source=False,
                           transcript_overrides=None) -> bool:
        self.events.append((chat_id, event, phase))
        return True

    def prepare_and_append_stream_event(self, chat_id, event, *, completed_text=None, metadata=None,
                                        phase="", include_source=False) -> bool:
        self.events.append((chat_id, event, phase))
        return True


class _FakeTemporaryChats:
    def should_persist_transcript(self, chat_id: str) -> bool:
        return True


class _FakeEndpoint:
    def __init__(self) -> None:
        self.webui_connections: set = set()

    def discard_connection(self, connection) -> None:
        self.webui_connections.discard(connection)


class _FakeGateway:
    """Test double for the transport's own seams only: the endpoint's
    webui_connections set and the side-effect-free session_exists lookup.
    The real router/projector are constructed against it."""

    def __init__(self, persisted: set[str] | None = None) -> None:
        self.endpoint = _FakeEndpoint()
        self.http = None
        self.media = None
        self.ingress = None
        self.transcripts = _FakeTranscripts()
        self.workspaces = None
        self.temporary_chats = _FakeTemporaryChats()
        self.session_projection = None
        self.session_manager = None
        self._persisted = set(persisted or ())

    def session_exists(self, session_key: str) -> bool:
        return session_key in self._persisted


class _RecordingRouter:
    def __init__(self) -> None:
        self.dispatched: list[tuple[object, dict]] = []
        self.closed = False

    async def dispatch(self, connection, client_id, envelope) -> None:
        self.dispatched.append((connection, envelope))

    async def cleanup_connection(self, connection) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class _RecordingProjector:
    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []
        self.hydrated: list[str] = []

    async def send(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)

    async def hydrate(self, chat_id: str) -> None:
        self.hydrated.append(chat_id)


@pytest.fixture
def webui_stack():
    pytest.importorskip("nanobot.webui.session_identity")
    pytest.importorskip("nanobot.webui.inbound_commands")
    pytest.importorskip("nanobot.webui.outbound_projection")


def _gateway_channel(persisted: set[str] | None = None) -> tuple[WebSocketChannel, _FakeGateway]:
    gateway = _FakeGateway(persisted)
    channel = WebSocketChannel(WebSocketChannel.default_config(), MessageBus(), gateway=gateway)
    return channel, gateway


async def _add_webui_connection(
    channel: WebSocketChannel, gateway: _FakeGateway, connection: FakeConnection
) -> str:
    """Simulate a bootstrap-audience handshake: endpoint marks the connection."""
    gateway.endpoint.webui_connections.add(connection)
    chat = f"webui-{id(connection):x}"
    channel._conn_default[connection] = chat
    channel._attach(connection, chat)
    return chat


@pytest.mark.asyncio
async def test_audience_dispatch_by_connection_not_payload_flag(webui_stack):
    channel, gateway = _gateway_channel()
    router = _RecordingRouter()
    channel._commands = router

    legacy_conn = FakeConnection()
    webui_conn = FakeConnection()
    await _add_webui_connection(channel, gateway, webui_conn)

    # A legacy connection stays on the fork envelope branch even when the
    # envelope claims webui: true — the audience is never taken from payload.
    await channel._dispatch_envelope(legacy_conn, "c", {
        "type": "message", "chat_id": "foreign-id", "content": "hi", "webui": True,
    })
    assert router.dispatched == []
    errors = [e for e in legacy_conn.sent if e.get("event") == "error"]
    assert errors and errors[0]["detail"] == "unknown chat_id"
    assert channel.bus.inbound.qsize() == 0

    # A bootstrap-audience connection goes to the WebUI command router.
    await channel._dispatch_envelope(webui_conn, "c", {"type": "attach", "chat_id": "x"})
    assert len(router.dispatched) == 1
    assert router.dispatched[0][0] is webui_conn


@pytest.mark.asyncio
async def test_webui_attach_refuses_live_legacy_bound_chat(webui_stack):
    channel, gateway = _gateway_channel()
    legacy_conn = FakeConnection()
    channel._attach(legacy_conn, "live-legacy")
    webui_conn = FakeConnection()
    gateway.endpoint.webui_connections.add(webui_conn)

    with pytest.raises(websocket_runtime._CrossAudienceBindError):
        channel.webui_attach(webui_conn, "live-legacy")
    assert webui_conn not in channel._subs.get("live-legacy", set())


@pytest.mark.asyncio
async def test_webui_attach_rejects_legacy_persisted_but_allows_resume(webui_stack):
    from nanobot.webui.session_identity import webui_session_key

    # websocket:X persisted without webui:X: a webui bind must be refused
    # even with no live holders (post-restart collision case).
    channel, gateway = _gateway_channel(persisted={"websocket:seeded"})
    webui_conn = FakeConnection()
    gateway.endpoint.webui_connections.add(webui_conn)
    with pytest.raises(websocket_runtime._CrossAudienceBindError):
        channel.webui_attach(webui_conn, "seeded")
    assert "seeded" not in channel._subs

    # Both namespaces persisted and no live legacy holder: resume is allowed,
    # and the fan-out set stays purely webui (no legacy leakage).
    gateway._persisted.add(webui_session_key("seeded"))
    channel.webui_attach(webui_conn, "seeded")
    assert channel._subs["seeded"] == {webui_conn}


@pytest.mark.asyncio
async def test_legacy_ownership_never_claims_webui_bound_chat(webui_stack):
    from nanobot.webui.session_identity import webui_session_key

    channel, gateway = _gateway_channel(persisted={webui_session_key("webui-only")})
    legacy_conn = FakeConnection()
    # Simulate a stale registration; ownership must still refuse.
    channel._conn_chats[legacy_conn].add("webui-only")
    assert channel._owns_chat(legacy_conn, "webui-only") is False

    # A live webui subscriber also makes the id un-ownable for legacy.
    webui_conn = FakeConnection()
    gateway.endpoint.webui_connections.add(webui_conn)
    channel._attach(webui_conn, "live-webui")
    channel._conn_chats[legacy_conn].add("live-webui")
    assert channel._owns_chat(legacy_conn, "live-webui") is False


@pytest.mark.asyncio
async def test_legacy_mint_avoids_webui_bound_ids(webui_stack):
    from nanobot.webui.session_identity import webui_session_key

    collision = str(uuid.uuid4())
    fresh = str(uuid.uuid4())
    channel, gateway = _gateway_channel(persisted={webui_session_key(collision)})
    sequence = iter([collision, fresh])

    def fake_uuid4() -> uuid.UUID:
        return uuid.UUID(next(sequence))

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(websocket_runtime.uuid, "uuid4", fake_uuid4)
        assert channel._mint_chat_id() == fresh
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_cross_audience_bind_refusal_sends_unknown_chat_id_error(webui_stack):
    channel, gateway = _gateway_channel()
    legacy_conn = FakeConnection()
    channel._attach(legacy_conn, "owned-by-legacy")
    webui_conn = FakeConnection()
    gateway.endpoint.webui_connections.add(webui_conn)

    class _BindThenAttachRouter:
        async def dispatch(self, connection, client_id, envelope) -> None:
            # Simulates a missed router pre-check: the bind itself fails closed.
            channel.webui_attach(connection, envelope["chat_id"])
            await channel.webui_send_event(connection, "attached", chat_id=envelope["chat_id"])

        async def cleanup_connection(self, connection) -> None:
            return None

        async def close(self) -> None:
            return None

    channel._commands = _BindThenAttachRouter()
    await channel._dispatch_active_envelope(
        webui_conn, "c", {"type": "attach", "chat_id": "owned-by-legacy"}
    )

    events = [e.get("event") for e in webui_conn.sent]
    assert "attached" not in events
    assert webui_conn.sent[-1] == {"event": "error", "detail": "unknown chat_id"}
    assert webui_conn not in channel._subs.get("owned-by-legacy", set())
    # The legacy holder's subscription is untouched.
    assert channel._subs["owned-by-legacy"] == {legacy_conn}


@pytest.mark.asyncio
async def test_require_existing_session_is_refused_not_ignored(webui_stack):
    channel, gateway = _gateway_channel()
    webui_conn = FakeConnection()
    gateway.endpoint.webui_connections.add(webui_conn)

    class _TemporaryMessageRouter:
        async def dispatch(self, connection, client_id, envelope) -> None:
            await channel.webui_dispatch_message(
                sender_id=client_id,
                chat_id=envelope["chat_id"],
                content=envelope["content"],
                media=None,
                metadata={},
                is_dm=False,
                session_key=None,
                require_existing_session=True,
            )

        async def cleanup_connection(self, connection) -> None:
            return None

        async def close(self) -> None:
            return None

    channel._commands = _TemporaryMessageRouter()
    await channel._dispatch_active_envelope(
        webui_conn, "c", {"type": "message", "chat_id": "temp-id", "content": "hi"}
    )

    assert channel.bus.inbound.qsize() == 0
    assert webui_conn.sent == [
        {"event": "error", "detail": "temporary_chat_unavailable", "chat_id": "temp-id"}
    ]


@pytest.mark.asyncio
async def test_pending_legacy_reply_isolation_across_disconnect_and_restart(webui_stack):
    """The parent-mandated collision scenario, at binding level: a live
    legacy client owns X; a webui message to X must never join that fan-out;
    after the legacy disconnect, a webui bind is only allowed once the webui
    namespace actually holds X (resume), never on the legacy evidence."""
    from nanobot.webui.session_identity import webui_session_key

    channel, gateway = _gateway_channel()
    legacy_conn = FakeConnection()
    channel._conn_default[legacy_conn] = "seeded"
    channel._attach(legacy_conn, "seeded")
    webui_conn = FakeConnection()
    gateway.endpoint.webui_connections.add(webui_conn)

    with pytest.raises(websocket_runtime._CrossAudienceBindError):
        channel.webui_attach(webui_conn, "seeded")

    # Legacy disconnect: fan-out set empties.
    channel._cleanup_connection(legacy_conn)
    assert "seeded" not in channel._subs

    # Persisted websocket:X alone is still not webui evidence.
    gateway._persisted.add("websocket:seeded")
    with pytest.raises(websocket_runtime._CrossAudienceBindError):
        channel.webui_attach(webui_conn, "seeded")

    # webui:X persisted: resume allowed; the legacy connection is gone from
    # the fan-out, so pending or future legacy replies cannot reach it.
    gateway._persisted.add(webui_session_key("seeded"))
    channel.webui_attach(webui_conn, "seeded")
    assert channel._subs["seeded"] == {webui_conn}


@pytest.mark.asyncio
async def test_gateway_mode_send_keeps_legacy_direct_and_webui_queued(webui_stack):
    channel, gateway = _gateway_channel()
    projector = _RecordingProjector()
    channel._outbound = projector

    legacy_conn = FakeConnection()
    channel._attach(legacy_conn, "legacy-chat")
    webui_conn = FakeConnection()
    gateway.endpoint.webui_connections.add(webui_conn)
    channel._attach(webui_conn, "webui-chat")

    # Typed coordinator events go to the projector and return None.
    typed = OutboundMessage(channel="websocket", chat_id="webui-chat", content="x")
    typed.event = object()  # additive backend field; transport dispatch seam
    assert await channel.send(typed) is None
    assert projector.sent == [typed]

    # Fork-shaped message to the webui chat: queued, acked as delivered.
    result = await channel.send(OutboundMessage(
        channel="websocket", chat_id="webui-chat", content="hello webui",
    ))
    assert isinstance(result, DeliveryResult)
    assert result.status == "delivered"
    await _drain()
    assert webui_conn.sent[-1]["event"] == "message"
    assert webui_conn.sent[-1]["text"] == "hello webui"

    # Fork-shaped message to the legacy chat: direct send, same DeliveryResult.
    result = await channel.send(OutboundMessage(
        channel="websocket", chat_id="legacy-chat", content="hello legacy",
    ))
    assert result.status == "delivered"
    assert legacy_conn.sent[-1]["text"] == "hello legacy"

    # Gateway mode persists fork-shaped messages into the WebUI transcript
    # (canonical rows for /webui-thread reads), for both audiences' chats.
    persisted = {event["event"] for _, event, _ in gateway.transcripts.events}
    assert persisted == {"message"}


@pytest.mark.asyncio
async def test_gateway_mode_hydrate_seam_replays_via_projector(webui_stack):
    channel, gateway = _gateway_channel()
    projector = _RecordingProjector()
    channel._outbound = projector

    await channel._hydrate_after_subscribe("some-chat")
    assert projector.hydrated == ["some-chat"]
