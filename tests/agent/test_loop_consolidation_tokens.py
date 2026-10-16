"""Threshold compaction shares checkpoint generation, never memory archival."""

from unittest.mock import AsyncMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.session.compaction import CompactionError, RequestBudget
from tests.session.test_compaction import SummaryProvider, transcript


def make_loop(tmp_path):
    provider = SummaryProvider()
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     model="physical-model", context_window_tokens=32768)
    loop.consolidator.flush_before_compaction = AsyncMock()
    return loop


async def test_prompt_below_threshold_does_not_consolidate(tmp_path):
    loop = make_loop(tmp_path)
    loop.consolidator.compact_session = AsyncMock()
    assert await loop.process_direct("hello", session_key="cli:test")
    loop.consolidator.compact_session.assert_not_awaited()


async def test_threshold_delegates_to_shared_checkpoint_operation(tmp_path):
    loop = make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:test")
    session.add_message("user", "question")
    loop.consolidator._build_messages = lambda **kw: [{"role": "system", "content": "large " * 50000}]
    loop.consolidator.compact_session = AsyncMock()
    await loop.consolidator.maybe_consolidate_by_tokens(session)
    loop.consolidator.compact_session.assert_awaited_once()
    kwargs = loop.consolidator.compact_session.await_args.kwargs
    snapshot = kwargs["budget_snapshot"]
    assert snapshot.model == "physical-model"
    assert snapshot.window == 32768
    assert snapshot.output == 256


async def test_threshold_does_not_archive_or_advance_cursor(tmp_path):
    loop = make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:test")
    session.messages = transcript()
    loop.sessions.save(session)
    loop.consolidator.archive = AsyncMock()
    await loop.consolidator.maybe_consolidate_by_tokens(session)
    assert session.last_consolidated == 0
    loop.consolidator.archive.assert_not_awaited()
    assert loop.context.memory.read_unprocessed_history(since_cursor=0) == []


async def test_checkpoint_summary_is_projected_not_injected_twice(tmp_path):
    loop = make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:test")
    session.messages = transcript()
    loop.sessions.save(session)
    assert await loop.consolidator.compact_idle_session(session.key)
    prepared, pending = loop.auto_compact.prepare_session(session, session.key)
    assert prepared is session
    assert pending is None
    history = session.get_history(projected=True, include_entry_ids=True)
    assert sum(bool(m.get("_compaction_summary")) for m in history) == 1
    assert "Original goal" not in str(history)


async def test_legacy_pending_summary_participates_in_projection(tmp_path):
    loop = make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:test")
    session.add_message("user", "recent question")
    session.metadata["_last_summary"] = {"text": "legacy task continuity", "last_active": session.updated_at.isoformat()}
    history = session.get_history(projected=True)
    assert history[0]["content"].endswith("legacy task continuity")
    assert history[1]["content"] == "recent question"


async def test_preflight_consolidation_before_llm_call(tmp_path):
    loop = make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:test")
    session.add_message("user", "old question")
    order = []
    loop.consolidator._build_messages = lambda **kw: [{"role": "system", "content": "large " * 50000}]

    async def compact(*args, **kwargs):
        order.append("compact")

    original = loop.provider.chat_with_retry

    async def chat(**kwargs):
        order.append("model")
        return await original(**kwargs)

    loop.consolidator.compact_session = compact
    loop.provider.chat_with_retry = chat
    assert await loop.process_direct("new question", session_key=session.key)
    assert order[:2] == ["compact", "model"]


async def test_projected_context_is_not_message_count_truncated(tmp_path):
    loop = make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:test")
    for i in range(80):
        session.add_message("user", f"question {i}")
        session.add_message("assistant", f"answer {i}")
    assert len(session.get_history(projected=True, max_messages=4, max_tokens=10)) == 160


def test_output_reserve_is_not_silently_clamped(tmp_path):
    provider = SummaryProvider()
    with pytest.raises(CompactionError, match="Invalid request budget"):
        RequestBudget.freeze(provider, "physical-model", 8192, output=8192)
    with pytest.raises(CompactionError, match="positive integer"):
        RequestBudget.freeze(provider, "physical-model", 8192, output=0)
