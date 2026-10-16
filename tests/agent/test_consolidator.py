"""Session checkpoints are non-destructive: they never trim, and the range they hide stays in searchable history."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.memory import _ARCHIVE_SUMMARY_MAX_CHARS, Consolidator, MemoryStore
from nanobot.providers.base import GenerationSettings, LLMResponse
from nanobot.session.compaction import CHECKPOINTS, latest_checkpoint
from nanobot.session.manager import SessionManager
from nanobot.utils.prompt_templates import render_template
from tests.session.test_compaction import SummaryProvider, transcript


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path)


@pytest.fixture
def mock_provider():
    provider = MagicMock()
    provider.generation = GenerationSettings(max_tokens=100)
    provider.chat_with_retry = AsyncMock()
    return provider


@pytest.fixture
def consolidator(store, mock_provider):
    return Consolidator(
        store=store, provider=mock_provider, model="test-model", sessions=SessionManager(store.workspace),
        context_window_tokens=1000, build_messages=MagicMock(return_value=[]),
        get_tool_definitions=MagicMock(return_value=[]), max_completion_tokens=100,
    )


@pytest.fixture
def checkpoint_consolidator(store):
    provider = SummaryProvider()
    result = Consolidator(
        store=store, provider=provider, model="physical-model", sessions=SessionManager(store.workspace),
        context_window_tokens=8192, build_messages=lambda **kw: kw["history"],
        get_tool_definitions=lambda: [], max_completion_tokens=256,
    )
    result.flush_before_compaction = AsyncMock()
    session = result.sessions.get_or_create("test:checkpoint")
    session.messages = transcript()
    result.sessions.save(session)
    return result


class TestConsolidatorSummarize:
    async def test_summarize_appends_to_history(self, consolidator, mock_provider, store):
        mock_provider.chat_with_retry.return_value = LLMResponse(content="User fixed a bug in the auth module.")
        result = await consolidator.archive([
            {"role": "user", "content": "fix the auth bug"},
            {"role": "assistant", "content": "Done, fixed the race condition."},
        ])
        assert result == "User fixed a bug in the auth module."
        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 1
        assert entries[0]["kind"] == "conversation"
        assert entries[0]["summary"] == result

    async def test_summarize_appends_session_key_to_history(self, consolidator, mock_provider, store):
        mock_provider.chat_with_retry.return_value = LLMResponse(content="Summary")
        await consolidator.archive([{"role": "user", "content": "fix auth"}], session_key="telegram:chat-1")
        assert store.read_unprocessed_history(since_cursor=0)[0]["session_key"] == "telegram:chat-1"

    async def test_summarize_archives_without_summary_on_llm_failure(self, consolidator, mock_provider, store):
        mock_provider.chat_with_retry.side_effect = Exception("API error")
        result = await consolidator.archive([{"role": "user", "content": "hello"}])
        assert result is None
        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 1
        assert entries[0]["kind"] == "conversation"
        assert entries[0]["messages"][0]["content"] == "hello"
        assert "summary" not in entries[0]
        assert "content" not in entries[0]

    async def test_raw_dump_fallback_appends_session_key(self, consolidator, mock_provider, store):
        mock_provider.chat_with_retry.side_effect = Exception("API error")
        await consolidator.archive([{"role": "user", "content": "hello"}], session_key="slack:chat-2")
        assert store.read_unprocessed_history(since_cursor=0)[0]["session_key"] == "slack:chat-2"

    async def test_summarize_skips_empty_messages(self, consolidator):
        assert await consolidator.archive([]) is None


class TestConsolidatorPromptContract:
    def test_archive_prompt_outputs_attribute_tags_without_missing_context_claims(self):
        prompt = render_template("agent/consolidator_archive.md", strip=True)
        assert "SNIP" in prompt
        for mark in ("[permanent]", "[durable]", "[ephemeral]", "[correction]", "[skip]"):
            assert mark in prompt
        assert "check context below" not in prompt.lower()
        assert "Do not mark something [skip] merely because it might already exist" in prompt


class TestConsolidatorArchiveErrorHandling:
    async def test_archive_omits_summary_on_error_finish_reason(self, consolidator, mock_provider, store):
        mock_provider.chat_with_retry.return_value = LLMResponse(content="Error: overloaded (529)", finish_reason="error")
        result = await consolidator.archive([{"role": "user", "content": "fix auth"}])
        assert result is None
        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 1
        assert entries[0]["kind"] == "conversation"
        assert "summary" not in entries[0]
        assert "content" not in entries[0]
        assert "Error:" not in store.history_entry_text(entries[0])

    async def test_archive_preserves_summary_on_success(self, consolidator, mock_provider, store):
        mock_provider.chat_with_retry.return_value = LLMResponse(content="User fixed a bug in the auth module.")
        result = await consolidator.archive([{"role": "user", "content": "fix auth"}])
        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 1
        assert "content" not in entries[0]
        assert entries[0]["summary"] == result


class TestContinuityCheckpoints:
    @pytest.mark.parametrize("replay_limit", [2, 4, 120])
    async def test_replay_window_never_archives_or_advances_cursor(self, checkpoint_consolidator, replay_limit):
        consolidator = checkpoint_consolidator
        session = consolidator.sessions.get_or_create("test:checkpoint")
        originals = deepcopy(session.messages)
        consolidator.archive = AsyncMock()
        projected = session.get_history(projected=True, max_messages=replay_limit)
        assert len(projected) == len(session.messages)
        await consolidator.maybe_consolidate_by_tokens(session)
        assert session.messages == originals
        assert session.last_consolidated == 0
        consolidator.archive.assert_not_awaited()

    async def test_idle_retains_originals_and_projects_checkpoint(self, checkpoint_consolidator):
        consolidator = checkpoint_consolidator
        session = consolidator.sessions.get_or_create("test:checkpoint")
        originals = deepcopy(session.messages)
        summary = await consolidator.compact_idle_session(session.key)
        assert summary
        assert session.messages == originals
        assert session.last_consolidated == 0
        assert session.metadata[CHECKPOINTS]
        assert "_last_summary" not in session.metadata
        assert session.get_history(projected=True, include_entry_ids=True)[0]["_compaction_summary"]
        consolidator.flush_before_compaction.assert_awaited_once_with(session)
        archived = consolidator.store.read_unprocessed_history(since_cursor=0)
        assert len(archived) == 1
        assert archived[0]["kind"] == "conversation"
        assert archived[0]["session_key"] == session.key
        assert "Original goal" in consolidator.store.history_entry_text(archived[0])

    async def test_archived_range_is_exactly_the_hidden_prefix(self, checkpoint_consolidator):
        consolidator = checkpoint_consolidator
        session = consolidator.sessions.get_or_create("test:checkpoint")
        assert await consolidator.compact_idle_session(session.key)
        checkpoint = latest_checkpoint(session.metadata)
        ids = [m.get("_entry_id") for m in session.messages]
        hidden = ids.index(checkpoint["first_kept_entry_id"])
        assert 0 < hidden < len(ids)

        archived = consolidator.store.read_unprocessed_history(since_cursor=0)
        assert len(archived) == 1
        assert archived[0]["message_count"] == hidden

    async def test_successive_checkpoints_archive_disjoint_ranges(self, checkpoint_consolidator):
        consolidator = checkpoint_consolidator
        session = consolidator.sessions.get_or_create("test:checkpoint")
        assert await consolidator.compact_idle_session(session.key)
        first_end = [m.get("_entry_id") for m in session.messages].index(
            latest_checkpoint(session.metadata)["first_kept_entry_id"]
        )

        for index in range(8):
            session.add_message("user", f"FOLLOW-UP-{index} " + "more context " * 200)
            session.add_message("assistant", "acknowledged " * 200)
        consolidator.sessions.save(session)
        assert await consolidator.compact_idle_session(session.key) is not None

        ids = [m.get("_entry_id") for m in session.messages]
        second_end = ids.index(latest_checkpoint(session.metadata)["first_kept_entry_id"])
        assert second_end > first_end

        archived = consolidator.store.read_unprocessed_history(since_cursor=0)
        assert [entry["message_count"] for entry in archived] == [first_end, second_end - first_end]

    @pytest.mark.parametrize("reason,text", [("error", "failed"), ("length", "partial"),
                                             ("aborted", "partial"), ("stop", "")])
    async def test_failed_summary_never_archives_or_advances(self, checkpoint_consolidator, reason, text):
        consolidator = checkpoint_consolidator
        session = consolidator.sessions.get_or_create("test:checkpoint")
        originals = deepcopy(session.messages)
        consolidator.provider.response = LLMResponse(content=text, finish_reason=reason)
        assert await consolidator.compact_idle_session(session.key) is None
        assert session.messages == originals
        assert session.last_consolidated == 0
        assert not latest_checkpoint(session.metadata)
        assert consolidator.store.read_unprocessed_history(since_cursor=0) == []

    async def test_cancelled_generation_leaves_checkpoint_unchanged(self, checkpoint_consolidator):
        consolidator = checkpoint_consolidator
        session = consolidator.sessions.get_or_create("test:checkpoint")
        original = deepcopy(session.messages)
        entered = asyncio.Event()

        async def block(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        consolidator.provider.chat_with_retry = block
        task = asyncio.create_task(consolidator.compact_idle_session(session.key))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.messages == original
        assert not latest_checkpoint(session.metadata)
        assert not consolidator.get_lock(session.key).locked()

    async def test_acquires_consolidation_lock(self, checkpoint_consolidator):
        consolidator = checkpoint_consolidator
        session = consolidator.sessions.get_or_create("test:checkpoint")
        lock = consolidator.get_lock(session.key)
        async with lock:
            task = asyncio.create_task(consolidator.compact_idle_session(session.key))
            await asyncio.sleep(0)
            assert not task.done()
            assert not consolidator.provider.requests
        assert await task
        assert not lock.locked()

    async def test_stale_session_cannot_overwrite_reloaded_messages(self, checkpoint_consolidator):
        consolidator = checkpoint_consolidator
        stale = consolidator.sessions.get_or_create("test:checkpoint")
        consolidator.sessions.invalidate(stale.key)
        current = consolidator.sessions.get_or_create(stale.key)
        current.add_message("user", "newer input")
        consolidator.sessions.save(current)
        assert await consolidator.compact_session(stale) is None
        assert current.messages[-1]["content"] == "newer input"
        assert not consolidator.provider.requests

    async def test_legacy_cursor_excludes_previously_archived_prefix(self, checkpoint_consolidator):
        consolidator = checkpoint_consolidator
        session = consolidator.sessions.get_or_create("test:checkpoint")
        session.messages = []
        for i in range(30):
            session.add_message("user", f"TURN-{i:02d} " + "context " * 150)
            session.add_message("assistant", "answer " * 150)
        session.last_consolidated = 50
        consolidator.sessions.save(session)
        assert await consolidator.compact_idle_session(session.key)
        sent = str(consolidator.provider.requests)
        assert "TURN-00" not in sent
        assert "TURN-25" in sent
        assert len(session.messages) == 60
        assert session.last_consolidated == 50

    async def test_reload_projection_matches_and_originals_remain(self, checkpoint_consolidator):
        consolidator = checkpoint_consolidator
        session = consolidator.sessions.get_or_create("test:checkpoint")
        assert await consolidator.compact_idle_session(session.key)
        expected = session.get_history(projected=True)
        originals = deepcopy(session.messages)
        consolidator.sessions.invalidate(session.key)
        loaded = consolidator.sessions.get_or_create(session.key)
        assert loaded.get_history(projected=True) == expected
        assert loaded.messages == originals

    async def test_empty_session_refreshes_timestamp(self, checkpoint_consolidator):
        consolidator = checkpoint_consolidator
        session = consolidator.sessions.get_or_create("test:empty")
        session.updated_at = datetime.now() - timedelta(hours=2)
        old = session.updated_at
        assert await consolidator.compact_idle_session(session.key) == ""
        assert session.updated_at > old


class TestRawArchiveTruncation:
    def test_raw_archive_truncates_large_content(self, store):
        store.raw_archive([{"role": "user", "content": "x" * 50_000}])
        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 1
        text = store.history_entry_text(entries[0])
        assert len(text) < 50_000
        assert "[RAW]" in text

    def test_raw_archive_preserves_small_content(self, store):
        store.raw_archive([{"role": "user", "content": "hello"}])
        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 1
        assert "hello" in store.history_entry_text(entries[0])

    def test_raw_archive_preserves_session_key(self, store):
        store.raw_archive([{"role": "user", "content": "hello"}], session_key="websocket:chat-1")
        assert store.read_unprocessed_history(since_cursor=0)[0]["session_key"] == "websocket:chat-1"

    def test_raw_archive_custom_max_chars(self, store):
        store.raw_archive([{"role": "user", "content": "a" * 200}], max_chars=100)
        assert len(store.history_entry_text(store.read_unprocessed_history(since_cursor=0)[0])) < 200


class TestArchiveTruncation:
    async def test_archive_truncates_large_formatted_text(self, consolidator, mock_provider, store):
        mock_provider.chat_with_retry.return_value = LLMResponse(content="Summary of large input.")
        await consolidator.archive([{"role": "user", "content": "x" * 100_000}])
        assert len(mock_provider.chat_with_retry.call_args.kwargs["messages"][1]["content"]) < 50_000

    async def test_archive_truncates_with_small_token_budget(self, consolidator, mock_provider, store):
        consolidator.context_window_tokens = 500
        mock_provider.chat_with_retry.return_value = LLMResponse(content="Summary.")
        await consolidator.archive([{"role": "user", "content": "word " * 50_000}])
        assert len(mock_provider.chat_with_retry.call_args.kwargs["messages"][1]["content"]) < 250_000

    async def test_oversized_summary_is_capped_before_append(self, consolidator, mock_provider, store):
        mock_provider.chat_with_retry.return_value = LLMResponse(content="S" * (_ARCHIVE_SUMMARY_MAX_CHARS * 10))
        await consolidator.archive([{"role": "user", "content": "hi"}])
        entry = store.read_unprocessed_history(since_cursor=0)[0]
        assert len(entry["summary"]) <= _ARCHIVE_SUMMARY_MAX_CHARS + 50

    async def test_archive_truncates_via_tiktoken_with_positive_budget(self, consolidator, mock_provider, store):
        import tiktoken

        consolidator.context_window_tokens = 10_000
        consolidator._SAFETY_BUFFER = 0
        mock_provider.chat_with_retry.return_value = LLMResponse(content="Summary.")
        await consolidator.archive([{"role": "user", "content": "word " * 50_000}])
        sent = mock_provider.chat_with_retry.call_args.kwargs["messages"][1]["content"]
        assert len(tiktoken.get_encoding("cl100k_base").encode(sent)) <= 9910
