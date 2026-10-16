"""Checkpoint-safe active-file bounds preserve a lossless redacted audit trail."""

import json
from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from nanobot.session.compaction import CHECKPOINTS, ensure_ids
from nanobot.session.manager import Session, SessionManager


def checkpoint(session, first):
    ensure_ids(session.messages)
    session.metadata[CHECKPOINTS] = [{
        "version": 1, "summary": "earlier task continuity", "type": "compaction", "id": "checkpoint",
        "first_kept_entry_id": session.messages[first]["_entry_id"],
        "source_end_id": session.messages[-1]["_entry_id"],
    }]


def test_save_caps_hidden_prefix_without_changing_projection(tmp_path):
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("test:bounded")
    for i in range(5000):
        session.add_message("user" if i % 2 == 0 else "assistant", f"record {i}")
    checkpoint(session, 4000)
    original = deepcopy(session.messages)
    expected = session.get_history(projected=True)
    first_id = session.metadata[CHECKPOINTS][-1]["first_kept_entry_id"]
    sessions.save(session, fsync=True)
    assert len(session.messages) == 2000
    assert first_id in {m["_entry_id"] for m in session.messages}
    assert session.get_history(projected=True) == expected
    files = list((sessions.sessions_dir / "archive").rglob("*.jsonl"))
    assert len(files) == 1
    records = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert records[0] == {"_type": "transcript_archive", "key": session.key}
    assert records[1:] == original[:3000]
    assert records[1:] + session.messages == original
    sessions.invalidate(session.key)
    assert sessions.get_or_create(session.key).get_history(projected=True) == expected


def test_cap_never_removes_system_or_retained_messages():
    session = Session("test:fixed", messages=[
        {"role": "system", "content": "fixed instructions"},
        *({"role": "user", "content": f"record {i}"} for i in range(12)),
    ])
    checkpoint(session, 3)
    expected = session.get_history(projected=True)
    archived = MagicMock()
    session.enforce_file_cap(archived, limit=4)
    assert len(session.messages) == 11  # Active suffix alone exceeds the soft cap.
    assert session.messages[0]["role"] == "system"
    assert session.get_history(projected=True) == expected
    assert [m["content"] for m in archived.call_args.args[0]] == ["record 0", "record 1"]


def test_cap_requires_archive_and_leaves_state_on_archive_failure():
    session = Session("test:failure", messages=[{"role": "user", "content": str(i)} for i in range(8)])
    checkpoint(session, 6)
    original = deepcopy(session)
    session.enforce_file_cap(limit=4)
    assert session == original
    with pytest.raises(OSError, match="archive failed"):
        session.enforce_file_cap(MagicMock(side_effect=OSError("archive failed")), limit=4)
    assert session == original


def test_archive_batches_are_idempotent(tmp_path):
    sessions = SessionManager(tmp_path)
    records = [{"role": "user", "content": "audit evidence", "_entry_id": "stable"}]
    sessions._archive_session_prefix("test:audit", records)
    sessions._archive_session_prefix("test:audit", records)
    assert len(list((sessions.sessions_dir / "archive").rglob("*.jsonl"))) == 1


def test_trim_sink_is_the_audit_archive_only(tmp_path, monkeypatch):
    """The active-file trim routes trimmed originals to the audit archive only.

    Deliberate contract (see ``SessionManager._archive_session_prefix``): a
    trimmed prefix is already hidden behind a checkpoint and carried by its
    summary, so it is preserved losslessly on disk but is not appended to
    ``memory/history.jsonl`` for consolidation. Re-wiring the trim back through
    ``MemoryStore.raw_archive`` fails here.
    """
    from nanobot.agent.memory import MemoryStore

    raw_archive = MagicMock()
    monkeypatch.setattr(MemoryStore, "raw_archive", raw_archive)
    monkeypatch.setattr("nanobot.session.manager.FILE_MAX_MESSAGES", 4)

    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("test:audit-only")
    for i in range(8):
        session.add_message("user" if i % 2 == 0 else "assistant", f"record {i}")
    checkpoint(session, 4)
    sessions.save(session)

    assert len(session.messages) == 4
    assert len(list((sessions.sessions_dir / "archive").rglob("*.jsonl"))) == 1
    raw_archive.assert_not_called()


