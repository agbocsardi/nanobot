from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# Import AgentLoop first to avoid command package initialization cycle.
from nanobot.agent.loop import AgentLoop  # noqa: F401
from nanobot.command.builtin import cmd_compact
from nanobot.command.router import CommandContext


@pytest.mark.asyncio
async def test_compact_archives_old_history_and_reports_counts():
    old = SimpleNamespace(messages=[{}, {}, {}, {}])
    new = SimpleNamespace(messages=[{}, {}])
    sessions = MagicMock()
    sessions.get_or_create.side_effect = [old, new]
    consolidator = SimpleNamespace(compact_idle_session=AsyncMock(return_value="summary"))
    loop = SimpleNamespace(sessions=sessions, consolidator=consolidator)
    msg = SimpleNamespace(channel="test", chat_id="chat", metadata={})
    ctx = CommandContext(msg=msg, session=old, key="test:chat", raw="/compact", loop=loop)

    result = await cmd_compact(ctx)

    consolidator.compact_idle_session.assert_awaited_once_with("test:chat", 8)
    assert result.content == "Compacted session: archived 2 old message(s) and kept 2 recent message(s)."
    assert result.metadata["render_as"] == "text"


@pytest.mark.asyncio
async def test_compact_empty_session_is_clear():
    session = SimpleNamespace(messages=[{}])
    sessions = MagicMock()
    sessions.get_or_create.side_effect = [session, session]
    consolidator = SimpleNamespace(compact_idle_session=AsyncMock(return_value=""))
    loop = SimpleNamespace(sessions=sessions, consolidator=consolidator)
    msg = SimpleNamespace(channel="test", chat_id="chat", metadata={})
    ctx = CommandContext(msg=msg, session=session, key="test:chat", raw="/compact", loop=loop)

    result = await cmd_compact(ctx)

    assert result.content == "Nothing to compact; kept 1 recent message(s)."


@pytest.mark.asyncio
async def test_compact_rejects_arguments():
    msg = SimpleNamespace(channel="test", chat_id="chat", metadata={})
    ctx = CommandContext(msg=msg, session=None, key="test:chat", raw="/compact", args="5", loop=MagicMock())
    result = await cmd_compact(ctx)
    assert result.content == "Usage: `/compact`"


@pytest.mark.asyncio
async def test_compact_reports_missing_summary_without_claiming_success():
    old = SimpleNamespace(messages=[{}, {}, {}, {}])
    new = SimpleNamespace(messages=[{}, {}])
    sessions = MagicMock()
    sessions.get_or_create.side_effect = [old, new]
    consolidator = SimpleNamespace(compact_idle_session=AsyncMock(return_value=None))
    loop = SimpleNamespace(sessions=sessions, consolidator=consolidator)
    msg = SimpleNamespace(channel="test", chat_id="chat", metadata={})
    ctx = CommandContext(msg=msg, session=old, key="test:chat", raw="/compact", loop=loop)

    result = await cmd_compact(ctx)

    assert "no summary was produced" in result.content
    assert "Compacted session" not in result.content
    assert ctx.session is new


@pytest.mark.asyncio
async def test_compact_shortcut_cannot_restore_stale_session(tmp_path):
    from nanobot.session.manager import SessionManager

    sessions = SessionManager(tmp_path)
    original = sessions.get_or_create("test:chat")
    original.add_message("user", "old")
    original.add_message("assistant", "old answer")
    sessions.save(original)

    async def compact(key, _max_suffix):
        sessions.invalidate(key)
        fresh = sessions.get_or_create(key)
        fresh.messages = fresh.messages[-1:]
        sessions.save(fresh)
        return "summary"

    loop = SimpleNamespace(
        sessions=sessions,
        consolidator=SimpleNamespace(compact_idle_session=compact),
    )
    msg = SimpleNamespace(channel="test", chat_id="chat", metadata={})
    ctx = CommandContext(msg=msg, session=original, key="test:chat", raw="/compact", loop=loop)
    await cmd_compact(ctx)

    # The loop's shortcut branch persists the original turn session after dispatch.
    original.add_message("user", "/compact", _command=True)
    sessions.save(original)
    sessions.invalidate("test:chat")
    retained = sessions.get_or_create("test:chat")
    assert [item["content"] for item in retained.messages] == ["old answer", "/compact"]
