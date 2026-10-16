"""Tests for auto compact (idle TTL) feature."""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.command import CommandContext
from nanobot.config.schema import AgentDefaults
from nanobot.providers.base import LLMResponse
from nanobot.session.microcompaction import MICROCOMPACT_THROUGH, microcompact

# Only the simulated old persisted-summary backend uses a message-count suffix.
_LEGACY_SUFFIX_MESSAGES = 8


def _make_loop(
    tmp_path: Path,
    session_ttl_minutes: int = 15,
) -> AgentLoop:
    """Create a minimal AgentLoop for testing."""
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.estimate_prompt_tokens.return_value = (10_000, "test")
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="ok", tool_calls=[]))
    provider.generation.max_tokens = 4096
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=128_000,
        session_ttl_minutes=session_ttl_minutes,
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    return loop


def _add_turns(session, turns: int, *, prefix: str = "msg") -> None:
    """Append simple user/assistant turns to a session."""
    for i in range(turns):
        session.add_message("user", f"{prefix} user {i}")
        session.add_message("assistant", f"{prefix} assistant {i}")


def _add_tool_turn(session, prefix: str, idx: int) -> None:
    call_id = f"{prefix}_{idx}"
    session.messages.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "exec", "arguments": "{}"},
                }
            ],
            "timestamp": datetime.now().isoformat(),
        }
    )
    session.messages.append(
        {
            "role": "tool",
            "tool_call_id": call_id,
            "name": "exec",
            "content": "ok",
            "timestamp": datetime.now().isoformat(),
        }
    )


def _make_fake_compact(
    loop: AgentLoop,
    *,
    summary: str = "Summary.",
    on_archive=None,
    track_archived: list | None = None,
    track_count: bool = False,
):
    """Seed the retired destructive summary format for legacy replay checks only."""
    from nanobot.session.manager import Session as _Session

    state = {"count": 0}

    async def _fake_compact(key: str, max_suffix: int = _LEGACY_SUFFIX_MESSAGES) -> str:
        state["count"] += 1
        session = loop.sessions.get_or_create(key)

        tail = list(session.messages[session.last_consolidated:])
        if not tail:
            session.updated_at = datetime.now()
            loop.sessions.save(session)
            return ""

        probe = _Session(
            key=session.key,
            messages=tail.copy(),
            created_at=session.created_at,
            updated_at=session.updated_at,
            metadata={},
            last_consolidated=0,
        )
        dropped, already_consolidated = probe.retain_recent_legal_suffix(
            max_suffix,
            extend_to_user=True,
        )
        kept = probe.messages
        archive_msgs = dropped[already_consolidated:]

        if not archive_msgs and not kept:
            session.updated_at = datetime.now()
            loop.sessions.save(session)
            return ""

        last_active = session.updated_at
        s = summary
        if archive_msgs:
            if on_archive:
                result = on_archive(archive_msgs)
                s = result if isinstance(result, str) else summary
            if track_archived is not None:
                track_archived.extend(archive_msgs)

        if s and s != "(nothing)":
            session.metadata["_last_summary"] = {
                "text": s,
                "last_active": last_active.isoformat(),
            }

        session.messages = kept
        session.last_consolidated = 0
        session.updated_at = datetime.now()
        loop.sessions.save(session)
        return s

    # Attach state for count access
    _fake_compact.state = state  # type: ignore[attr-defined]
    return _fake_compact


async def _drain_background_tasks(loop: AgentLoop) -> None:
    tasks = list(loop._background_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)


class TestSessionTTLConfig:
    """Test session TTL configuration."""

    def test_default_ttl_is_zero(self):
        """Default TTL should be 0 (disabled)."""
        defaults = AgentDefaults()
        assert defaults.session_ttl_minutes == 0

    def test_custom_ttl(self):
        """Custom TTL should be stored correctly."""
        defaults = AgentDefaults(session_ttl_minutes=30)
        assert defaults.session_ttl_minutes == 30

    def test_user_friendly_alias_is_supported(self):
        """Config should accept idleCompactAfterMinutes as the preferred JSON key."""
        defaults = AgentDefaults.model_validate({"idleCompactAfterMinutes": 30})
        assert defaults.session_ttl_minutes == 30

    def test_legacy_alias_is_still_supported(self):
        """Config should still accept the old sessionTtlMinutes key for compatibility."""
        defaults = AgentDefaults.model_validate({"sessionTtlMinutes": 30})
        assert defaults.session_ttl_minutes == 30

    def test_serializes_with_user_friendly_alias(self):
        """Config dumps should use idleCompactAfterMinutes for JSON output."""
        defaults = AgentDefaults(session_ttl_minutes=30)
        data = defaults.model_dump(mode="json", by_alias=True)
        assert data["idleCompactAfterMinutes"] == 30
        assert "sessionTtlMinutes" not in data

    def test_session_file_cap_is_internal_constant(self):
        """Session file cap should remain an internal constant, not a config field."""
        from nanobot.session.manager import FILE_MAX_MESSAGES
        assert FILE_MAX_MESSAGES == 2000