async def test_final_save_does_not_reappend_a_committed_archived_run_prefix(tmp_path, monkeypatch):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from tests.session.test_compaction import SummaryProvider

    monkeypatch.setattr("nanobot.session.manager.FILE_MAX_MESSAGES", 4)
    loop = AgentLoop(bus=MessageBus(), provider=SummaryProvider(), workspace=tmp_path,
                     model="physical-model", context_window_tokens=32768)
    session = loop.sessions.get_or_create("test:run-prefix")
    source = [{"role": "user", "content": str(i)} for i in range(10)]
    ensure_ids(source)
    session.messages = deepcopy(source)
    checkpoint(session, 8)
    loop.sessions.save(session)
    assert len(session.messages) == 4
    loop._save_turn(session, [*source, {"role": "assistant", "content": "final answer"}], 0)
    loop.sessions.save(session)
    assert [m["content"] for m in session.messages] == ["7", "8", "9", "final answer"]


def test_cap_archives_tool_history_without_hidden_reasoning(tmp_path, monkeypatch):
    monkeypatch.setattr("nanobot.session.manager.FILE_MAX_MESSAGES", 2)
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("test:privacy")
    session.messages = [
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "", "reasoning_content": "hidden-secret",
         "tool_calls": [{"id": "call", "type": "function", "function": {
             "name": "exec", "arguments": '{"command":"argument-secret"}'}}]},
        {"role": "tool", "name": "exec", "tool_call_id": "call", "content": "result-secret"},
        {"role": "assistant", "content": "done"}, {"role": "user", "content": "continue"},
    ]
    checkpoint(session, 4)
    sessions.save(session)
    path = next((sessions.sessions_dir / "archive").rglob("*.jsonl"))
    text = path.read_text()
    assert "hidden-secret" not in text
    assert "argument-secret" in text
    assert "result-secret" in text
    records = [json.loads(line) for line in text.splitlines()][1:]
    assert len(records) == 3
    assert records[1]["tool_calls"][0]["function"]["arguments"] == '{"command":"argument-secret"}'
    assert records[2]["tool_call_id"] == "call"
    assert records[2]["content"] == "result-secret"


async def test_message_count_pressure_creates_checkpoint_before_safe_trimming(tmp_path, monkeypatch):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from tests.session.test_compaction import SummaryProvider

    monkeypatch.setattr("nanobot.agent.memory.FILE_MAX_MESSAGES", 4)
    monkeypatch.setattr("nanobot.session.manager.FILE_MAX_MESSAGES", 4)
    loop = AgentLoop(bus=MessageBus(), provider=SummaryProvider(), workspace=tmp_path,
                     model="physical-model", context_window_tokens=32768)
    from unittest.mock import AsyncMock

    loop.consolidator.flush_before_compaction = AsyncMock()
    session = loop.sessions.get_or_create("test:count-pressure")
    for i in range(10):
        session.add_message("user", f"request {i} " * 20)
        session.add_message("assistant", f"answer {i} " * 20)
    loop.sessions.save(session)
    assert len(session.messages) == 20  # No checkpoint: no destructive fallback.
    await loop.consolidator.maybe_consolidate_by_tokens(session)
    assert session.metadata[CHECKPOINTS]
    first = session.metadata[CHECKPOINTS][-1]["first_kept_entry_id"]
    assert session.messages[0]["_entry_id"] == first
    assert len(session.messages) < 20
    assert len(list((loop.sessions.sessions_dir / "archive").rglob("*.jsonl"))) == 1
