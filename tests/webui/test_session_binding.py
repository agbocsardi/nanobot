"""Session namespace, alias rejection, and binding-conflict invariants.

Issue #38: WebUI history lives under the literal ``webui:`` prefix; supplied
ids may not alias other keys through the storage-stem mapping; and a WebUI
binding must be refused whenever legacy ``websocket:<chat_id>`` evidence
exists (cached pending turn or persisted restart survivor), because outbound
fanout is by chat_id.
"""

from __future__ import annotations

import json
from pathlib import Path

from nanobot.session.manager import SessionManager
from nanobot.webui.session_identity import (
    is_valid_webui_chat_id,
    is_webui_session_key,
    legacy_websocket_session_key,
    webui_binding_conflicts_with_legacy,
    webui_chat_id,
    webui_session_key,
)


def _write_session(sessions: SessionManager, key: str) -> Path:
    session = sessions.get_or_create(key)
    session.add_message("user", "hello")
    sessions.save(session, fsync=True)
    return sessions._get_session_path(key)  # noqa: SLF001 - test seam


def test_namespace_prefix_is_literal_webui() -> None:
    assert webui_session_key("abc") == "webui:abc"
    assert is_webui_session_key("webui:abc")
    assert not is_webui_session_key("websocket:abc")
    assert webui_chat_id("webui:abc") == "abc"
    assert webui_chat_id("websocket:abc") is None


def test_supplied_ids_reject_alias_forming_characters() -> None:
    assert is_valid_webui_chat_id("4f4d0f1e-8f0a-4a5e-9d1f-2b3c4d5e6f70")
    assert is_valid_webui_chat_id("a_b-c")
    # ':' maps to '_' under safe_filename(key.replace(":", "_")) — rejected.
    assert not is_valid_webui_chat_id("a:b")
    assert not is_valid_webui_chat_id("")
    assert not is_valid_webui_chat_id("a/b")
    assert not is_valid_webui_chat_id(None)


def test_alias_filename_cannot_impersonate_requested_key(tmp_path) -> None:
    sessions = SessionManager(workspace=tmp_path)
    # Seed the canonical key whose storage stem an alias would collide with.
    _write_session(sessions, "webui:a_b")
    sessions.invalidate("webui:a_b")

    # The aliased request resolves to the same file, but the metadata-line key
    # differs, so it is not evidence for the requested key.
    assert sessions.session_exists("webui:a_b") is True
    assert sessions.session_exists("webui:a:b") is False


def test_written_metadata_key_matches_webui_session_key(tmp_path) -> None:
    sessions = SessionManager(workspace=tmp_path)
    chat_id = "4f4d0f1e-8f0a-4a5e-9d1f-2b3c4d5e6f70"
    path = _write_session(sessions, webui_session_key(chat_id))
    header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert header["_type"] == "metadata"
    assert header["key"] == webui_session_key(chat_id)


def test_session_exists_is_side_effect_free(tmp_path) -> None:
    sessions = SessionManager(workspace=tmp_path)
    assert sessions.session_exists("webui:missing") is False
    assert list(sessions.sessions_dir.glob("*.jsonl")) == []
    # And for the legacy namespace probe too.
    assert sessions.session_exists("websocket:missing") is False
    assert list(sessions.sessions_dir.glob("*.jsonl")) == []


def test_persisted_legacy_session_blocks_webui_binding(tmp_path) -> None:
    sessions = SessionManager(workspace=tmp_path)
    _write_session(sessions, legacy_websocket_session_key("shared-id"))
    sessions.invalidate(legacy_websocket_session_key("shared-id"))

    assert webui_binding_conflicts_with_legacy(sessions, "shared-id") is True


def test_cached_legacy_turn_blocks_webui_binding_without_saved_file(tmp_path) -> None:
    sessions = SessionManager(workspace=tmp_path)
    # A pending legacy turn exists only in memory until it is saved.
    sessions.get_or_create(legacy_websocket_session_key("pending-id"))
    assert not sessions._get_session_path(  # noqa: SLF001 - test seam
        legacy_websocket_session_key("pending-id")
    ).exists()

    assert webui_binding_conflicts_with_legacy(sessions, "pending-id") is True


def test_equal_ids_in_both_namespaces_still_conflict(tmp_path) -> None:
    """webui:X existing does not license binding while websocket:X exists."""
    sessions = SessionManager(workspace=tmp_path)
    _write_session(sessions, webui_session_key("both"))
    _write_session(sessions, legacy_websocket_session_key("both"))
    sessions.invalidate(webui_session_key("both"))
    sessions.invalidate(legacy_websocket_session_key("both"))

    assert sessions.session_exists(webui_session_key("both")) is True
    assert webui_binding_conflicts_with_legacy(sessions, "both") is True


def test_clean_webui_chat_has_no_conflict(tmp_path) -> None:
    sessions = SessionManager(workspace=tmp_path)
    _write_session(sessions, webui_session_key("clean"))
    sessions.invalidate(webui_session_key("clean"))

    assert webui_binding_conflicts_with_legacy(sessions, "clean") is False
