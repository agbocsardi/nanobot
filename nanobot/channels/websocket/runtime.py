"""WebSocket server channel: one listener serves legacy and WebUI clients.

Grafted from the upstream ``nanobot/channels/websocket/runtime.py`` at pinned
HKUDS/nanobot ``d0d0a44e57632c3d269e511339cff7ddb698e62e``, with localized
fork compatibility grafts (issue #38):

- ``gateway`` is optional. ``WebSocketChannel(config, bus)`` keeps the fork's
  gateway-less legacy behavior: own HTTP dispatch (WS upgrade + token issue),
  inline ``new_chat``/``attach``/``message`` envelopes, connection-scoped
  ownership, truthful ``DeliveryResult`` sends, and metadata-marker streaming
  (``_stream_id``/``_stream_end``) for the fork manager's positional calls.
- ``WebSocketConfig.enabled`` defaults to ``False`` (fork discovery default).
- ``send_delta``/``send_reasoning_*`` read ``_stream_id``/``_stream_end``
  from *metadata* when the upstream kwargs are absent — the fork channel
  manager passes streaming markers positionally; kwargs still win when given.
- Per-connection audience dispatch: connections authenticated with a
  bootstrap-audience token (``gateway.endpoint.webui_connections``) are
  dispatched to the ``WebUICommandRouter``; every other connection (static
  token, ``token_issue_path`` audience-``client`` tokens, tokenless
  localhost) uses the legacy envelope branch. The envelope's ``webui: true``
  flag is never trusted for audience.
- Single-audience chat binding: in gateway mode legacy mints avoid ids bound
  to the WebUI audience (live registry + persisted ``webui:`` sessions via
  ``gateway.session_exists``), ``webui_attach`` refuses legacy-bound ids, and
  ``_owns_chat`` never owns a webui-bound id. ``require_existing_session``
  is refused explicitly (temporary sessions are not supported yet) instead
  of being silently ignored.
- All ``nanobot.webui`` imports are lazy: the module imports and runs legacy
  mode without the WebUI package present.
"""

from __future__ import annotations

import asyncio
import errno
import hmac
import ipaddress
import json
import re
import secrets
import socket
import ssl
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import parse_qs, urlsplit, urlunsplit
from weakref import WeakSet

from pydantic import Field, PrivateAttr, field_validator, model_validator
from websockets.asyncio.server import Server, ServerConnection, serve, unix_serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request as WsRequest

from nanobot.bus.events import OUTBOUND_META_AGENT_UI, DeliveryResult, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import Base
from nanobot.utils.media_decode import FileSizeExceeded, save_base64_data_url

if TYPE_CHECKING:
    from nanobot.webui.gateway_services import GatewayServices

# Plain HTTP WebUI routes also run through websockets.process_request.
_WEBUI_HTTP_OPEN_TIMEOUT_S = 360.0
_LISTENER_CHECK_INTERVAL_S = 0.5
_LISTENER_STABLE_AFTER_S = 30.0
_LISTENER_RESTART_BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)

# Outbound delivery is isolated per connection.  A bounded queue keeps a slow
# or suspended terminal from retaining an unbounded stream in server memory.
_OUTBOUND_QUEUE_MAX_FRAMES = 256
_OUTBOUND_QUEUE_MAX_BYTES = 8 * 1024 * 1024
_OUTBOUND_SEND_TIMEOUT_S = 10.0
_OUTBOUND_CLOSE_TIMEOUT_S = 1.0

# A bind conflict or invalid address needs operator action and must not be
# retried forever. These errors can be caused by a transient local network
# interruption and are safe to retry at the channel boundary.
_RECOVERABLE_LISTENER_ERRNOS = {
    getattr(socket, name)
    for name in (
        "ECONNABORTED",
        "ECONNRESET",
        "EHOSTDOWN",
        "EHOSTUNREACH",
        "ENETDOWN",
        "ENETRESET",
        "ENETUNREACH",
        "ETIMEDOUT",
    )
    if hasattr(socket, name)
}
_RECOVERABLE_LISTENER_WINERRORS = {
    64,  # ERROR_NETNAME_DELETED / "The specified network name is no longer available."
    995,  # ERROR_OPERATION_ABORTED
    10050,  # WSAENETDOWN
    10052,  # WSAENETRESET
    10053,  # WSAECONNABORTED
    10054,  # WSAECONNRESET
    10060,  # WSAETIMEDOUT
    10065,  # WSAEHOSTUNREACH
}


class _CrossAudienceBindError(Exception):
    """A WebUI bind was refused because the chat id is bound to the legacy audience."""

    def __init__(self, chat_id: str) -> None:
        super().__init__(f"chat id is bound to the legacy audience: {chat_id}")
        self.chat_id = chat_id


class _TemporarySessionUnsupportedError(Exception):
    """``require_existing_session`` was requested but the fork cannot honor it."""

    def __init__(self, chat_id: str) -> None:
        super().__init__(f"temporary sessions are not supported: {chat_id}")
        self.chat_id = chat_id


@dataclass(slots=True)
class _OutboundFrame:
    raw: str
    utf8_bytes: int
    label: str


@dataclass(slots=True)
class _ConnectionOutbound:
    queue: asyncio.Queue[_OutboundFrame]
    buffered_bytes: int = 0
    writer: asyncio.Task[None] | None = None
    closing: bool = False


_ROUTING_ASSERTION_HEADERS = frozenset(
    {
        "host",
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-real-ip",
        "cf-connecting-ip",
    }
)