class TestAgentLoopTTLParam:
    """Test that AutoCompact receives and stores session_ttl_minutes."""

    def test_loop_stores_ttl(self, tmp_path):
        """AutoCompact should store the TTL value."""
        loop = _make_loop(tmp_path, session_ttl_minutes=25)
        assert loop.auto_compact._ttl == 25

    def test_loop_default_ttl_zero(self, tmp_path):
        """AutoCompact default TTL should be 0 (disabled)."""
        loop = _make_loop(tmp_path, session_ttl_minutes=0)
        assert loop.auto_compact._ttl == 0

    @pytest.mark.asyncio
    async def test_process_message_reads_projected_history(self, tmp_path):
        """_process_message should request the projected replay, not a capped slice.

        Context is governed by the runner's frozen request budget and by
        token-budget checkpoints, so the history call must not cap the replay by
        token budget or message count (see the retired max_messages field).
        """
        loop = _make_loop(tmp_path)
        session = loop.sessions.get_or_create("cli:direct")
        session.get_history = MagicMock(return_value=[])
        loop.context.build_messages = MagicMock(return_value=[])
        loop._run_agent_loop = AsyncMock(return_value=("ok", [], [], "stop", False))
        loop._save_turn = MagicMock()

        msg = InboundMessage(
            channel="cli",
            sender_id="u1",
            chat_id="direct",
            content="hello",
        )
        await loop._process_message(msg)
        session.get_history.assert_called_once()
        kwargs = session.get_history.call_args.kwargs
        assert kwargs["projected"] is True
        assert kwargs["include_timestamps"] is True
        assert kwargs["include_entry_ids"] is True
        assert "max_tokens" not in kwargs
        assert "max_messages" not in kwargs

    @pytest.mark.asyncio
    async def test_session_file_cap_archives_and_trims_old_messages(self, tmp_path):
        loop = _make_loop(tmp_path)
        loop.context.memory.raw_archive = MagicMock()

        for i in range(4):
            msg = InboundMessage(
                channel="cli",
                sender_id="u1",
                chat_id="direct",
                content=f"hello {i}",
            )
            await loop._process_message(msg)

        session = loop.sessions.get_or_create("cli:direct")
        from nanobot.session.manager import FILE_MAX_MESSAGES
        assert len(session.messages) <= FILE_MAX_MESSAGES

    def test_file_cap_does_not_discard_legacy_cursor_without_checkpoint(self, tmp_path):
        from nanobot.session.manager import Session
        archive_fn = MagicMock()
        session = Session(key="cli:direct")
        for i in range(8):
            session.add_message("user", f"u{i}")
        session.last_consolidated = 6

        session.enforce_file_cap(on_archive=archive_fn, limit=4)

        assert len(session.messages) == 8
        archive_fn.assert_not_called()

    def test_file_cap_archives_all_checkpoint_hidden_originals(self, tmp_path):
        from nanobot.session.manager import Session
        archive_fn = MagicMock()
        session = Session(key="cli:direct")
        for i in range(8):
            session.add_message("user", f"u{i}")
        session.last_consolidated = 2
        from tests.session.test_compaction_retention import checkpoint

        checkpoint(session, 4)
        session.enforce_file_cap(on_archive=archive_fn, limit=4)

        assert len(session.messages) <= 4
        archive_fn.assert_called_once()
        archived = archive_fn.call_args.args[0]
        assert [m["content"] for m in archived] == ["u0", "u1", "u2", "u3"]


