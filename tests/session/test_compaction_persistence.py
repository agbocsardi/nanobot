"""Owning-loop checkpoint commits include pending batches atomically."""

from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.session.compaction import CHECKPOINTS, latest_checkpoint
from tests.session.test_compaction import CountingProvider, SideEffect, SummaryProvider, transcript


async def test_pending_batches_commit_once_and_reload_with_checkpoint(tmp_path):
    tool = SideEffect()
    provider = CountingProvider(tool)
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     model="physical-model", context_window_tokens=12000)
    loop.consolidator.flush_before_compaction = AsyncMock()
    tools = ToolRegistry()
    tools.register(tool)
    session = loop.sessions.get_or_create("test:pending")
    content = "Execute ten actions exactly once. " * 250
    session.add_message("user", content)
    session.metadata[loop._PENDING_USER_TURN_KEY] = True
    loop.sessions.save(session)
    final, _, messages, reason, _ = await loop._run_agent_loop(
        [{"role": "system", "content": "instructions"}, {"role": "user", "content": content}],
        session=session, tools=tools,
    )
    assert reason == "completed"
    assert final == "all actions completed"
    assert tool.executed == list(range(10))
    assert len(session.metadata[CHECKPOINTS]) >= 3
    # Compaction persisted the pending prefix; final save must deduplicate it.
    loop._save_turn(session, messages, 2)
    loop._clear_runtime_checkpoint(session)
    loop.sessions.save(session)
    assert len(session.messages) == 22
    assert len({m["_entry_id"] for m in session.messages}) == 22
    projection = session.get_history(projected=True)
    loop.sessions.invalidate(session.key)
    loaded = loop.sessions.get_or_create(session.key)
    assert loaded.get_history(projected=True) == projection
    assert [m["content"] for m in loaded.messages if m["role"] == "tool"] == [
        m["content"] for m in session.messages if m["role"] == "tool"
    ]
    assert [
        tc["function"]["arguments"]
        for m in loaded.messages for tc in m.get("tool_calls", [])
    ] == [
        tc["function"]["arguments"]
        for m in session.messages for tc in m.get("tool_calls", [])
    ]
    loop.consolidator.flush_before_compaction.assert_awaited()


def test_metadata_rewrite_updates_cache_before_releasing_file_lock(tmp_path):
    from nanobot.session.manager import SessionManager

    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("test:metadata-lock")
    session.add_message("user", "unchanged transcript")
    sessions.save(session)
    original_lock = sessions._session_files_lock

    @contextmanager
    def observe_unlock():
        with original_lock:
            yield
            # A concurrent full save must never see old cached metadata after
            # the metadata-only writer releases the common canonical-file lock.
            assert session.metadata["title"] == "updated"

    sessions._session_files_lock = observe_unlock()
    assert sessions.update_session_metadata(session.key, {"title": "updated"})
    assert session.messages[0]["content"] == "unchanged transcript"


async def test_failed_atomic_save_leaves_transcript_and_checkpoint_unchanged(tmp_path, monkeypatch):
    provider = SummaryProvider()
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     model="physical-model", context_window_tokens=8192)
    loop.consolidator.flush_before_compaction = AsyncMock()
    session = loop.sessions.get_or_create("test:atomic")
    session.messages = transcript()
    loop.sessions.save(session)
    original = deepcopy(session.messages)
    save = loop.sessions.save
    failed = False

    def fail_once(candidate, **kwargs):
        nonlocal failed
        if latest_checkpoint(candidate.metadata) and not failed:
            failed = True
            raise OSError("simulated fsync failure")
        save(candidate, **kwargs)

    monkeypatch.setattr(loop.sessions, "save", fail_once)
    with pytest.raises(OSError, match="simulated"):
        await loop.consolidator.compact_session(session)
    assert session.messages == original
    assert not latest_checkpoint(session.metadata)
    loop.sessions.invalidate(session.key)
    loaded = loop.sessions.get_or_create(session.key)
    assert loaded.messages == original
    assert not latest_checkpoint(loaded.metadata)

    # Retry commits the checkpoint, while the pre-save archive append is deduplicated.
    assert await loop.consolidator.compact_session(loaded)
    archives = [
        entry for entry in loop.consolidator.store._read_entries()
        if entry.get("session_key") == session.key
        and entry.get("kind") == "conversation"
    ]
    assert len(archives) == 1