class TrustedProxyAuthConfig(Base):
    """Authentication assertions accepted from explicitly trusted proxy peers."""

    trusted_peer_cidrs: list[str] = Field(min_length=1)
    assertion_header: str = Field(min_length=1)
    _trusted_peer_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = PrivateAttr(
        default=()
    )

    @field_validator("trusted_peer_cidrs")
    @classmethod
    def validate_trusted_peer_cidrs(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            value = value.strip()
            try:
                network = ipaddress.ip_network(value, strict=False)
            except ValueError as exc:
                raise ValueError(f"invalid trusted proxy CIDR: {value!r}") from exc
            if network.prefixlen == 0:
                raise ValueError("universal trusted proxy CIDRs are not allowed")
            if isinstance(network, ipaddress.IPv6Network):
                mapped_start = ipaddress.IPv6Address("::ffff:0:0")
                mapped_end = ipaddress.IPv6Address("::ffff:ffff:ffff")
                if mapped_start in network and mapped_end in network:
                    raise ValueError("trusted proxy CIDRs must not cover all IPv4-mapped addresses")
            normalized.append(network.with_prefixlen)
        return normalized

    @field_validator("assertion_header")
    @classmethod
    def validate_assertion_header(cls, value: str) -> str:
        value = value.strip()
        if not value or any(char.isspace() or ord(char) < 0x21 for char in value):
            raise ValueError("assertion_header must be a valid HTTP header name")
        normalized = value.casefold()
        if normalized in _ROUTING_ASSERTION_HEADERS or normalized.startswith("x-forwarded-"):
            raise ValueError(
                "assertion_header must identify a proxy-generated authentication assertion, "
                "not a routing or client metadata header"
            )
        return value

    @model_validator(mode="after")
    def compile_trusted_peer_networks(self) -> Self:
        self._trusted_peer_networks = tuple(
            ipaddress.ip_network(value, strict=False) for value in self.trusted_peer_cidrs
        )
        return self


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

    Gateway-backed deployments additionally serve WebUI bootstrap/HTTP routes
    on the same listener via the gateway endpoint.
    """

    # Fork default: channels are disabled unless configured on.
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    unix_socket_path: str = ""
    path: str = "/"
    public_ws_url: str = ""
    token: str = ""
    token_issue_path: str = ""
    token_issue_secret: str = ""
    trusted_proxy_auth: TrustedProxyAuthConfig | None = None
    token_ttl_s: int = Field(default=300, ge=30, le=86_400)
    websocket_requires_token: bool = True
    allow_from: list[str] = Field(default_factory=lambda: ["*"])
    streaming: bool = True
    # Default 36 MB, upper 40 MB: supports up to 4 images at ~6 MB each after
    # client-side Worker normalization (see webui Composer). 4 × 6 MB × 1.37
    # (base64 overhead) + envelope framing stays under 36 MB; the 40 MB ceiling
    # leaves a small margin for sender slop without opening a DoS avenue.
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
        # Fork behavior: tolerate and normalize instead of rejecting.
        return _normalize_path(value)

    @field_validator("token_issue_path")
    @classmethod
    def token_issue_path_format(cls, value: str) -> str:
        return _normalize_path(value) if value.strip() else ""

    @field_validator("public_ws_url")
    @classmethod
    def public_ws_url_format(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"ws", "wss"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("public_ws_url must be an absolute ws:// or wss:// URL without credentials")
        return urlunsplit(
            (parsed.scheme, parsed.netloc, _normalize_path(parsed.path or "/"), "", "")
        )

    @model_validator(mode="after")
    def public_ws_url_matches_path(self) -> Self:
        if self.public_ws_url and urlsplit(self.public_ws_url).path != _normalize_path(self.path):
            raise ValueError("public_ws_url path must match path")
        return self

    @model_validator(mode="after")
    def token_issue_path_differs_from_ws_path(self) -> Self:
        if self.token_issue_path and _normalize_path(self.token_issue_path) == _normalize_path(self.path):
            raise ValueError("token_issue_path must differ from path")
        return self

    @model_validator(mode="after")
    def wildcard_host_requires_auth(self) -> Self:
        if self.host not in ("0.0.0.0", "::"):
            return self
        if self.token.strip() or self.token_issue_secret.strip() or self.trusted_proxy_auth is not None:
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
    """Legacy chat-id syntax check (fork semantics; not ownership)."""
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


class _ListenerUnavailableError(OSError):
    """Raised when a previously bound listener loses its serving socket."""


class WebSocketChannel(BaseChannel):
    """Run a WebSocket server and forward messages to the bus.

    Without ``gateway`` this is the fork's legacy programmatic-client channel.
    With ``gateway`` the same listener additionally serves WebUI clients
    (bootstrap-audience tokens) through the upstream command/projector seam.
    """

    name = "websocket"
    display_name = "WebSocket"

    def __init__(
        self,
        config: Any,
        bus: MessageBus,
        *,
        gateway: GatewayServices | None = None,
    ):
        if isinstance(config, dict):
            config = WebSocketConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WebSocketConfig = config
        # chat_id -> connections subscribed to it (fan-out target).
        self._subs: dict[str, set[Any]] = {}
        # connection -> chat_ids it is subscribed to (O(1) cleanup on disconnect).
        self._conn_chats: dict[Any, set[str]] = {}
        # connection -> default chat_id for legacy frames that omit routing.
        self._conn_default: dict[Any, str] = {}
        self._issued_tokens: dict[str, float] = {}
        self._stop_event: asyncio.Event | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._stream_text_buffers: dict[tuple[str, str], list[str]] = {}
        self._reasoning_text_buffers: dict[tuple[str, str], list[str]] = {}
        # Gateway-mode state (populated by _init_webui; legacy mode never touches it).
        self.gateway = gateway
        self._webui_connections: set[Any] = set()
        self._server: Server | None = None
        self._connection_outbound: dict[Any, _ConnectionOutbound] = {}
        self._outbound_retire_tasks: set[asyncio.Task[None]] = set()
        self._retired_connections: WeakSet[Any] = WeakSet()
        if gateway is not None:
            self._init_webui(gateway)

    def _init_webui(self, gateway: GatewayServices) -> None:
        """Wire the upstream WebUI stack. Lazy imports: legacy mode needs none of it."""
        from nanobot.webui.inbound_commands import WebUICommandRouter
        from nanobot.webui.outbound_projection import WebUIOutboundProjector

        # Connections authenticated with a bootstrap-audience token
        # (live set owned by the gateway endpoint).
        self._webui_connections = gateway.endpoint.webui_connections
        self._media = gateway.media
        self._transcripts = gateway.transcripts
        self._temporary_chats = gateway.temporary_chats
        self._session_projection = gateway.session_projection
        self._commands = WebUICommandRouter(self, gateway)
        self._webui_request_tasks = self._commands.request_tasks
        self._webui_request_operations = self._commands.request_operations
        self._webui_request_locks = self._commands.request_locks
        self._outbound = WebUIOutboundProjector(self, self._session_projection)

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return WebSocketConfig().model_dump(by_alias=True)

    # -- Single-audience chat binding (gateway mode) ------------------------

    def _legacy_session_key(self, chat_id: str) -> str:
        """Fork derivation for legacy websocket sessions (untouched)."""
        return f"{self.name}:{chat_id}"

    def _webui_session_key(self, chat_id: str) -> str:
        from nanobot.webui.session_identity import webui_session_key

        return webui_session_key(chat_id)

    def _session_exists_strict(self, session_key: str, *, default: bool) -> bool:
        """``gateway.session_exists`` lookup; *default* is the fail-closed answer.

        ``session_exists`` is side-effect-free over cached + persisted sessions
        (backend-owned). On absence or error we return the conservative
        default so a broken lookup can never open a cross-audience bind.
        """
        exists = getattr(self.gateway, "session_exists", None)
        if exists is None:
            self.logger.warning(
                "gateway.session_exists unavailable; treating {} as {}", session_key, default
            )
            return default
        try:
            return bool(exists(session_key))
        except Exception as exc:
            self.logger.warning("session_exists lookup failed for {}: {}", session_key, exc)
            return default

    def _chat_is_webui_bound(self, chat_id: str) -> bool:
        """True when *chat_id* belongs to the WebUI audience (live or persisted)."""
        if self.gateway is None:
            return False
        if any(conn in self._webui_connections for conn in self._subs.get(chat_id, ())):
            return True
        return self._session_exists_strict(self._webui_session_key(chat_id), default=True)

    def _chat_is_legacy_bound(self, chat_id: str) -> bool:
        """True when *chat_id* belongs to the legacy audience (live or persisted-only)."""
        if self.gateway is None:
            return False
        if any(conn not in self._webui_connections for conn in self._subs.get(chat_id, ())):
            return True
        legacy_persisted = self._session_exists_strict(
            self._legacy_session_key(chat_id), default=True
        )
        webui_persisted = self._session_exists_strict(
            self._webui_session_key(chat_id), default=False
        )
        return legacy_persisted and not webui_persisted

    def _mint_chat_id(self) -> str:
        """Mint a chat id for the legacy audience.

        UUID randomness is not treated as the boundary: in gateway mode the
        mint explicitly avoids ids bound to the WebUI audience so a legacy
        chat can never collide with a live or persisted ``webui:`` session.
        """
        for _ in range(100):
            chat_id = str(uuid.uuid4())
            if not self._chat_is_webui_bound(chat_id):
                return chat_id
        raise RuntimeError("could not mint a chat id outside the webui namespace")

    # -- Subscription bookkeeping -------------------------------------------

    def webui_subscribers(self, chat_id: str) -> tuple[Any, ...]:
        """Return a stable snapshot of one chat's transport subscribers."""
        return tuple(self._subs.get(chat_id, ()))

    def webui_connection_chats(self, connection: Any) -> tuple[str, ...]:
        return tuple(self._conn_chats.get(connection, ()))

    def webui_attach(self, connection: Any, chat_id: str) -> None:
        """Register one WebUI bind; refuse ids bound to the legacy audience.

        Defense in depth behind the router's pre-checks: every WebUI
        subscribe funnels through here, so a missed router path still fails
        closed instead of mixing audiences on one fan-out set.
        """
        if self._chat_is_legacy_bound(chat_id):
            raise _CrossAudienceBindError(chat_id)
        self._attach(connection, chat_id)

    def webui_detach(self, connection: Any, chat_id: str) -> None:
        self._detach(connection, chat_id)

    def webui_clear_connection_default(self, connection: Any) -> None:
        self._conn_default.pop(connection, None)

    def webui_clear_stream_buffers(self, chat_id: str) -> None:
        self._clear_stream_buffers(chat_id)

    async def webui_hydrate(self, chat_id: str) -> None:
        await self._hydrate_after_subscribe(chat_id)

    async def webui_send_event(self, connection: Any, event: str, **fields: Any) -> None:
        await self._send_event(connection, event, **fields)

    async def webui_send_raw(self, connection: Any, raw: str, *, label: str = "") -> None:
        await self._safe_send_to(connection, raw, label=label)

    async def webui_dispatch_message(
        self,
        *,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None,
        metadata: dict[str, Any],
        is_dm: bool,
        session_key: str | None,
        require_existing_session: bool,
    ) -> None:
        if require_existing_session:
            # The fork loop has no active-session-only guard yet (pending
            # core decision). Refuse temporary-session operations explicitly
            # instead of accepting-and-ignoring the flag: nothing is published
            # and no session is created.
            raise _TemporarySessionUnsupportedError(str(chat_id))
        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            media=media,
            metadata=metadata,
            is_dm=is_dm,
            session_key=session_key,
        )

    def _attach(self, connection: Any, chat_id: str) -> None:
        """Idempotently subscribe *connection* to *chat_id*."""
        if self.gateway is not None and not self._register_connection_outbound(connection):
            return
        self._subs.setdefault(chat_id, set()).add(connection)
        self._conn_chats.setdefault(connection, set()).add(chat_id)

    def _register_connection_outbound(self, connection: Any) -> bool:
        if connection in self._retired_connections:
            return False
        self._connection_outbound.setdefault(
            connection,
            _ConnectionOutbound(asyncio.Queue(maxsize=_OUTBOUND_QUEUE_MAX_FRAMES)),
        )
        return True

    def _detach(self, connection: Any, chat_id: str) -> None:
        chats = self._conn_chats.get(connection)
        if chats is not None:
            chats.discard(chat_id)
            if not chats:
                self._conn_chats.pop(connection, None)
        subscribers = self._subs.get(chat_id)
        if subscribers is not None:
            subscribers.discard(connection)
            if not subscribers:
                self._subs.pop(chat_id, None)

    def _clear_stream_buffers(self, chat_id: str) -> None:
        for key in tuple(self._stream_text_buffers):
            if key[0] == chat_id:
                self._stream_text_buffers.pop(key, None)
        for key in tuple(self._reasoning_text_buffers):
            if key[0] == chat_id:
                self._reasoning_text_buffers.pop(key, None)

    def _cleanup_connection(self, connection: Any) -> None:
        """Fork-compatible synchronous detach; safe to call multiple times."""
        for chat_id in self._conn_chats.pop(connection, set()):
            subs = self._subs.get(chat_id)
            if subs is None:
                continue
            subs.discard(connection)
            if not subs:
                self._subs.pop(chat_id, None)
        self._conn_default.pop(connection, None)
        if self.gateway is not None:
            with suppress(Exception):
                self.gateway.endpoint.discard_connection(connection)

    async def _cleanup_connection_async(self, connection: Any) -> None:
        """Full gateway-mode cleanup: router state, endpoint registry, subs."""
        self._retired_connections.add(connection)
        state = self._connection_outbound.get(connection)
        if state is not None:
            state.closing = True
            await self._stop_connection_writer(state)
        try:
            await self._commands.cleanup_connection(connection)
        finally:
            for chat_id in tuple(self._conn_chats.get(connection, ())):
                self._detach(connection, chat_id)
            self._conn_default.pop(connection, None)
            with suppress(Exception):
                self.gateway.endpoint.discard_connection(connection)
            if self._connection_outbound.get(connection) is state:
                self._connection_outbound.pop(connection, None)

    async def _hydrate_after_subscribe(self, chat_id: str) -> None:
        """Replay persisted or actively running per-chat state after subscribe."""
        if self.gateway is None:
            return
        await self._outbound.hydrate(chat_id)

    async def _send_event(self, connection: Any, event: str, **fields: Any) -> None:
        """Send a control event (attached, error, ...) to a single connection."""
        payload: dict[str, Any] = {"event": event, **fields}
        raw = json.dumps(payload, ensure_ascii=False)
        if self.gateway is not None:
            await self._safe_send_to(connection, raw, label=f" {event} ")
            return
        try:
            await connection.send(raw)
        except ConnectionClosed:
            self._cleanup_connection(connection)
        except Exception as exc:
            self.logger.warning("failed to send {} event: {}", event, exc)

    # -- Auth (legacy mode; gateway mode delegates to the endpoint) ---------

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
        if self.gateway is not None:
            # Gateway mode: the endpoint owns all HTTP routes (WS upgrade
            # auth, token issue, bootstrap, WebUI API, static dist).
            return await self.gateway.endpoint.process_request(
                connection,
                request,
                is_allowed=self.is_allowed,
            )
        got, query = _parse_request_path(request.path)
        if got == self._expected_path() and _is_websocket_upgrade(request):
            client_id = (_query_first(query, "client_id") or "")[:128]
            if not self.is_allowed(client_id):
                return self._http_response(connection, 403, "Forbidden")
            return self._authorize_websocket_handshake(connection, query)
        if self.config.token_issue_path and got == _normalize_path(self.config.token_issue_path):
            return self._issue_token_response(connection, request)
        return self._http_response(connection, 404, "Not Found")

    def _authorize_websocket_handshake(
        self,
        connection: Any,
        query: dict[str, list[str]],
        headers: Any = None,
    ) -> Any:
        if self.gateway is not None:
            # Compatibility proxy, mirroring the upstream integration.
            return self.gateway.endpoint.authorize_websocket_handshake(
                connection, query, headers
            )
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

    # -- Server lifecycle and connection ingress ---------------------------

    @staticmethod
    def _socket_is_accepting(sock: socket.socket) -> bool:
        """Return whether a bound socket still advertises a listen capability.

        ``SO_ACCEPTCONN`` is not portable: macOS/BSD raise ``OSError`` with
        ``ENOPROTOOPT`` ("Protocol not available") for this option even on a
        perfectly healthy listening socket. Treating that as "not serving"
        makes the listener look permanently degraded, so the caller retries
        forever and the channel never reaches a ready state. When the option
        is unavailable we fall back to the file-descriptor liveness check.
        """
        if sock.fileno() < 0:
            return False
        try:
            return bool(sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN))
        except OSError as exc:
            if exc.errno in (errno.ENOPROTOOPT, errno.EOPNOTSUPP):
                return True
            raise

    @classmethod
    def _listener_is_serving(cls, server: Server) -> bool:
        """Return whether every bound socket still has a live listen capability."""
        try:
            sockets = server.sockets
            return bool(sockets) and server.is_serving() and all(
                cls._socket_is_accepting(sock) for sock in sockets
            )
        except OSError:
            return False

    @staticmethod
    def _is_recoverable_listener_error(error: Exception, *, was_serving: bool) -> bool:
        if isinstance(error, _ListenerUnavailableError):
            return True
        if not isinstance(error, OSError):
            return False
        if was_serving:
            return True
        winerror = getattr(error, "winerror", None)
        return (
            error.errno in _RECOVERABLE_LISTENER_ERRNOS
            or winerror in _RECOVERABLE_LISTENER_WINERRORS
        )

    async def _wait_for_listener_loss(self, server: Server) -> None:
        """Wait for shutdown or raise when the serving socket disappears."""
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=_LISTENER_CHECK_INTERVAL_S,
                )
            except TimeoutError:
                if not self._listener_is_serving(server):
                    raise _ListenerUnavailableError(
                        "WebSocket listener is no longer accepting connections"
                    )

    async def _close_server(self, server: Server, socket_path: str) -> None:
        server.close()
        try:
            await server.wait_closed()
        except OSError as exc:
            self.logger.warning("WebSocket server close failed: {}", exc)
        if socket_path:
            with suppress(FileNotFoundError):
                Path(socket_path).unlink()

    def _log_listener_ready(self, scheme: str) -> None:
        self.logger.info(
            "WebSocket server listening on {}",
            (
                f"unix:{self.config.unix_socket_path}{self.config.path}"
                if self.config.unix_socket_path
                else f"{scheme}://{self.config.host}:{self.config.port}{self.config.path}"
            ),
        )

    async def start(self) -> None:
        from nanobot.utils.logging_bridge import redirect_lib_logging

        redirect_lib_logging("websockets", level="WARNING")
        self._stop_event = asyncio.Event()
        stop_event = self._stop_event
        ssl_context = self._build_ssl_context()
        scheme = "wss" if ssl_context else "ws"

        async def process_request(connection: ServerConnection, request: WsRequest) -> Any:
            return await self._dispatch_http(connection, request)

        async def handler(connection: ServerConnection) -> None:
            await self._connection_loop(connection)

        self._log_listener_ready(scheme)

        if self.gateway is None:
            # Legacy runner: fork behavior, no watchdog, default open timeout.
            self._running = True

            async def legacy_runner() -> None:
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
                self._server = server
                try:
                    assert self._stop_event is not None
                    await self._stop_event.wait()
                finally:
                    await self._close_server(server, socket_path if socket_path else "")

            self._server_task = asyncio.create_task(legacy_runner())
            await self._server_task
            return

        # Gateway mode: WebUI HTTP routes ride process_request and may take
        # longer than the default open timeout; the listener watches for a
        # lost serving socket and restarts with backoff (upstream behavior).
        remote_instances = getattr(getattr(self.gateway, "http", None), "remote_instances", None)
        if remote_instances is not None:
            remote_instances.resume()

        async def gateway_runner() -> None:
            socket_path = self.config.unix_socket_path
            failures = 0
            while not stop_event.is_set():
                server: Server | None = None
                was_serving = False
                started_at = 0.0
                try:
                    if socket_path:
                        path_obj = Path(socket_path)
                        path_obj.parent.mkdir(parents=True, exist_ok=True)
                        with suppress(FileNotFoundError):
                            path_obj.unlink()
                        server = await unix_serve(
                            handler,
                            socket_path,
                            process_request=process_request,
                            open_timeout=_WEBUI_HTTP_OPEN_TIMEOUT_S,
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
                            open_timeout=_WEBUI_HTTP_OPEN_TIMEOUT_S,
                            max_size=self.config.max_message_bytes,
                            ping_interval=self.config.ping_interval_s,
                            ping_timeout=self.config.ping_timeout_s,
                            ssl=ssl_context,
                        )

                    self._server = server
                    was_serving = True
                    if not self._listener_is_serving(server):
                        raise _ListenerUnavailableError(
                            "WebSocket listener did not enter a serving state"
                        )
                    self._running = True
                    started_at = asyncio.get_running_loop().time()
                    await self._wait_for_listener_loss(server)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._running = False
                    if not self._is_recoverable_listener_error(exc, was_serving=was_serving):
                        raise
                    uptime = (
                        asyncio.get_running_loop().time() - started_at if started_at else 0.0
                    )
                    if uptime >= _LISTENER_STABLE_AFTER_S:
                        failures = 0
                    delay = _LISTENER_RESTART_BACKOFF_S[
                        min(failures, len(_LISTENER_RESTART_BACKOFF_S) - 1)
                    ]
                    failures += 1
                    self.logger.warning(
                        "WebSocket listener failed ({}: {}); retrying in {:.1f}s",
                        type(exc).__name__,
                        exc,
                        delay,
                    )
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=delay)
                    except TimeoutError:
                        pass
                finally:
                    self._running = False
                    if server is not None:
                        await self._close_server(server, socket_path)
                    if self._server is server:
                        self._server = None

        task = asyncio.create_task(gateway_runner())
        self._server_task = task
        try:
            await task
        finally:
            self._running = False
            if self._server_task is task:
                self._server_task = None

    async def stop(self) -> None:
        if self.gateway is not None:
            remote_instances = getattr(
                getattr(self.gateway, "http", None), "remote_instances", None
            )
            if remote_instances is not None:
                with suppress(Exception):
                    await remote_instances.close()
            server_task = self._server_task
            if (
                not self._running
                and server_task is None
                and not self._connection_outbound
                and not self._outbound_retire_tasks
            ):
                return
            self._running = False
            if self._stop_event:
                self._stop_event.set()
            for connection in tuple(self._connection_outbound):
                await self._cleanup_connection_async(connection)
            if server_task:
                try:
                    await server_task
                except asyncio.CancelledError:
                    current_task = asyncio.current_task()
                    if current_task is not None and current_task.cancelling():
                        raise
                    self.logger.debug("server task was already cancelled during shutdown")
                except Exception as e:
                    self.logger.warning("server task error during shutdown: {}", e)
                if self._server_task is server_task:
                    self._server_task = None
            retire_tasks = tuple(self._outbound_retire_tasks)
            if retire_tasks:
                await asyncio.gather(*retire_tasks, return_exceptions=True)
            await self._commands.close()
            self._subs.clear()
            self._conn_chats.clear()
            self._conn_default.clear()
            self._issued_tokens.clear()
            return
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
        self._retired_connections.discard(connection)
        request = connection.request
        _, query = _parse_request_path(request.path if request else "/")
        client_id = (_query_first(query, "client_id") or f"anon-{uuid.uuid4().hex[:12]}").strip()
        client_id = client_id[:128]
        default_chat_id = self._mint_chat_id()
        try:
            await connection.send(json.dumps({
                "event": "ready",
                "chat_id": default_chat_id,
                "client_id": client_id,
            }, ensure_ascii=False))
            # Register only after ready is successfully sent to avoid out-of-order sends
            self._conn_default[connection] = default_chat_id
            self._attach(connection, default_chat_id)
            # Replay state only for WebUI-audience connections; legacy
            # programmatic clients keep the fork's hydrate-free handshake.
            if self.gateway is not None and connection in self._webui_connections:
                await self._hydrate_after_subscribe(default_chat_id)

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
                    # WebSocket already authenticates at handshake time (token),
                    # so pairing is not applicable. Treat as non-DM to avoid
                    # sending pairing codes to an already-authenticated client.
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
            if self.gateway is not None:
                await self._cleanup_connection_async(connection)
            else:
                self._cleanup_connection(connection)

    # -- Inbound WebSocket envelopes ---------------------------------------

    async def _dispatch_envelope(
        self,
        connection: Any,
        client_id: str,
        envelope: dict[str, Any],
    ) -> None:
        """Dispatch by authenticated audience, never by the envelope's webui flag."""
        if self.gateway is not None and connection in self._webui_connections:
            await self._dispatch_active_envelope(connection, client_id, envelope)
            return
        await self._dispatch_legacy_envelope(connection, client_id, envelope)

    async def _dispatch_active_envelope(
        self,
        connection: Any,
        client_id: str,
        envelope: dict[str, Any],
    ) -> None:
        if not self._register_connection_outbound(connection):
            return
        try:
            await self._commands.dispatch(connection, client_id, envelope)
        except _CrossAudienceBindError:
            # Same answer as the legacy branch for foreign chat ids so the
            # error never confirms another audience's chats exist.
            await self._send_event(connection, "error", detail="unknown chat_id")
        except _TemporarySessionUnsupportedError as exc:
            await self._send_event(
                connection,
                "error",
                detail="temporary_chat_unavailable",
                chat_id=exc.chat_id,
            )

    async def _dispatch_legacy_envelope(
        self,
        connection: Any,
        client_id: str,
        envelope: dict[str, Any],
    ) -> None:
        t = envelope.get("type")
        if t == "new_chat":
            chat_id = self._mint_chat_id()
            self._attach(connection, chat_id)
            await self._send_event(connection, "attached", chat_id=chat_id)
            return
        if t == "attach":
            chat_id = envelope.get("chat_id")
            if not self._owns_chat(connection, chat_id):
                # Same answer for malformed, unknown, foreign, and
                # webui-bound chat IDs so the error never confirms another
                # connection's (or audience's) chats exist.
                await self._send_event(connection, "error", detail="unknown chat_id")
                return
            self._attach(connection, chat_id)
            await self._send_event(connection, "attached", chat_id=chat_id)
            return
        if t == "message":
            chat_id = envelope.get("chat_id")
            if not _is_valid_chat_id(chat_id):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            if not self._owns_chat(connection, chat_id):
                await self._send_event(connection, "error", detail="unknown chat_id")
                return
            content = envelope.get("content")
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

    def _owns_chat(self, connection: Any, chat_id: Any) -> bool:
        """True only when this connection registered the chat itself.

        Chat-ID syntax checks are not ownership: a connection may only
        attach to and message chats it created (its ready chat or chats
        returned by ``new_chat``). A reconnect gets a fresh identity and
        cannot reclaim IDs a previous connection used. In gateway mode a
        chat bound to the WebUI audience (live registry or persisted
        ``webui:`` session) is never ownable by a legacy connection.
        """
        if not _is_valid_chat_id(chat_id):
            return False
        if chat_id not in self._conn_chats.get(connection, set()):
            return False
        return not self._chat_is_webui_bound(chat_id)

    # -- Per-connection outbound plumbing ----------------------------------

    @staticmethod
    async def _stop_connection_writer(state: _ConnectionOutbound) -> None:
        task = state.writer
        current = asyncio.current_task()
        if task is not None and task is not current and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        while True:
            try:
                state.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        state.buffered_bytes = 0

    def _start_connection_writer(self, connection: Any, state: _ConnectionOutbound) -> None:
        if state.closing or (state.writer is not None and not state.writer.done()):
            return
        state.writer = asyncio.create_task(
            self._drain_connection_outbound(connection, state),
            name=f"websocket-outbound-{id(connection):x}",
        )

    async def _drain_connection_outbound(self, connection: Any, state: _ConnectionOutbound) -> None:
        current = asyncio.current_task()
        try:
            while not state.closing:
                try:
                    frame = state.queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    async with asyncio.timeout(_OUTBOUND_SEND_TIMEOUT_S):
                        await connection.send(frame.raw)
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    self.logger.warning("connection send timed out{}", frame.label)
                    self._schedule_connection_retirement(
                        connection,
                        state,
                        close_connection=True,
                        close_code=1013,
                        close_reason="outbound send timeout",
                    )
                    return
                except ConnectionClosed:
                    self.logger.warning("connection gone{}", frame.label)
                    self._schedule_connection_retirement(
                        connection,
                        state,
                        close_connection=False,
                    )
                    return
                except Exception:
                    self.logger.exception("send failed{}", frame.label)
                    self._schedule_connection_retirement(
                        connection,
                        state,
                        close_connection=True,
                        close_code=1011,
                        close_reason="outbound send failed",
                    )
                    return
                finally:
                    state.buffered_bytes = max(0, state.buffered_bytes - frame.utf8_bytes)
        finally:
            if state.writer is current:
                state.writer = None
            if not state.closing and not state.queue.empty():
                self._start_connection_writer(connection, state)

    def _schedule_connection_retirement(
        self,
        connection: Any,
        state: _ConnectionOutbound,
        *,
        close_connection: bool,
        close_code: int = 1000,
        close_reason: str = "",
    ) -> None:
        if state.closing:
            return
        state.closing = True
        task = asyncio.create_task(
            self._retire_connection(
                connection,
                close_connection=close_connection,
                close_code=close_code,
                close_reason=close_reason,
            ),
            name=f"websocket-retire-{id(connection):x}",
        )
        self._outbound_retire_tasks.add(task)
        task.add_done_callback(self._outbound_retire_tasks.discard)

    async def _retire_connection(
        self,
        connection: Any,
        *,
        close_connection: bool,
        close_code: int,
        close_reason: str,
    ) -> None:
        try:
            if close_connection:
                try:
                    async with asyncio.timeout(_OUTBOUND_CLOSE_TIMEOUT_S):
                        await connection.close(code=close_code, reason=close_reason)
                except TimeoutError:
                    self.logger.warning("timed out closing slow WebSocket connection")
                    with suppress(Exception):
                        connection.transport.abort()
                except ConnectionClosed:
                    pass
                except Exception:
                    self.logger.exception("failed to close WebSocket connection")
                    with suppress(Exception):
                        connection.transport.abort()
        finally:
            try:
                await self._cleanup_connection_async(connection)
            except Exception:
                self.logger.exception("failed to clean up WebSocket connection")

    async def _safe_send_to(self, connection: Any, raw: str, *, label: str = "") -> None:
        """Send one frame: direct in legacy mode (fork error semantics); queued
        per connection in gateway mode so one slow client never blocks others."""
        if self.gateway is None:
            try:
                await connection.send(raw)
            except ConnectionClosed:
                self._cleanup_connection(connection)
                self.logger.warning("connection gone{}", label)
            except Exception:
                self.logger.exception("send failed{}", label)
                raise
            return
        state = self._connection_outbound.get(connection)
        if state is None or state.closing:
            return
        utf8_bytes = len(raw.encode("utf-8"))
        if state.queue.full() or state.buffered_bytes + utf8_bytes > _OUTBOUND_QUEUE_MAX_BYTES:
            self.logger.warning(
                "disconnecting slow WebSocket connection: outbound queue full "
                "({} frames, {} bytes)",
                state.queue.qsize(),
                state.buffered_bytes,
            )
            self._schedule_connection_retirement(
                connection,
                state,
                close_connection=True,
                close_code=1013,
                close_reason="outbound queue full",
            )
            await asyncio.sleep(0)
            return
        try:
            state.queue.put_nowait(_OutboundFrame(raw, utf8_bytes, label))
        except asyncio.QueueFull:
            self._schedule_connection_retirement(
                connection,
                state,
                close_connection=True,
                close_code=1013,
                close_reason="outbound queue full",
            )
            await asyncio.sleep(0)
            return
        state.buffered_bytes += utf8_bytes
        self._start_connection_writer(connection, state)
        # Give an idle writer a chance to start without waiting for physical I/O.
        await asyncio.sleep(0)

    async def _fan_one(self, connection: Any, raw: str, *, label: str) -> None:
        """Send one frame to one subscriber.

        WebUI-audience connections use the bounded outbound queue; legacy
        connections keep the fork's direct send so ``send()`` delivery
        results stay truthful for the fork manager.
        """
        if self.gateway is not None and connection in self._webui_connections:
            await self._safe_send_to(connection, raw, label=label)
            return
        try:
            await connection.send(raw)
        except ConnectionClosed:
            self._cleanup_connection(connection)
            self.logger.warning("connection gone{}", label)
        except Exception:
            self.logger.exception("send failed{}", label)
            raise

    # -- WebUI transcript persistence hooks ---------------------------------

    def _persist_turn_transcript_event(
        self,
        chat_id: str,
        event: dict[str, Any],
        *,
        metadata: dict[str, Any] | None,
        phase: str,
        include_source: bool = False,
        transcript_overrides: dict[str, Any] | None = None,
    ) -> bool:
        """Persist one canonical turn event and retain unsafe owners on failure."""
        from nanobot.session.webui_turns import mark_websocket_turn_transcript_persistence_failed
        from nanobot.webui.metadata import WEBSOCKET_TURN_OWNER_METADATA_KEY

        if not self._temporary_chats.should_persist_transcript(chat_id):
            self._transcripts.prepare_event(
                chat_id, event, metadata=metadata, phase=phase, include_source=include_source,
            )
            return True
        persisted = self._transcripts.prepare_and_append(
            chat_id,
            event,
            metadata=metadata,
            phase=phase,
            include_source=include_source,
            transcript_overrides=transcript_overrides,
        )
        if not persisted and phase in {"answer", "complete"} and (metadata or {}).get("webui") is True:
            owner = (metadata or {}).get(WEBSOCKET_TURN_OWNER_METADATA_KEY)
            mark_websocket_turn_transcript_persistence_failed(
                chat_id,
                owner if isinstance(owner, str) else None,
            )
        return persisted

    def _persist_turn_stream_event(
        self,
        chat_id: str,
        event: dict[str, Any],
        *,
        completed_text: str | None,
        metadata: dict[str, Any] | None,
        phase: str,
        include_source: bool = False,
    ) -> bool:
        """Persist the canonical end of a live stream, never its wire chunks."""
        if not self._temporary_chats.should_persist_transcript(chat_id):
            self._transcripts.prepare_event(
                chat_id, event, metadata=metadata, phase=phase, include_source=include_source,
            )
            return True
        persisted = self._transcripts.prepare_and_append_stream_event(
            chat_id,
            event,
            completed_text=completed_text,
            metadata=metadata,
            phase=phase,
            include_source=include_source,
        )
        return persisted

    # -- Outbound WebSocket events -----------------------------------------

    async def send(self, msg: OutboundMessage) -> DeliveryResult | None:
        if self.gateway is not None and getattr(msg, "event", None) is not None:
            # Typed WebUI coordinator events ride the upstream projector seam.
            await self._outbound.send(msg)
            return None
        if msg.metadata.get("_runtime_model_updated"):
            # chat_id="*" broadcast: fanned out to every open connection of
            # both audiences (content-free; pre-existing fork and upstream
            # behavior, kept identical so either manager dialect works).
            await self.send_runtime_model_updated(
                model_name=msg.metadata.get("model"),
                model_preset=msg.metadata.get("model_preset"),
            )
            return DeliveryResult("delivered")
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
        if self.gateway is not None:
            self._enrich_and_persist_message(msg, payload)
        raw = json.dumps(payload, ensure_ascii=False)
        if not conns:
            return DeliveryResult("failed", "no websocket subscriber for chat")
        sent = 0
        for conn in conns:
            if self.gateway is not None and conn in self._webui_connections:
                await self._safe_send_to(conn, raw, label=" ")
                sent += 1
                continue
            try:
                await conn.send(raw)
                sent += 1
            except ConnectionClosed:
                self._cleanup_connection(conn)
                self.logger.warning("connection gone")
            except Exception:
                self.logger.exception("send failed")
        if sent == 0:
            return DeliveryResult("failed", "no live websocket subscriber")
        if sent < len(conns):
            # At least one subscriber got the frame while another outcome is
            # unknown — retrying could duplicate the delivered copies.
            return DeliveryResult("unknown", "some websocket sends failed")
        return DeliveryResult("delivered")

    def _enrich_and_persist_message(self, msg: OutboundMessage, payload: dict[str, Any]) -> None:
        """Gateway-mode enrichment of a fork-shaped message: turn id, signed
        media URLs, and canonical transcript persistence for WebUI threads."""
        from nanobot.webui.metadata import WEBUI_TURN_METADATA_KEY

        turn_id = msg.metadata.get(WEBUI_TURN_METADATA_KEY)
        if isinstance(turn_id, str) and turn_id:
            payload["turn_id"] = turn_id
        if msg.media:
            urls: list[dict[str, str]] = []
            for entry in msg.media:
                signed = self._media.sign_or_stage_media_path(Path(entry))
                if signed is not None:
                    urls.append(signed)
            if urls:
                payload["media_urls"] = urls
        phase = "activity" if payload.get("kind") in ("tool_hint", "progress") else "answer"
        self._persist_turn_transcript_event(
            msg.chat_id,
            payload,
            metadata=msg.metadata,
            phase=phase,
            include_source=True,
            transcript_overrides={"text": msg.content},
        )

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
        *,
        stream_id: str | None = None,
        stream_end: bool = False,
        resuming: bool = False,
        merge_next: bool = False,
    ) -> None:
        conns = list(self._subs.get(chat_id, ()))
        meta = metadata or {}
        # Fork manager passes streaming markers via metadata (positional
        # three-arg calls); the upstream manager passes kwargs. Metadata must
        # not be swallowed merely because the kwargs have defaults.
        if stream_id is None and meta.get("_stream_id") is not None:
            stream_id = meta["_stream_id"]
        stream_end = stream_end or bool(meta.get("_stream_end"))
        stream_key = (chat_id, str(stream_id or ""))
        completed_text: str | None = None
        if stream_end:
            body: dict[str, Any] = {"event": "stream_end", "chat_id": chat_id}
            buffered = (
                self._stream_text_buffers.setdefault(stream_key, [])
                if merge_next
                else self._stream_text_buffers.pop(stream_key, [])
            )
            if delta:
                buffered.append(delta)
            full_text = "".join(buffered)
            if self.gateway is not None:
                rewritten = self._media.rewrite_local_markdown_images(full_text)
                completed_text = rewritten
                if delta or rewritten != full_text:
                    body["text"] = rewritten
            elif buffered:
                body["text"] = full_text
        else:
            body = {
                "event": "delta",
                "chat_id": chat_id,
                "text": delta,
            }
            self._stream_text_buffers.setdefault(stream_key, []).append(delta)
        if stream_id is not None:
            body["stream_id"] = stream_id
        if stream_end and resuming:
            body["resuming"] = True
        if stream_end and merge_next:
            body["merge_next"] = True
        if self.gateway is not None:
            self._persist_turn_stream_event(
                chat_id,
                body,
                completed_text=completed_text,
                metadata=meta,
                phase="answer",
                include_source=True,
            )
        raw = json.dumps(body, ensure_ascii=False)
        if not conns:
            return
        for connection in conns:
            await self._fan_one(connection, raw, label=" stream ")

    async def send_reasoning_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
        *,
        stream_id: str | None = None,
    ) -> None:
        """Push one chunk of model reasoning. Mirrors ``send_delta`` shape so
        clients receive a stream that opens, updates in place, and closes —
        rendered above the active assistant bubble with a shimmer header
        until the matching ``reasoning_end`` arrives.
        """
        conns = list(self._subs.get(chat_id, ()))
        if not delta:
            return
        meta = metadata or {}
        if stream_id is None and meta.get("_stream_id") is not None:
            stream_id = meta["_stream_id"]
        body: dict[str, Any] = {
            "event": "reasoning_delta",
            "chat_id": chat_id,
            "text": delta,
        }
        if stream_id is not None:
            body["stream_id"] = stream_id
        stream_key = (chat_id, str(stream_id or ""))
        if self.gateway is not None:
            self._reasoning_text_buffers.setdefault(stream_key, []).append(delta)
            self._persist_turn_stream_event(
                chat_id,
                body,
                completed_text=None,
                metadata=meta,
                phase="reasoning",
            )
        raw = json.dumps(body, ensure_ascii=False)
        if not conns:
            return
        for connection in conns:
            await self._fan_one(connection, raw, label=" reasoning ")

    async def send_reasoning_end(
        self,
        chat_id: str,
        metadata: dict[str, Any] | None = None,
        *,
        stream_id: str | None = None,
    ) -> None:
        """Close the current reasoning stream segment for in-place renderers."""
        conns = list(self._subs.get(chat_id, ()))
        meta = metadata or {}
        if stream_id is None and meta.get("_stream_id") is not None:
            stream_id = meta["_stream_id"]
        body: dict[str, Any] = {
            "event": "reasoning_end",
            "chat_id": chat_id,
        }
        if stream_id is not None:
            body["stream_id"] = stream_id
        stream_key = (chat_id, str(stream_id or ""))
        if self.gateway is not None:
            reasoning_text = "".join(self._reasoning_text_buffers.pop(stream_key, []))
            self._persist_turn_stream_event(
                chat_id,
                body,
                completed_text=reasoning_text or None,
                metadata=meta,
                phase="reasoning",
            )
        raw = json.dumps(body, ensure_ascii=False)
        if not conns:
            return
        for connection in conns:
            await self._fan_one(connection, raw, label=" reasoning_end ")

    async def send_file_edit_events(
        self,
        chat_id: str,
        edits: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        conns = list(self._subs.get(chat_id, ()))
        payload: dict[str, Any] = {
            "event": "file_edit",
            "chat_id": chat_id,
            "edits": edits,
        }
        if self.gateway is not None:
            self._persist_turn_transcript_event(
                chat_id,
                payload,
                metadata=metadata,
                phase="activity",
            )
        raw = json.dumps(payload, ensure_ascii=False)
        if not conns:
            return
        for connection in conns:
            await self._fan_one(connection, raw, label=" file_edit ")

    async def send_projected_message(
        self,
        msg: OutboundMessage,
        progress_event: Any | None,
    ) -> None:
        """Serialize one ordinary outbound message selected by the projector."""
        from nanobot.webui.metadata import WEBUI_TURN_METADATA_KEY
        from nanobot.webui.outbound_wire import project_tool_events

        conns = list(self._subs.get(msg.chat_id, ()))
        text = msg.content
        wire_text = self._media.rewrite_local_markdown_images(text)
        payload: dict[str, Any] = {
            "event": "message",
            "chat_id": msg.chat_id,
            "text": wire_text,
        }
        turn_id = msg.metadata.get(WEBUI_TURN_METADATA_KEY)
        if isinstance(turn_id, str) and turn_id:
            payload["turn_id"] = turn_id
        if msg.media:
            payload["media"] = msg.media
            urls: list[dict[str, str]] = []
            for entry in msg.media:
                signed = self._media.sign_or_stage_media_path(Path(entry))
                if signed is not None:
                    urls.append(signed)
            if urls:
                payload["media_urls"] = urls
        if msg.reply_to:
            payload["reply_to"] = msg.reply_to
        lat = msg.metadata.get("latency_ms")
        if isinstance(lat, (int, float)):
            payload["latency_ms"] = int(lat)
        if progress_event and progress_event.tool_events:
            payload["tool_events"] = project_tool_events(progress_event.tool_events)
        agent_ui = msg.metadata.get(OUTBOUND_META_AGENT_UI)
        if agent_ui is not None:
            payload["agent_ui"] = agent_ui
        # Mark intermediate agent breadcrumbs (tool-call hints, generic
        # progress strings) so WS clients can render them as subordinate
        # trace rows rather than conversational replies.
        if progress_event and progress_event.tool_hint:
            payload["kind"] = "tool_hint"
        elif progress_event:
            payload["kind"] = "progress"
        phase = "activity" if payload.get("kind") in ("tool_hint", "progress") else "answer"
        self._persist_turn_transcript_event(
            msg.chat_id,
            payload,
            metadata=msg.metadata,
            phase=phase,
            include_source=True,
            transcript_overrides={"text": text},
        )
        raw = json.dumps(payload, ensure_ascii=False)
        if not conns:
            return
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" ")

    async def send_payload(
        self,
        chat_id: str,
        payload: dict[str, Any],
        *,
        persistence: str,
        metadata: dict[str, Any] | None = None,
        turn_owner: str | None = None,
    ) -> None:
        """Persist as requested, frame, and fan out one encoded WebUI payload."""
        from nanobot.session.webui_turns import (
            clear_websocket_turn_if_current,
            websocket_turn_transcript_persistence_failed,
        )
        from nanobot.webui.transcript import WEBUI_TRANSCRIPT_INCOMPLETE_KEY

        conns = list(self._subs.get(chat_id, ()))
        body: dict[str, Any] = dict(payload)
        if persistence == "turn_activity":
            self._persist_turn_transcript_event(
                chat_id,
                body,
                metadata=metadata,
                phase="activity",
            )
        elif persistence == "turn_complete":
            canonical_webui_turn = (metadata or {}).get("webui") is True
            prior_persistence_failure = (
                canonical_webui_turn
                and websocket_turn_transcript_persistence_failed(chat_id, turn_owner)
            )
            persisted = self._persist_turn_transcript_event(
                chat_id,
                body,
                metadata=metadata,
                phase="complete",
                transcript_overrides=(
                    {WEBUI_TRANSCRIPT_INCOMPLETE_KEY: True}
                    if prior_persistence_failure
                    else None
                ),
            )
            if persisted:
                # A successful completion either has a complete transcript or now
                # carries a durable incomplete marker. The HTTP replay path can
                # recover the latter from session history after a gateway restart.
                clear_websocket_turn_if_current(chat_id, turn_owner)
            self._clear_stream_buffers(chat_id)
        raw = json.dumps(body, ensure_ascii=False)
        if not conns:
            return
        for connection in conns:
            await self._safe_send_to(connection, raw, label=f" {body['event']} ")

    async def send_goal_state(self, chat_id: str, blob: dict[str, Any]) -> None:
        """Push persisted goal-state snapshot for *chat_id* (multi-chat isolation)."""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body = {"event": "goal_state", "chat_id": chat_id, "goal_state": blob}
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" goal_state ")

    async def send_goal_status(
        self,
        chat_id: str,
        status: str,
        *,
        started_at: float | None = None,
        turn_id: str | None = None,
    ) -> None:
        """Notify subscribed clients that a turn started or finished (wall-clock hint)."""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body: dict[str, Any] = {
            "event": "goal_status",
            "chat_id": chat_id,
            "status": status,
        }
        if status == "running" and started_at is not None:
            body["started_at"] = started_at
        if turn_id:
            body["turn_id"] = turn_id
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" goal_status ")

    async def send_session_updated(self, chat_id: str, *, scope: str | None = None) -> None:
        """Notify WebUI clients that a session row should refresh."""
        conns = list(self._conn_chats)
        if not conns:
            return
        body: dict[str, Any] = {"event": "session_updated", "chat_id": chat_id}
        if scope:
            body["scope"] = scope
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" session_updated ")

    async def send_user_input(
        self,
        chat_id: str,
        *,
        content: str,
        created_at_ms: int,
        provenance: dict[str, Any],
    ) -> None:
        """Project user input produced outside a WebSocket connection."""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body: dict[str, Any] = {
            "event": "user_message",
            "chat_id": chat_id,
            "text": content,
            "created_at_ms": created_at_ms,
            "starts_turn": False,
        }
        if provenance:
            body["provenance"] = provenance
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" user_message ")

    async def send_runtime_model_updated(
        self,
        *,
        model_name: Any,
        model_preset: Any = None,
    ) -> None:
        """Broadcast runtime model changes to every open websocket connection."""
        conns = list(self._conn_chats)
        if not conns or not isinstance(model_name, str) or not model_name.strip():
            return
        body: dict[str, Any] = {
            "event": "runtime_model_updated",
            "model_name": model_name.strip(),
        }
        if isinstance(model_preset, str) and model_preset.strip():
            body["model_preset"] = model_preset.strip()
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._fan_one(connection, raw, label=" runtime_model_updated ")

    async def send_turn_model_updated(
        self,
        chat_id: str,
        *,
        model_name: Any,
        model_preset: Any = None,
        context_window_tokens: Any = None,
        fallback: bool = False,
        reauth_provider: str | None = None,
    ) -> None:
        """Notify one chat's subscribers which model is handling its current request."""
        conns = list(self._subs.get(chat_id, ()))
        if (
            not conns
            or not isinstance(model_name, str)
            or not model_name.strip()
        ):
            return
        body: dict[str, Any] = {
            "event": "turn_model_updated",
            "chat_id": chat_id,
            "model_name": model_name.strip(),
        }
        if isinstance(model_preset, str) and model_preset.strip():
            body["model_preset"] = model_preset.strip()
        if isinstance(context_window_tokens, int) and context_window_tokens > 0:
            body["context_window_tokens"] = context_window_tokens
        if fallback:
            body["fallback"] = True
            if reauth_provider:
                body["reauth_provider"] = reauth_provider
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" turn_model_updated ")