class TestAutoCompact:
    """Test the _archive method."""

    @pytest.mark.asyncio
    async def test_is_expired_boundary(self, tmp_path):
        """Exactly at TTL boundary should be expired (>= not >)."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        ts = datetime.now() - timedelta(minutes=15)
        assert loop.auto_compact._is_expired(ts) is True
        ts2 = datetime.now() - timedelta(minutes=14, seconds=59)
        assert loop.auto_compact._is_expired(ts2) is False
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_is_expired_string_timestamp(self, tmp_path):
        """_is_expired should parse ISO string timestamps."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        ts = (datetime.now() - timedelta(minutes=20)).isoformat()
        assert loop.auto_compact._is_expired(ts) is True
        assert loop.auto_compact._is_expired(None) is False
        assert loop.auto_compact._is_expired("") is False
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_check_expired_only_archives_expired_sessions(self, tmp_path):
        """With multiple sessions, only the expired one should be archived."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        # Expired session
        s1 = loop.sessions.get_or_create("cli:expired")
        s1.add_message("user", "old")
        s1.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(s1)
        # Active session
        s2 = loop.sessions.get_or_create("cli:active")
        s2.add_message("user", "recent")
        loop.sessions.save(s2)

        loop.auto_compact.check_expired(loop._schedule_background)
        await _drain_background_tasks(loop)

        active_after = loop.sessions.get_or_create("cli:active")
        assert len(active_after.messages) == 1
        assert active_after.messages[0]["content"] == "recent"
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_auto_compact_hides_old_results_without_archiving(self, tmp_path):
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        session.add_message("user", "inspect these results")
        for i in range(12):
            _add_tool_turn(session, "output", i)
            session.messages[-1]["content"] = f"evidence-{i} " * 100
        loop.sessions.save(session)
        originals = [dict(m) for m in session.messages]

        await loop.auto_compact._archive("cli:test")

        projected = microcompact(session.get_history(projected=True, include_entry_ids=True),
                                 session.metadata[MICROCOMPACT_THROUGH])
        results = [m for m in projected if m["role"] == "tool"]
        assert [m["content"] for m in results[:2]] == ["[exec result omitted from context]"] * 2
        assert results[2]["content"] == session.messages[6]["content"]
        assert session.messages == originals
        assert loop.context.memory.read_unprocessed_history(since_cursor=0) == []
        loop.provider.chat_with_retry.assert_not_awaited()
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_auto_compact_preserves_transcript_and_projects_legal_suffix(self, tmp_path):
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        loop.provider.estimate_prompt_tokens.return_value = (0, "heuristic")
        session = loop.sessions.get_or_create("cli:test")
        _add_turns(session, 2, prefix="old")
        session.add_message("user", "record this")
        for i in range(12):
            _add_tool_turn(session, "recent", i)
            session.messages[-1]["content"] = "tool evidence " * 100
        session.add_message("assistant", "done")
        loop.sessions.save(session)

        await loop.auto_compact._archive("cli:test")

        session_after = loop.sessions.get_or_create("cli:test")
        assert len(session_after.messages) > _LEGACY_SUFFIX_MESSAGES
        assert session_after.messages[0]["content"] == "old user 0"
        assert session_after.messages[-1]["content"] == "done"
        projected = microcompact(
            session_after.get_history(projected=True, include_entry_ids=True),
            session_after.metadata[MICROCOMPACT_THROUGH],
        )
        assert "compaction_checkpoints" not in session_after.metadata
        assert len(projected) == len(session_after.messages)
        tool_results = {
            m.get("tool_call_id")
            for m in projected
            if m.get("role") == "tool"
        }
        assert all(
            tc["id"] in tool_results
            for m in projected
            for tc in (m.get("tool_calls") or [])
        )
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_auto_compact_persists_boundary_not_summary(self, tmp_path):
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        _add_turns(session, 6, prefix="hello")
        loop.sessions.save(session)

        await loop.auto_compact._archive("cli:test")

        boundary = session.messages[-1]["_entry_id"]
        loop.sessions.invalidate(session.key)
        session_after = loop.sessions.get_or_create(session.key)
        assert session_after.metadata[MICROCOMPACT_THROUGH] == boundary
        assert "_last_summary" not in session_after.metadata
        assert loop.auto_compact._summaries == {}
        assert len(session_after.messages) == 12
        loop.provider.chat_with_retry.assert_not_awaited()
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_auto_compact_empty_session(self, tmp_path):
        """_archive on empty session should not store a summary."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)

        await loop.auto_compact._archive("cli:test")

        session_after = loop.sessions.get_or_create("cli:test")
        assert len(session_after.messages) == 0
        assert "cli:test" not in loop.auto_compact._summaries
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_auto_compact_respects_last_consolidated(self, tmp_path):
        """Idle projection never advances the legacy cursor or deletes originals."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        _add_turns(session, 14)
        session.last_consolidated = 18
        loop.sessions.save(session)

        await loop.auto_compact._archive("cli:test")

        assert loop.context.memory.read_unprocessed_history(since_cursor=0) == []
        assert session.last_consolidated == 18
        assert len(session.messages) == 28
        loop.provider.chat_with_retry.assert_not_awaited()
        await loop.close_mcp()


class TestAutoCompactIdleDetection:
    """Test idle detection triggers auto-new in _process_message."""

    @pytest.mark.asyncio
    async def test_no_auto_compact_when_ttl_disabled(self, tmp_path):
        """No auto-new should happen when TTL is 0 (disabled)."""
        loop = _make_loop(tmp_path, session_ttl_minutes=0)
        session = loop.sessions.get_or_create("cli:test")
        session.add_message("user", "old message")
        session.updated_at = datetime.now() - timedelta(minutes=30)
        loop.sessions.save(session)

        msg = InboundMessage(channel="cli", sender_id="user", chat_id="test", content="new msg")
        await loop._process_message(msg)

        session_after = loop.sessions.get_or_create("cli:test")
        assert any(m["content"] == "old message" for m in session_after.messages)
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_auto_compact_triggers_on_idle(self, tmp_path):
        """Proactive auto-new archives expired session; _process_message reloads it."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        _add_turns(session, 6, prefix="old")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)

        archived_messages = []
        loop.consolidator.compact_idle_session = _make_fake_compact(
            loop, track_archived=archived_messages,
        )

        # Simulate proactive archive completing before message arrives
        await loop.auto_compact._archive("cli:test")

        msg = InboundMessage(channel="cli", sender_id="user", chat_id="test", content="new msg")
        await loop._process_message(msg)

        session_after = loop.sessions.get_or_create("cli:test")
        assert archived_messages == []
        assert any(m["content"] == "old user 0" for m in session_after.messages)
        assert any(m["content"] == "new msg" for m in session_after.messages)
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_no_auto_compact_when_active(self, tmp_path):
        """No auto-new should happen when session is recently active."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        session.add_message("user", "recent message")
        loop.sessions.save(session)

        msg = InboundMessage(channel="cli", sender_id="user", chat_id="test", content="new msg")
        await loop._process_message(msg)

        session_after = loop.sessions.get_or_create("cli:test")
        assert any(m["content"] == "recent message" for m in session_after.messages)
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_auto_compact_does_not_affect_priority_commands(self, tmp_path):
        """Priority commands (/stop, /restart) bypass _process_message entirely via run()."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        session.add_message("user", "old message")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)

        # Priority commands are dispatched in run() before _process_message is called.
        # Simulate that path directly via dispatch_priority.
        raw = "/stop"
        msg = InboundMessage(channel="cli", sender_id="user", chat_id="test", content=raw)
        ctx = CommandContext(msg=msg, session=session, key="cli:test", raw=raw, loop=loop)
        result = await loop.commands.dispatch_priority(ctx)
        assert result is not None
        assert "stopped" in result.content.lower() or "no active task" in result.content.lower()

        # Session should be untouched since priority commands skip _process_message
        session_after = loop.sessions.get_or_create("cli:test")
        assert any(m["content"] == "old message" for m in session_after.messages)
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_auto_compact_with_slash_new(self, tmp_path):
        """Auto-new fires before /new dispatches; session is cleared twice but idempotent."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        for i in range(4):
            session.add_message("user", f"msg{i}")
            session.add_message("assistant", f"resp{i}")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)

        loop.consolidator.compact_idle_session = _make_fake_compact(loop)

        msg = InboundMessage(channel="cli", sender_id="user", chat_id="test", content="/new")
        response = await loop._process_message(msg)

        assert response is not None
        assert "new session started" in response.content.lower()

        session_after = loop.sessions.get_or_create("cli:test")
        # Session is empty (auto-new archived and cleared, /new cleared again)
        assert len(session_after.messages) == 0
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_shortcut_command_persisted_with_command_flag(self, tmp_path):
        """Shortcut commands (e.g. /help) are persisted so WebUI can show them,
        but tagged with _command so they don't leak into LLM context."""
        loop = _make_loop(tmp_path)
        msg = InboundMessage(channel="cli", sender_id="user", chat_id="test", content="/help")
        response = await loop._process_message(msg)

        assert response is not None
        session_after = loop.sessions.get_or_create("cli:test")
        assert len(session_after.messages) == 2
        assert session_after.messages[0]["role"] == "user"
        assert session_after.messages[0]["content"] == "/help"
        assert session_after.messages[0].get("_command") is True
        assert session_after.messages[1]["role"] == "assistant"
        assert session_after.messages[1].get("_command") is True
        assert AgentLoop._PENDING_USER_TURN_KEY not in session_after.metadata
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_shortcut_command_excluded_from_get_history(self, tmp_path):
        """Messages marked _command are invisible to get_history (LLM context)."""
        loop = _make_loop(tmp_path)
        session = loop.sessions.get_or_create("cli:test")
        session.add_message("user", "real question")
        session.add_message("assistant", "real answer")
        session.add_message("user", "/help", _command=True)
        session.add_message("assistant", "help text", _command=True)

        history = session.get_history()
        assert len(history) == 2
        assert all(m["content"] != "/help" for m in history)
        assert all(m["content"] != "help text" for m in history)
        await loop.close_mcp()


