"""Title projection must never materialize a legacy ``websocket:`` session.

Regression (issue #38 binding invariant): the auto-title path derived its
per-chat target key from ``channel`` (``websocket:<chat_id>``) and called
``get_or_create`` on it. That wrote a metadata-only legacy session, which
``webui_binding_conflicts_with_legacy`` correctly reads as evidence that the
chat id belongs to the legacy audience — so the WebUI permanently refused to
attach the very chat it had just titled ("This chat cannot be opened here").
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nanobot.session.manager import SessionManager
from nanobot.session.webui_turns import (
    WEBUI_SESSION_METADATA_KEY,
    maybe_generate_webui_title_after_turn,
)
from nanobot.webui.session_identity import (
    legacy_websocket_session_key,
    webui_binding_conflicts_with_legacy,
    webui_session_key,
)


class _StubProvider:
    """Minimal provider returning a fixed title, recording nothing else."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.calls = 0

    async def chat_stream_with_retry(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1

        class _Response:
            content = self.title
            finish_reason = "stop"

        return _Response()


def _seed_webui_chat(sessions: SessionManager, chat_id: str) -> str:
    key = webui_session_key(chat_id)
    session = sessions.get_or_create(key)
    session.metadata[WEBUI_SESSION_METADATA_KEY] = True
    session.add_message("user", "how do I plan meals around a luteal phase?")
    session.add_message("assistant", "Start by tracking protein and iron.")
    sessions.save(session, fsync=True)
    return key


def _legacy_evidence(sessions: SessionManager, chat_id: str) -> bool:
    return webui_binding_conflicts_with_legacy(sessions, chat_id)


def test_title_generation_does_not_create_legacy_session(tmp_path) -> None:
    sessions = SessionManager(workspace=tmp_path)
    chat_id = "11111111-2222-3333-4444-555555555555"
    key = _seed_webui_chat(sessions, chat_id)
    assert _legacy_evidence(sessions, chat_id) is False

    provider = _StubProvider("Meal planning around luteal phase")
    generated = asyncio.run(
        maybe_generate_webui_title_after_turn(
            channel="websocket",
            chat_id=chat_id,
            metadata={WEBUI_SESSION_METADATA_KEY: True},
            sessions=sessions,
            session_key=key,
            provider=provider,  # type: ignore[arg-type]
            model="test-model",
        )
    )

    assert generated is True
    assert provider.calls == 1
    # The title lands on the routed WebUI session ...
    assert sessions.get_existing(key) is not None
    assert sessions.get_existing(key).metadata["title"] == "Meal planning around luteal phase"  # type: ignore[union-attr]
    # ... and no legacy evidence was produced for that chat id.
    legacy_key = legacy_websocket_session_key(chat_id)
    assert sessions.get_existing(legacy_key) is None
    assert not sessions._get_session_path(legacy_key).exists()  # noqa: SLF001 - test seam
    assert _legacy_evidence(sessions, chat_id) is False


def test_title_generation_never_creates_an_absent_projection(tmp_path) -> None:
    sessions = SessionManager(workspace=tmp_path)
    chat_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    key = _seed_webui_chat(sessions, chat_id)
    absent_projection = webui_session_key("99999999-0000-0000-0000-000000000000")

    provider = _StubProvider("Should not be written")
    generated = asyncio.run(
        maybe_generate_webui_title_after_turn(
            channel="websocket",
            chat_id=chat_id,
            metadata={WEBUI_SESSION_METADATA_KEY: True},
            sessions=sessions,
            session_key=absent_projection,
            provider=provider,  # type: ignore[arg-type]
            model="test-model",
        )
    )

    assert generated is False
    assert provider.calls == 0
    # Reading a projection may never bring it into existence.
    assert sessions.get_existing(absent_projection) is None
    assert not sessions._get_session_path(absent_projection).exists()  # noqa: SLF001


def test_get_existing_does_not_persist_a_new_session(tmp_path) -> None:
    sessions = SessionManager(workspace=tmp_path)
    assert sessions.get_existing("webui:never-written") is None
    assert not sessions._get_session_path("webui:never-written").exists()  # noqa: SLF001


def test_get_existing_loads_persisted_session(tmp_path) -> None:
    sessions = SessionManager(workspace=tmp_path)
    key = _seed_webui_chat(sessions, "cccccccc-1111-2222-3333-444444444444")
    sessions.invalidate(key)
    assert sessions.get_existing(key) is not None
    assert sessions.get_existing(key).messages  # type: ignore[union-attr]


@pytest.mark.parametrize("channel", ["telegram"])
def test_title_generation_ignores_other_channels(tmp_path, channel: str) -> None:
    sessions = SessionManager(workspace=tmp_path)
    chat_id = "dddddddd-1111-2222-3333-444444444444"
    key = webui_session_key(chat_id)
    provider = _StubProvider("nope")

    generated = asyncio.run(
        maybe_generate_webui_title_after_turn(
            channel=channel,
            chat_id=chat_id,
            metadata={WEBUI_SESSION_METADATA_KEY: True},
            sessions=sessions,
            session_key=key,
            provider=provider,  # type: ignore[arg-type]
            model="test-model",
        )
    )
    assert generated is False
    assert provider.calls == 0
