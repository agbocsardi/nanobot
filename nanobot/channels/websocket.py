"""Minimal WebSocket server channel for programmatic nanobot clients."""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import secrets
import ssl
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, Self
from urllib.parse import parse_qs, urlsplit

from pydantic import Field, field_validator, model_validator
from websockets.asyncio.server import ServerConnection, serve, unix_serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request as WsRequest

from nanobot.bus.events import OUTBOUND_META_AGENT_UI, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import Base
from nanobot.utils.media_decode import FileSizeExceeded, save_base64_data_url


def _normalize_path(value: str) -> str:
    value = value.strip() or "/"
    if not value.startswith("/"):
        value = f"/{value}"
    if len(value) > 1:
        value = value.rstrip("/")
    return value or "/"


def _parse_request_path(path: str) -> tuple[str, dict[str, list[str]]]:
    parsed = urlsplit(path or "/")
    return _normalize_path(parsed.path or "/"), parse_qs(parsed.query, keep_blank_values=True)


def _query_first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    return values[0]


def _is_websocket_upgrade(request: WsRequest) -> bool:
    upgrade = request.headers.get("Upgrade") or request.headers.get("upgrade")
    connection = request.headers.get("Connection") or request.headers.get("connection")
    if not upgrade or "websocket" not in upgrade.lower():
        return False
    return bool(connection and "upgrade" in connection.lower())