class TestAutoCompactSystemMessages:
    """Test that auto-new also works for system messages."""

    @pytest.mark.asyncio
    async def test_auto_compact_triggers_for_system_messages(self, tmp_path):
        """Proactive auto-new archives expired session; system messages reload it."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        _add_turns(session, 6, prefix="old")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)

        loop.consolidator.compact_idle_session = _make_fake_compact(loop)

        # Simulate proactive archive completing before system message arrives
        await loop.auto_compact._archive("cli:test")

        msg = InboundMessage(
            channel="system", sender_id="subagent", chat_id="cli:test",
            content="subagent result",
        )
        await loop._process_message(msg)

        session_after = loop.sessions.get_or_create("cli:test")
        assert any(
            m["content"] == "old user 0"
            for m in session_after.messages
        )
        await loop.close_mcp()


class TestAutoCompactEdgeCases:
    """Edge cases for auto session new."""

    @pytest.mark.asyncio
    async def test_auto_compact_with_nothing_summary(self, tmp_path):
        """An empty semantic summary must not advance continuity."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        _add_turns(session, 6, prefix="thanks")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)

        loop.provider.chat_with_retry = AsyncMock(
            return_value=LLMResponse(content="(nothing)", tool_calls=[])
        )

        await loop.auto_compact._archive("cli:test")

        session_after = loop.sessions.get_or_create("cli:test")
        assert len(session_after.messages) == 12
        assert "compaction_checkpoints" not in session_after.metadata
        # "(nothing)" summary should not be stored
        assert "cli:test" not in loop.auto_compact._summaries

        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_auto_compact_failure_preserves_all_original_messages(self, tmp_path):
        """Failed generation must not discard unsummarized task context."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        _add_turns(session, 6, prefix="important")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)

        loop.provider.chat_with_retry = AsyncMock(side_effect=Exception("API down"))

        # Should not raise
        await loop.auto_compact._archive("cli:test")

        session_after = loop.sessions.get_or_create("cli:test")
        assert len(session_after.messages) == 12
        assert "compaction_checkpoints" not in session_after.metadata
        assert session_after.last_consolidated == 0

        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_auto_compact_preserves_runtime_checkpoint_before_check(self, tmp_path):
        """Short expired sessions keep recent messages; checkpoint restore still works on resume."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        session.metadata[AgentLoop._RUNTIME_CHECKPOINT_KEY] = {
            "assistant_message": {"role": "assistant", "content": "interrupted response"},
            "completed_tool_results": [],
            "pending_tool_calls": [],
        }
        session.add_message("user", "previous message")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)

        archived_messages = []
        loop.consolidator.compact_idle_session = _make_fake_compact(
            loop, track_archived=archived_messages,
        )

        # Simulate proactive archive completing before message arrives
        await loop.auto_compact._archive("cli:test")

        msg = InboundMessage(channel="cli", sender_id="user", chat_id="test", content="continue")
        await loop._process_message(msg)

        session_after = loop.sessions.get_or_create("cli:test")
        assert archived_messages == []
        assert any(m["content"] == "previous message" for m in session_after.messages)
        assert any(m["content"] == "interrupted response" for m in session_after.messages)

        await loop.close_mcp()


