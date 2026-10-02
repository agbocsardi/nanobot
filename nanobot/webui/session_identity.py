"""Stable mapping between public WebUI chat IDs and persisted session keys."""

from __future__ import annotations

import re
from typing import Any, TypeGuard

# Fork namespace decision (issue #38): WebUI history lives under the literal
# ``webui:`` prefix so it stays isolated from legacy ``websocket:`` history and
# from Telegram. Changing the constant alone is not enough — inbound dispatch
# must pass session_key=webui_session_key(chat_id) explicitly (the loop would
# otherwise default to websocket:<id>).
WEBUI_SESSION_STORAGE_PREFIX = "webui:"

# Fork hardening: no ``:`` — the only alias-forming character under
# safe_filename(key.replace(":", "_")) — so a client-supplied id like ``a:b``
# can never address the file of key ``webui:a_b``. Canonical new-chat ids are
# server-minted UUIDs; legacy ids containing ``:`` are rejected.
_WEBUI_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def is_valid_webui_chat_id(value: Any) -> TypeGuard[str]:
    """Validate the compact chat IDs accepted by the WebUI protocol."""
    return isinstance(value, str) and _WEBUI_CHAT_ID_RE.fullmatch(value) is not None


def webui_session_key(chat_id: str) -> str:
    """Return the backward-compatible persisted key for a WebUI chat."""
    return f"{WEBUI_SESSION_STORAGE_PREFIX}{chat_id}"


def is_webui_session_key(session_key: str) -> bool:
    """Return whether *session_key* belongs to the WebUI session namespace."""
    return session_key.startswith(WEBUI_SESSION_STORAGE_PREFIX)


def webui_chat_id(session_key: str) -> str | None:
    """Extract a non-empty WebUI chat ID from a persisted session key."""
    if not is_webui_session_key(session_key):
        return None
    chat_id = session_key.removeprefix(WEBUI_SESSION_STORAGE_PREFIX)
    return chat_id or None


LEGACY_WEBSOCKET_STORAGE_PREFIX = "websocket:"


def legacy_websocket_session_key(chat_id: str) -> str:
    """Return the legacy-namespace key for a chat id."""
    return f"{LEGACY_WEBSOCKET_STORAGE_PREFIX}{chat_id}"


def webui_binding_conflicts_with_legacy(sessions: Any, chat_id: str) -> bool:
    """True when legacy ``websocket:<chat_id>`` evidence exists.

    Binding invariant (issue #38): outbound fanout is by chat_id, so a WebUI
    client must never bind to a chat id whose legacy-namespace session exists
    — cached (pending legacy turn, maybe not yet saved) or persisted (restart
    survivor). UUID randomness of new mints is not the boundary; callers must
    check this at every attach/message/fork binding path.
    """
    legacy_key = legacy_websocket_session_key(chat_id)
    if sessions.get_cached(legacy_key) is not None:
        return True
    return bool(sessions.session_exists(legacy_key))