class WebSocketConfig(Base):
    """WebSocket server configuration.

    Clients connect to ``ws://{host}:{port}{path}?client_id=...&token=...``.
    The channel supports plain text frames and JSON envelopes:

    - ``{"type": "new_chat"}`` → creates/subscribes a new chat.
    - ``{"type": "attach", "chat_id": "..."}`` → subscribes to an existing chat.
    - ``{"type": "message", "chat_id": "...", "content": "..."}`` → sends a turn.
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    unix_socket_path: str = ""
    path: str = "/"
    token: str = ""
    token_issue_path: str = ""
    token_issue_secret: str = ""
    token_ttl_s: int = Field(default=300, ge=30, le=86_400)
    websocket_requires_token: bool = True
    allow_from: list[str] = Field(default_factory=lambda: ["*"])
    streaming: bool = True
    max_message_bytes: int = Field(default=37_748_736, ge=1024, le=41_943_040)
    ping_interval_s: float = Field(default=20.0, ge=5.0, le=300.0)
    ping_timeout_s: float = Field(default=20.0, ge=5.0, le=300.0)
    ssl_certfile: str = ""
    ssl_keyfile: str = ""

    @field_validator("unix_socket_path")
    @classmethod
    def unix_socket_path_format(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if "\x00" in value:
            raise ValueError("unix_socket_path must not contain NUL bytes")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("unix_socket_path must be an absolute path")
        return str(path)

    @field_validator("path")
    @classmethod
    def path_must_start_with_slash(cls, value: str) -> str:
        return _normalize_path(value)

    @field_validator("token_issue_path")
    @classmethod
    def token_issue_path_format(cls, value: str) -> str:
        return _normalize_path(value) if value.strip() else ""

    @model_validator(mode="after")
    def token_issue_path_differs_from_ws_path(self) -> Self:
        if self.token_issue_path and _normalize_path(self.token_issue_path) == _normalize_path(self.path):
            raise ValueError("token_issue_path must differ from path")
        return self

    @model_validator(mode="after")
    def wildcard_host_requires_auth(self) -> Self:
        if self.host not in ("0.0.0.0", "::"):
            return self
        if self.token.strip() or self.token_issue_secret.strip():
            return self
        raise ValueError(
            "host is 0.0.0.0 (all interfaces) but neither token nor "
            "token_issue_secret is set"
        )


_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_:-]{1,64}$")
_MAX_IMAGES_PER_MESSAGE = 4
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_VIDEOS_PER_MESSAGE = 1
_MAX_VIDEO_BYTES = 20 * 1024 * 1024
_IMAGE_MIME_ALLOWED = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
_VIDEO_MIME_ALLOWED = frozenset({"video/mp4", "video/webm", "video/quicktime"})
_UPLOAD_MIME_ALLOWED = _IMAGE_MIME_ALLOWED | _VIDEO_MIME_ALLOWED
_DATA_URL_MIME_RE = re.compile(r"^data:([^;,]+)(?:;[^,]*)*;base64,", re.DOTALL)


def publish_runtime_model_update(bus: MessageBus, model: str, model_preset: str | None) -> None:
    """Broadcast a runtime model update to WebSocket subscribers."""
    bus.outbound.put_nowait(OutboundMessage(
        channel="websocket",
        chat_id="*",
        content="",
        metadata={
            "_runtime_model_updated": True,
            "model": model,
            "model_preset": model_preset,
        },
    ))


def _is_valid_chat_id(value: Any) -> bool:
    return isinstance(value, str) and _CHAT_ID_RE.match(value) is not None


def _parse_inbound_payload(raw: str) -> str | None:
    text = raw.strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(data, dict):
            for key in ("content", "text", "message"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            return None
        return None
    return text


def _parse_envelope(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data if isinstance(data.get("type"), str) else None


def _extract_data_url_mime(url: str) -> str | None:
    m = _DATA_URL_MIME_RE.match(url) if isinstance(url, str) else None
    return m.group(1).strip().lower() if m else None


class WebSocketChannel(BaseChannel):
    """Run a WebSocket server and forward messages to the bus."""

    name = "websocket"
    display_name = "WebSocket"

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = WebSocketConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WebSocketConfig = config
        self._subs: dict[str, set[Any]] = {}
        self._conn_chats: dict[Any, set[str]] = {}
        self._conn_default: dict[Any, str] = {}
        self._issued_tokens: dict[str, float] = {}
        self._stop_event: asyncio.Event | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._stream_text_buffers: dict[tuple[str, str], list[str]] = {}

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return WebSocketConfig().model_dump(by_alias=True)

    def _expected_path(self) -> str:
        return _normalize_path(self.config.path)

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        cert = self.config.ssl_certfile.strip()
        key = self.config.ssl_keyfile.strip()
        if not cert and not key:
            return None
        if not cert or not key:
            raise ValueError("ssl_certfile and ssl_keyfile must both be set")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile=cert, keyfile=key)
        return ctx

    def _attach(self, connection: Any, chat_id: str) -> None:
        self._subs.setdefault(chat_id, set()).add(connection)
        self._conn_chats.setdefault(connection, set()).add(chat_id)

    def _cleanup_connection(self, connection: Any) -> None:
        for chat_id in self._conn_chats.pop(connection, set()):
            subs = self._subs.get(chat_id)
            if subs is None:
                continue
            subs.discard(connection)
            if not subs:
                self._subs.pop(chat_id, None)
        self._conn_default.pop(connection, None)

    async def _send_event(self, connection: Any, event: str, **fields: Any) -> None:
        payload = {"event": event, **fields}
        try:
            await connection.send(json.dumps(payload, ensure_ascii=False))
        except ConnectionClosed:
            self._cleanup_connection(connection)
        except Exception as exc:
            self.logger.warning("failed to send {} event: {}", event, exc)

    def _purge_expired_tokens(self) -> None:
        now = time.time()
        expired = [tok for tok, expires in self._issued_tokens.items() if expires <= now]
        for tok in expired:
            self._issued_tokens.pop(tok, None)

    def _take_issued_token_if_valid(self, supplied: str | None) -> bool:
        if not supplied:
            return False
        self._purge_expired_tokens()
        expires = self._issued_tokens.pop(supplied, None)
        return expires is not None and expires > time.time()

    def _http_response(self, connection: Any, status: int, body: str) -> Any:
        return connection.respond(status, body)

    def _issue_token_response(self, connection: Any, request: WsRequest) -> Any:
        secret = self.config.token_issue_secret.strip()
        if secret:
            auth = request.headers.get("Authorization", "")
            header = request.headers.get("X-Nanobot-Auth", "")
            supplied = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else header
            if not hmac.compare_digest(supplied, secret):
                return self._http_response(connection, 401, "Unauthorized")
        token = secrets.token_urlsafe(32)
        self._issued_tokens[token] = time.time() + self.config.token_ttl_s
        return self._http_response(
            connection,
            200,
            json.dumps({"token": token, "expires_in": self.config.token_ttl_s}),
        )

    async def _dispatch_http(self, connection: Any, request: WsRequest) -> Any:
        got, query = _parse_request_path(request.path)
        if got == self._expected_path() and _is_websocket_upgrade(request):
            client_id = (_query_first(query, "client_id") or "")[:128]
            if not self.is_allowed(client_id):
                return self._http_response(connection, 403, "Forbidden")
            return self._authorize_websocket_handshake(connection, query)
        if self.config.token_issue_path and got == _normalize_path(self.config.token_issue_path):
            return self._issue_token_response(connection, request)
        return self._http_response(connection, 404, "Not Found")

    def _authorize_websocket_handshake(self, connection: Any, query: dict[str, list[str]]) -> Any:
        supplied = _query_first(query, "token")
        static_token = self.config.token.strip()
        if static_token:
            if supplied and hmac.compare_digest(supplied, static_token):
                return None
            if self._take_issued_token_if_valid(supplied):
                return None
            return self._http_response(connection, 401, "Unauthorized")
        if self.config.websocket_requires_token and not self._take_issued_token_if_valid(supplied):
            return self._http_response(connection, 401, "Unauthorized")
        if supplied:
            self._take_issued_token_if_valid(supplied)
        return None

    async def start(self) -> None:
        from nanobot.utils.logging_bridge import redirect_lib_logging

        redirect_lib_logging("websockets", level="WARNING")
        self._running = True
        self._stop_event = asyncio.Event()
        ssl_context = self._build_ssl_context()
        scheme = "wss" if ssl_context else "ws"

        async def process_request(connection: ServerConnection, request: WsRequest) -> Any:
            return await self._dispatch_http(connection, request)

        async def handler(connection: ServerConnection) -> None:
            await self._connection_loop(connection)

        self.logger.info(
            "WebSocket server listening on {}",
            (
                f"unix:{self.config.unix_socket_path}{self.config.path}"
                if self.config.unix_socket_path
                else f"{scheme}://{self.config.host}:{self.config.port}{self.config.path}"
            ),
        )

        async def runner() -> None:
            socket_path = self.config.unix_socket_path
            if socket_path:
                path_obj = Path(socket_path)
                path_obj.parent.mkdir(parents=True, exist_ok=True)
                with suppress(FileNotFoundError):
                    path_obj.unlink()
                server = await unix_serve(
                    handler,
                    socket_path,
                    process_request=process_request,
                    max_size=self.config.max_message_bytes,
                    ping_interval=self.config.ping_interval_s,
                    ping_timeout=self.config.ping_timeout_s,
                )
                with suppress(OSError):
                    path_obj.chmod(0o600)
            else:
                server = await serve(
                    handler,
                    self.config.host,
                    self.config.port,
                    process_request=process_request,
                    max_size=self.config.max_message_bytes,
                    ping_interval=self.config.ping_interval_s,
                    ping_timeout=self.config.ping_timeout_s,
                    ssl=ssl_context,
                )
            try:
                assert self._stop_event is not None
                await self._stop_event.wait()
            finally:
                server.close()
                await server.wait_closed()
                if socket_path:
                    with suppress(FileNotFoundError):
                        Path(socket_path).unlink()

        self._server_task = asyncio.create_task(runner())
        await self._server_task

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._stop_event:
            self._stop_event.set()
        if self._server_task:
            try:
                await self._server_task
            except Exception as exc:
                self.logger.warning("server task error during shutdown: {}", exc)
            self._server_task = None
        self._subs.clear()
        self._conn_chats.clear()
        self._conn_default.clear()
        self._issued_tokens.clear()

    async def _connection_loop(self, connection: Any) -> None:
        request = connection.request
        _, query = _parse_request_path(request.path if request else "/")
        client_id = (_query_first(query, "client_id") or f"anon-{uuid.uuid4().hex[:12]}").strip()
        client_id = client_id[:128]
        default_chat_id = str(uuid.uuid4())
        try:
            await connection.send(json.dumps({
                "event": "ready",
                "chat_id": default_chat_id,
                "client_id": client_id,
            }, ensure_ascii=False))
            self._conn_default[connection] = default_chat_id
            self._attach(connection, default_chat_id)

            async for raw in connection:
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        self.logger.warning("ignoring non-utf8 binary frame")
                        continue
                envelope = _parse_envelope(raw)
                if envelope is not None:
                    await self._dispatch_envelope(connection, client_id, envelope)
                    continue
                content = _parse_inbound_payload(raw)
                if content:
                    await self._handle_message(
                        sender_id=client_id,
                        chat_id=default_chat_id,
                        content=content,
                        metadata={"remote": getattr(connection, "remote_address", None)},
                        is_dm=False,
                    )
        except Exception as exc:
            self.logger.debug("connection ended: {}", exc)
        finally:
            self._cleanup_connection(connection)

    def _save_envelope_media(self, media: list[Any]) -> tuple[list[str], str | None]:
        image_count = 0
        video_count = 0
        for item in media:
            mime = _extract_data_url_mime(item.get("data_url", "")) if isinstance(item, dict) else None
            if mime in _VIDEO_MIME_ALLOWED:
                video_count += 1
            elif mime in _IMAGE_MIME_ALLOWED:
                image_count += 1
        if image_count > _MAX_IMAGES_PER_MESSAGE:
            return [], "too_many_images"
        if video_count > _MAX_VIDEOS_PER_MESSAGE:
            return [], "too_many_videos"

        media_dir = get_media_dir("websocket")
        paths: list[str] = []

        def abort(reason: str) -> tuple[list[str], str]:
            for path in paths:
                with suppress(OSError):
                    Path(path).unlink(missing_ok=True)
            return [], reason

        for item in media:
            if not isinstance(item, dict):
                return abort("malformed")
            data_url = item.get("data_url")
            if not isinstance(data_url, str) or not data_url:
                return abort("malformed")
            mime = _extract_data_url_mime(data_url)
            if mime is None:
                return abort("decode")
            if mime not in _UPLOAD_MIME_ALLOWED:
                return abort("mime")
            max_bytes = _MAX_VIDEO_BYTES if mime in _VIDEO_MIME_ALLOWED else _MAX_IMAGE_BYTES
            try:
                saved = save_base64_data_url(data_url, media_dir, max_bytes=max_bytes)
            except FileSizeExceeded:
                return abort("size")
            except Exception as exc:
                self.logger.warning("media decode failed: {}", exc)
                return abort("decode")
            if saved is None:
                return abort("decode")
            paths.append(saved)
        return paths, None

    async def _dispatch_envelope(self, connection: Any, client_id: str, envelope: dict[str, Any]) -> None:
        t = envelope.get("type")
        if t == "new_chat":
            chat_id = str(uuid.uuid4())
            self._attach(connection, chat_id)
            await self._send_event(connection, "attached", chat_id=chat_id)
            return
        if t == "attach":
            chat_id = envelope.get("chat_id")
            if not _is_valid_chat_id(chat_id):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            self._attach(connection, chat_id)
            await self._send_event(connection, "attached", chat_id=chat_id)
            return
        if t == "message":
            chat_id = envelope.get("chat_id")
            content = envelope.get("content")
            if not _is_valid_chat_id(chat_id):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            if not isinstance(content, str):
                await self._send_event(connection, "error", detail="missing content")
                return
            media_paths: list[str] = []
            raw_media = envelope.get("media")
            if raw_media is not None:
                if not isinstance(raw_media, list):
                    await self._send_event(connection, "error", detail="media_rejected", reason="malformed")
                    return
                media_paths, reason = self._save_envelope_media(raw_media)
                if reason is not None:
                    await self._send_event(connection, "error", detail="media_rejected", reason=reason)
                    return
            if not content.strip() and not media_paths:
                await self._send_event(connection, "error", detail="missing content")
                return
            self._attach(connection, chat_id)
            metadata: dict[str, Any] = {"remote": getattr(connection, "remote_address", None)}
            image_generation = envelope.get("image_generation")
            if isinstance(image_generation, dict) and image_generation.get("enabled") is True:
                aspect_ratio = image_generation.get("aspect_ratio")
                metadata["image_generation"] = {
                    "enabled": True,
                    "aspect_ratio": aspect_ratio if isinstance(aspect_ratio, str) else None,
                }
            await self._handle_message(
                sender_id=client_id,
                chat_id=chat_id,
                content=content,
                media=media_paths or None,
                metadata=metadata,
                is_dm=False,
            )
            return
        await self._send_event(connection, "error", detail=f"unknown type: {t!r}")

    async def _safe_send_to(self, connection: Any, raw: str, *, label: str = "") -> None:
        try:
            await connection.send(raw)
        except ConnectionClosed:
            self._cleanup_connection(connection)
            self.logger.warning("connection gone{}", label)
        except Exception:
            self.logger.exception("send failed{}", label)
            raise

    async def send(self, msg: OutboundMessage) -> None:
        if msg.metadata.get("_runtime_model_updated"):
            await self.send_runtime_model_updated(
                model_name=msg.metadata.get("model"),
                model_preset=msg.metadata.get("model_preset"),
            )
            return
        conns = list(self._subs.get(msg.chat_id, ()))
        payload: dict[str, Any] = {
            "event": "message",
            "chat_id": msg.chat_id,
            "text": msg.content,
        }
        if msg.media:
            payload["media"] = msg.media
        if msg.reply_to:
            payload["reply_to"] = msg.reply_to
        if isinstance(msg.metadata.get("latency_ms"), (int, float)):
            payload["latency_ms"] = int(msg.metadata["latency_ms"])
        if msg.metadata.get("_tool_events"):
            payload["tool_events"] = msg.metadata["_tool_events"]
        if msg.metadata.get(OUTBOUND_META_AGENT_UI) is not None:
            payload["agent_ui"] = msg.metadata[OUTBOUND_META_AGENT_UI]
        if msg.metadata.get("_tool_hint"):
            payload["kind"] = "tool_hint"
        elif msg.metadata.get("_progress"):
            payload["kind"] = "progress"
        raw = json.dumps(payload, ensure_ascii=False)
        for conn in conns:
            await self._safe_send_to(conn, raw)

    async def send_delta(self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None) -> None:
        meta = metadata or {}
        stream_key = (chat_id, str(meta.get("_stream_id") or ""))
        if meta.get("_stream_end"):
            body: dict[str, Any] = {"event": "stream_end", "chat_id": chat_id}
            buffered = self._stream_text_buffers.pop(stream_key, [])
            if delta:
                buffered.append(delta)
            if buffered:
                body["text"] = "".join(buffered)
        else:
            body = {"event": "delta", "chat_id": chat_id, "text": delta}
            self._stream_text_buffers.setdefault(stream_key, []).append(delta)
        if meta.get("_stream_id") is not None:
            body["stream_id"] = meta["_stream_id"]
        raw = json.dumps(body, ensure_ascii=False)
        for conn in list(self._subs.get(chat_id, ())):
            await self._safe_send_to(conn, raw, label=" stream")

    async def send_reasoning_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not delta:
            return
        body: dict[str, Any] = {"event": "reasoning_delta", "chat_id": chat_id, "text": delta}
        meta = metadata or {}
        if meta.get("_stream_id") is not None:
            body["stream_id"] = meta["_stream_id"]
        raw = json.dumps(body, ensure_ascii=False)
        for conn in list(self._subs.get(chat_id, ())):
            await self._safe_send_to(conn, raw, label=" reasoning")

    async def send_reasoning_end(
        self,
        chat_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        body: dict[str, Any] = {"event": "reasoning_end", "chat_id": chat_id}
        meta = metadata or {}
        if meta.get("_stream_id") is not None:
            body["stream_id"] = meta["_stream_id"]
        raw = json.dumps(body, ensure_ascii=False)
        for conn in list(self._subs.get(chat_id, ())):
            await self._safe_send_to(conn, raw, label=" reasoning_end")

    async def send_file_edit_events(
        self,
        chat_id: str,
        edits: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        body = {"event": "file_edit", "chat_id": chat_id, "edits": edits}
        raw = json.dumps(body, ensure_ascii=False)
        for conn in list(self._subs.get(chat_id, ())):
            await self._safe_send_to(conn, raw, label=" file_edit")

    async def send_runtime_model_updated(self, *, model_name: Any, model_preset: Any = None) -> None:
        conns = list(self._conn_chats)
        if not conns or not isinstance(model_name, str) or not model_name.strip():
            return
        body: dict[str, Any] = {"event": "runtime_model_updated", "model_name": model_name.strip()}
        if isinstance(model_preset, str) and model_preset.strip():
            body["model_preset"] = model_preset.strip()
        raw = json.dumps(body, ensure_ascii=False)
        for conn in conns:
            await self._safe_send_to(conn, raw, label=" runtime_model_updated")