class TestAutoCompactIntegration:
    """End-to-end test of auto session new feature."""

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, tmp_path):
        """
        Full lifecycle: messages -> idle -> auto-new -> archive -> clear -> summary injected as runtime context.
        """
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")

        # Phase 1: User has a conversation longer than the retained recent suffix
        session.add_message("user", "I'm learning English, teach me past tense")
        session.add_message("assistant", "Past tense is used for actions completed in the past...")
        session.add_message("user", "Give me an example")
        session.add_message("assistant", '"I walked to the store yesterday."')
        session.add_message("user", "Give me another example")
        session.add_message("assistant", '"She visited Paris last year."')
        session.add_message("user", "Quiz me")
        session.add_message("assistant", "What is the past tense of go?")
        session.add_message("user", "I think it is went")
        session.add_message("assistant", "Correct.")
        loop.sessions.save(session)

        # Phase 2: Time passes (simulate idle)
        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)

        # Phase 3: User returns with a new message
        loop.provider.chat_with_retry = AsyncMock(
            return_value=LLMResponse(
                content="User is learning English past tense. Example: 'I walked to the store yesterday.'",
                tool_calls=[],
            )
        )

        msg = InboundMessage(
            channel="cli", sender_id="user", chat_id="test",
            content="Let's continue, teach me present perfect",
        )
        response = await loop._process_message(msg)

        # Phase 4: Verify
        session_after = loop.sessions.get_or_create("cli:test")

        # The oldest messages should be trimmed from live session history
        assert not any(
            "past tense is used" in str(m.get("content", "")) for m in session_after.messages
        )

        # Summary should NOT be persisted in session (ephemeral, one-shot)
        assert not any(
            "[Resumed Session]" in str(m.get("content", "")) for m in session_after.messages
        )
        # Runtime context end marker should NOT be persisted
        assert not any(
            "[/Runtime Context]" in str(m.get("content", "")) for m in session_after.messages
        )

        # Pending summary should be consumed (one-shot)
        assert "cli:test" not in loop.auto_compact._summaries

        # The new message should be processed (response exists)
        assert response is not None

        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_runtime_context_markers_not_persisted_for_multi_paragraph_turn(self, tmp_path):
        """Auto-compact resume context must not leak runtime markers into persisted session history."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        session.add_message("user", "old message")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)

        loop.consolidator.compact_idle_session = _make_fake_compact(loop)

        # Simulate proactive archive completing before message arrives
        await loop.auto_compact._archive("cli:test")

        msg = InboundMessage(
            channel="cli", sender_id="user", chat_id="test",
            content="Paragraph one\n\nParagraph two\n\nParagraph three",
        )
        await loop._process_message(msg)

        session_after = loop.sessions.get_or_create("cli:test")
        assert any(m.get("content") == "old message" for m in session_after.messages)
        for persisted in session_after.messages:
            content = str(persisted.get("content", ""))
            assert "[Runtime Context" not in content
            assert "[/Runtime Context]" not in content
        await loop.close_mcp()


class TestProactiveAutoCompact:
    """Test proactive auto-new on idle ticks (TimeoutError path in run loop)."""

    @staticmethod
    async def _run_check_expired(loop, active_session_keys=()):
        """Helper: run check_expired via callback and wait for background tasks."""
        loop.auto_compact.check_expired(
            loop._schedule_background,
            active_session_keys=active_session_keys,
        )
        await _drain_background_tasks(loop)

    @pytest.mark.asyncio
    async def test_no_check_when_ttl_disabled(self, tmp_path):
        """check_expired should be a no-op when TTL is 0."""
        loop = _make_loop(tmp_path, session_ttl_minutes=0)
        session = loop.sessions.get_or_create("cli:test")
        session.add_message("user", "old message")
        session.updated_at = datetime.now() - timedelta(minutes=30)
        loop.sessions.save(session)

        await self._run_check_expired(loop)

        session_after = loop.sessions.get_or_create("cli:test")
        assert len(session_after.messages) == 1
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_proactive_microcompact_on_idle_tick(self, tmp_path):
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        _add_turns(session, 5, prefix="old")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        old_time = session.updated_at
        loop.sessions.save(session)

        await self._run_check_expired(loop)

        assert len(session.messages) == 10
        assert session.updated_at == old_time
        assert session.metadata[MICROCOMPACT_THROUGH] == session.messages[-1]["_entry_id"]
        assert loop.auto_compact._summaries == {}
        loop.provider.chat_with_retry.assert_not_awaited()
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_proactive_archive_skips_dream_sessions(self, tmp_path):
        """Internal Dream sessions should be left to Dream retention, not idle compact."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("dream:20260602-155256")
        _add_turns(session, 6, prefix="dream")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)

        _fake_compact = _make_fake_compact(loop)
        loop.consolidator.compact_idle_session = _fake_compact

        await self._run_check_expired(loop)

        session_after = loop.sessions.get_or_create("dream:20260602-155256")
        assert len(session_after.messages) == 12
        assert _fake_compact.state["count"] == 0
        assert "dream:20260602-155256" not in loop.auto_compact._archiving
        assert "dream:20260602-155256" not in loop.auto_compact._summaries
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_no_proactive_archive_when_active(self, tmp_path):
        """Recently active session should NOT be archived on idle tick."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        session.add_message("user", "recent message")
        loop.sessions.save(session)

        await self._run_check_expired(loop)

        session_after = loop.sessions.get_or_create("cli:test")
        assert len(session_after.messages) == 1
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_no_duplicate_archive(self, tmp_path):
        """Should not archive the same session twice if already in progress."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        _add_turns(session, 6, prefix="old")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)

        archive_count = 0
        started = asyncio.Event()
        block_forever = asyncio.Event()

        async def _slow_compact(key):
            nonlocal archive_count
            archive_count += 1
            started.set()
            await block_forever.wait()
            return "Summary."

        loop.consolidator.microcompact_idle_session = _slow_compact

        # First call starts archiving via callback
        loop.auto_compact.check_expired(loop._schedule_background)
        await started.wait()
        assert archive_count == 1

        # Second call should skip (key is in _archiving)
        loop.auto_compact.check_expired(loop._schedule_background)
        assert archive_count == 1

        # Clean up
        block_forever.set()
        await _drain_background_tasks(loop)
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_proactive_archive_error_does_not_block(self, tmp_path):
        """Proactive archive failure should be caught and not block future ticks."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        _add_turns(session, 6, prefix="old")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)

        async def _failing_compact(key):
            raise RuntimeError("LLM down")

        loop.consolidator.microcompact_idle_session = _failing_compact

        # Should not raise
        await self._run_check_expired(loop)

        # Key should be removed from _archiving (finally block)
        assert "cli:test" not in loop.auto_compact._archiving
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_proactive_archive_skips_empty_sessions(self, tmp_path):
        """Proactive archive should not produce a summary for sessions with no messages."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)

        loop.consolidator.compact_idle_session = _make_fake_compact(loop)

        await self._run_check_expired(loop)

        # Empty session should not produce a summary
        assert "cli:test" not in loop.auto_compact._summaries
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_skip_expired_session_with_active_agent_task(self, tmp_path):
        """Expired session with an active agent task should NOT be archived."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        _add_turns(session, 6, prefix="old")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)

        _fake_compact = _make_fake_compact(loop)
        loop.consolidator.compact_idle_session = _fake_compact

        # Simulate an active agent task for this session
        await self._run_check_expired(loop, active_session_keys={"cli:test"})
        assert _fake_compact.state["count"] == 0

        session_after = loop.sessions.get_or_create("cli:test")
        assert len(session_after.messages) == 12  # All messages preserved

        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_microcompact_after_active_task_completes(self, tmp_path):
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        _add_turns(session, 6, prefix="old")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)
        operation = AsyncMock(wraps=loop.consolidator.microcompact_idle_session)
        loop.consolidator.microcompact_idle_session = operation

        await self._run_check_expired(loop, active_session_keys={"cli:test"})
        operation.assert_not_awaited()

        await self._run_check_expired(loop)
        operation.assert_awaited_once_with("cli:test")
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_partial_active_set_only_microcompacts_inactive_expired(self, tmp_path):
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        s1 = loop.sessions.get_or_create("cli:expired_idle")
        _add_turns(s1, 6, prefix="old_a")
        s1.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(s1)
        s2 = loop.sessions.get_or_create("cli:expired_active")
        _add_turns(s2, 6, prefix="old_b")
        s2.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(s2)
        s3 = loop.sessions.get_or_create("cli:recent")
        s3.add_message("user", "recent")
        loop.sessions.save(s3)
        operation = AsyncMock(wraps=loop.consolidator.microcompact_idle_session)
        loop.consolidator.microcompact_idle_session = operation

        await self._run_check_expired(loop, active_session_keys={"cli:expired_active"})

        operation.assert_awaited_once_with(s1.key)
        assert MICROCOMPACT_THROUGH in s1.metadata
        assert MICROCOMPACT_THROUGH not in s2.metadata
        assert MICROCOMPACT_THROUGH not in s3.metadata
        assert [len(s.messages) for s in (s1, s2, s3)] == [12, 12, 1]
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_no_reschedule_after_successful_microcompact(self, tmp_path):
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        _add_turns(session, 5, prefix="old")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        old_time = session.updated_at
        loop.sessions.save(session)
        operation = AsyncMock(wraps=loop.consolidator.microcompact_idle_session)
        loop.consolidator.microcompact_idle_session = operation

        await self._run_check_expired(loop)
        # Pass the global start interval too: suppression must not depend on it.
        loop.auto_compact._next_start = 0
        await self._run_check_expired(loop)

        operation.assert_awaited_once_with("cli:test")
        assert session.updated_at == old_time
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_empty_skip_refreshes_updated_at_prevents_reschedule(self, tmp_path):
        """Empty session skip refreshes updated_at, preventing immediate re-scheduling."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)

        loop.consolidator.compact_idle_session = _make_fake_compact(loop)

        # First tick: skips (no messages), refreshes updated_at
        await self._run_check_expired(loop)
        assert "cli:test" not in loop.auto_compact._summaries

        # Second tick: should NOT re-schedule because updated_at is fresh
        await self._run_check_expired(loop)
        assert "cli:test" not in loop.auto_compact._summaries
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_session_can_be_compacted_again_after_new_messages(self, tmp_path):
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        _add_turns(session, 5, prefix="first")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)
        await loop.auto_compact._archive("cli:test")
        first_boundary = session.metadata[MICROCOMPACT_THROUGH]

        msg = InboundMessage(channel="cli", sender_id="user", chat_id="test", content="second topic")
        await loop._process_message(msg)
        assert session.metadata[MICROCOMPACT_THROUGH] == first_boundary

        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)
        await loop.auto_compact._archive("cli:test")
        assert session.metadata[MICROCOMPACT_THROUGH] == session.messages[-1]["_entry_id"]
        assert session.metadata[MICROCOMPACT_THROUGH] != first_boundary
        await loop.close_mcp()


