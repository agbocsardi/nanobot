"""WebUI chat fork orchestration."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol

from loguru import logger

from nanobot.session.manager import SessionManager
from nanobot.session.webui_turns import WEBUI_TITLE_METADATA_KEY, clean_generated_title
from nanobot.webui.session_identity import (
    is_valid_webui_chat_id,
    webui_binding_conflicts_with_legacy,
    webui_session_key,
)
from nanobot.webui.transcript import (
    append_fork_marker,
    delete_webui_transcript,
    fork_transcript_before_user_index,
    write_session_messages_as_transcript,
)

if TYPE_CHECKING:
    from websockets.asyncio.server import ServerConnection

    from nanobot.webui.gateway_services import GatewayServices


class WebUIForkHost(Protocol):
    gateway: GatewayServices

    async def send_webui_protocol_error(
        self,
        connection: ServerConnection,
        detail: str,
    ) -> None: ...

    async def attach_webui_fork(
        self,
        connection: ServerConnection,
        *,
        fork_id: str,
        fork_key: str,
    ) -> None: ...


def _mint_fork_chat_id(session_manager: SessionManager) -> str:
    """Mint a canonical fork chat id with a bounded collision check."""
    for _ in range(8):
        candidate = str(uuid.uuid4())
        if session_manager.session_exists(webui_session_key(candidate)):
            continue
        if webui_binding_conflicts_with_legacy(session_manager, candidate):
            continue
        return candidate
    raise RuntimeError("unable to mint a collision-free fork chat id")


def create_webui_chat_fork(
    session_manager: SessionManager,
    *,
    source_chat_id: str,
    before_user_index: int,
    title: str | None = None,
) -> tuple[str, str] | None:
    """Return ``(chat_id, session_key)`` for a new fork, or ``None`` for bad input."""
    # Server-minted canonical id with a bounded collision check against both
    # namespaces (UUID randomness is not the binding boundary).
    new_id = _mint_fork_chat_id(session_manager)
    source_key = webui_session_key(source_chat_id)
    target_key = webui_session_key(new_id)
    try:
        forked = session_manager.fork_session_before_user_index(
            source_key,
            target_key,
            before_user_index,
        )
        if forked is None:
            return None

        transcript_ok = fork_transcript_before_user_index(
            source_key,
            target_key,
            before_user_index,
        )
        if not transcript_ok:
            write_session_messages_as_transcript(target_key, forked.messages)
        append_fork_marker(target_key)

        fork_title = clean_generated_title(title)
        if fork_title:
            forked.metadata[WEBUI_TITLE_METADATA_KEY] = fork_title
            session_manager.save(forked, fsync=True)
    except Exception:
        delete_webui_transcript(target_key)
        session_manager.delete_session(target_key)
        raise
    return new_id, target_key


async def handle_webui_fork_chat(
    channel: WebUIForkHost,
    connection: ServerConnection,
    envelope: Mapping[str, Any],
) -> None:
    """Handle the WebUI ``fork_chat`` websocket command.

    ``websocket.py`` owns the transport. This module owns WebUI fork semantics:
    validate the request, clone session/transcript state, attach the new chat,
    and hydrate the client.
    """
    source_chat_id = envelope.get("source_chat_id")
    raw_index = envelope.get("before_user_index")
    if not is_valid_webui_chat_id(source_chat_id):
        await channel.send_webui_protocol_error(connection, "invalid source_chat_id")
        return
    if isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index < 0:
        await channel.send_webui_protocol_error(connection, "invalid before_user_index")
        return

    session_manager = channel.gateway.session_manager
    if session_manager is None:
        await channel.send_webui_protocol_error(connection, "session_manager_unavailable")
        return

    # Binding invariant: never clone a source whose legacy websocket: session
    # exists — chat-id fanout cannot separate the audiences, and the fork copy
    # would leak legacy history into the WebUI namespace.
    if webui_binding_conflicts_with_legacy(session_manager, source_chat_id):
        await channel.send_webui_protocol_error(connection, "chat_id_unavailable")
        return

    try:
        forked = create_webui_chat_fork(
            session_manager,
            source_chat_id=source_chat_id,
            before_user_index=raw_index,
            title=envelope.get("title") if isinstance(envelope.get("title"), str) else None,
        )
        if forked is None:
            await channel.send_webui_protocol_error(connection, "invalid fork source or index")
            return
        fork_id, fork_key = forked
    except Exception as exc:
        logger.warning("fork_chat failed: {}", exc)
        await channel.send_webui_protocol_error(connection, "fork_chat_failed")
        return

    await channel.attach_webui_fork(
        connection,
        fork_id=fork_id,
        fork_key=fork_key,
    )