class TestSummaryPersistence:
    """Test that summary survives restart via session metadata."""

    @pytest.mark.asyncio
    async def test_summary_persisted_in_session_metadata(self, tmp_path):
        """After archive, _last_summary should be in session metadata."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        _add_turns(session, 6, prefix="hello")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)

        await _make_fake_compact(loop, summary="User said hello.")("cli:test")

        # A legacy summary is still readable from session metadata
        session_after = loop.sessions.get_or_create("cli:test")
        meta = session_after.metadata.get("_last_summary")
        assert meta is not None
        assert meta["text"] == "User said hello."
        assert "last_active" in meta
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_summary_recovered_after_restart(self, tmp_path):
        """Summary should be recovered from metadata when _summaries is empty (simulates restart)."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        _add_turns(session, 6, prefix="hello")
        last_active = datetime.now() - timedelta(minutes=20)
        session.updated_at = last_active
        loop.sessions.save(session)

        # Seed a session written by the retired compactor.
        await _make_fake_compact(loop, summary="User said hello.")("cli:test")

        # Simulate restart: clear in-memory state
        loop.auto_compact._summaries.clear()
        loop.sessions.invalidate("cli:test")

        # prepare_session should recover summary from metadata
        reloaded = loop.sessions.get_or_create("cli:test")
        assert len(reloaded.messages) == _LEGACY_SUFFIX_MESSAGES
        _, summary = loop.auto_compact.prepare_session(reloaded, "cli:test")

        assert summary is not None
        assert "User said hello." in summary
        assert "Previous conversation summary" in summary
        # _last_summary persists in metadata for restart survival.
        assert "_last_summary" in reloaded.metadata
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_metadata_persists_for_restart(self, tmp_path):
        """_last_summary stays in metadata so it survives process restarts."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        _add_turns(session, 6, prefix="hello")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)

        await _make_fake_compact(loop)("cli:test")

        # Clear in-memory to force metadata path
        loop.auto_compact._summaries.clear()
        loop.sessions.invalidate("cli:test")
        reloaded = loop.sessions.get_or_create("cli:test")

        # Every call returns the summary from metadata (no _consumed_keys gate)
        _, summary = loop.auto_compact.prepare_session(reloaded, "cli:test")
        assert summary is not None
        _, summary2 = loop.auto_compact.prepare_session(reloaded, "cli:test")
        assert summary2 is not None
        assert "Summary." in summary2
        # _last_summary persists in metadata for restart survival.
        assert "_last_summary" in reloaded.metadata
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_metadata_cleanup_on_inmemory_path(self, tmp_path):
        """In-memory _summaries path should also clean up _last_summary from metadata."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        _add_turns(session, 6, prefix="hello")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)

        await _make_fake_compact(loop)("cli:test")
        meta = session.metadata["_last_summary"]
        loop.auto_compact._summaries[session.key] = (
            meta["text"], datetime.fromisoformat(meta["last_active"]),
        )

        # Legacy in-memory and persisted summaries are both readable
        assert "cli:test" in loop.auto_compact._summaries
        loop.sessions.invalidate("cli:test")
        reloaded = loop.sessions.get_or_create("cli:test")
        assert "_last_summary" in reloaded.metadata

        # In-memory path is taken (no restart)
        _, summary = loop.auto_compact.prepare_session(reloaded, "cli:test")
        assert summary is not None
        # _last_summary persists in metadata for restart survival.
        assert "_last_summary" in reloaded.metadata
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_new_summary_overrides_old(self, tmp_path):
        """A fresh archive writes a new summary that replaces the old one."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        _add_turns(session, 6, prefix="hello")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)

        await _make_fake_compact(loop, summary="First summary.")("cli:test")

        # Consume the first summary via hot path
        _, summary1 = loop.auto_compact.prepare_session(
            loop.sessions.get_or_create("cli:test"), "cli:test"
        )
        assert summary1 is not None
        assert "First summary." in summary1
        assert "cli:test" not in loop.auto_compact._summaries  # popped by hot path

        # Add new messages and archive again (simulating a later turn)
        _add_turns(session, 4, prefix="world")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)

        await _make_fake_compact(loop, summary="Second summary.")("cli:test")

        # The second legacy archive replaces the persisted summary.
        assert session.metadata["_last_summary"]["text"] == "Second summary."

        # prepare_session must return the new summary
        reloaded = loop.sessions.get_or_create("cli:test")
        _, summary2 = loop.auto_compact.prepare_session(reloaded, "cli:test")
        assert summary2 is not None
        assert "Second summary." in summary2
        await loop.close_mcp()

    @pytest.mark.asyncio
    async def test_new_command_clears_last_summary(self, tmp_path):
        """/new should clear _last_summary so the new session starts fresh."""
        loop = _make_loop(tmp_path, session_ttl_minutes=15)
        session = loop.sessions.get_or_create("cli:test")
        _add_turns(session, 6, prefix="hello")
        session.updated_at = datetime.now() - timedelta(minutes=20)
        loop.sessions.save(session)

        await _make_fake_compact(loop, summary="Old summary.")("cli:test")
        await loop.auto_compact._archive("cli:test")
        assert MICROCOMPACT_THROUGH in session.metadata

        # Verify summary exists before /new
        reloaded = loop.sessions.get_or_create("cli:test")
        assert "_last_summary" in reloaded.metadata

        # Simulate /new command
        session.clear()
        loop.sessions.save(session)
        loop.sessions.invalidate(session.key)

        # After /new, metadata should no longer contain _last_summary
        fresh = loop.sessions.get_or_create("cli:test")
        assert "_last_summary" not in fresh.metadata
        assert MICROCOMPACT_THROUGH not in fresh.metadata
        await loop.close_mcp()
