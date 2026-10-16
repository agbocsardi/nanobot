from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# Import AgentLoop first to avoid command package initialization cycle.
from nanobot.agent.loop import AgentLoop  # noqa: F401
from nanobot.command.builtin import cmd_compact
from nanobot.command.router import CommandContext
from nanobot.session.compaction import CHECKPOINTS
from nanobot.session.manager import Session


@pytest.mark.asyncio
async def test_compact_checkpoints_old_history_and_reports_counts():
    old = Session(key="test:chat", messages=[{"role": "user", "content": str(i)} for i in range(4)])
    new = Session(key="test:chat", messages=list(old.messages))
    new.metadata[CHECKPOINTS] = [{"first_kept_entry_id": new.messages[2]["_entry_id"]}]
    sessions = MagicMock()
    sessions.get_or_create.side_effect = [old, new]
    consolidator = SimpleNamespace(compact_idle_session=AsyncMock(return_value="summary"))
    loop = SimpleNamespace(sessions=sessions, consolidator=consolidator)
    msg = SimpleNamespace(channel="test", chat_id="chat", metadata={})
    ctx = CommandContext(msg=msg, session=old, key="test:chat", raw="/compact", loop=loop)

    result = await cmd_compact(ctx)

    consolidator.compact_idle_session.assert_awaited_once_with("test:chat")
    assert result.content == (
        "Compacted 2 message(s) into a continuity checkpoint; kept 2 recent message(s). "
        "Original transcript retained."
    )
    assert new.messages == old.messages
    assert result.metadata["render_as"] == "text"


@pytest.mark.asyncio
async def test_compact_empty_session_is_clear():
    session = Session(key="test:chat")
    sessions = MagicMock()
    sessions.get_or_create.side_effect = [session, session]
    consolidator = SimpleNamespace(compact_idle_session=AsyncMock(return_value=""))
    loop = SimpleNamespace(sessions=sessions, consolidator=consolidator)
    msg = SimpleNamespace(channel="test", chat_id="chat", metadata={})
    ctx = CommandContext(msg=msg, session=session, key="test:chat", raw="/compact", loop=loop)

    result = await cmd_compact(ctx)

    assert result.content == "Nothing to compact; original transcript retained."


@pytest.mark.asyncio
async def test_compact_rejects_arguments():
    msg = SimpleNamespace(channel="test", chat_id="chat", metadata={})
    ctx = CommandContext(msg=msg, session=None, key="test:chat", raw="/compact", args="5", loop=MagicMock())
    result = await cmd_compact(ctx)
    assert result.content == "Usage: `/compact`"


@pytest.mark.asyncio
async def test_compact_reports_missing_summary_without_claiming_success():
    old = Session(key="test:chat", messages=[{"role": "user", "content": str(i)} for i in range(4)])
    new = Session(key="test:chat", messages=list(old.messages))
    sessions = MagicMock()
    sessions.get_or_create.side_effect = [old, new]
    consolidator = SimpleNamespace(compact_idle_session=AsyncMock(return_value=None))
    loop = SimpleNamespace(sessions=sessions, consolidator=consolidator)
    msg = SimpleNamespace(channel="test", chat_id="chat", metadata={})
    ctx = CommandContext(msg=msg, session=old, key="test:chat", raw="/compact", loop=loop)

    result = await cmd_compact(ctx)

    assert "could not produce a safe checkpoint" in result.content
    assert new.messages == old.messages
    assert "Compacted session" not in result.content
    assert ctx.session is new


@pytest.mark.asyncio
async def test_compact_shortcut_cannot_restore_stale_session(tmp_path):
    from nanobot.session.manager import SessionManager

    sessions = SessionManager(tmp_path)
    original = sessions.get_or_create("test:chat")
    original.add_message("user", "old")
    original.add_message("assistant", "old answer")
    # Simulate _state_restore's dirty mutation before /compact dispatch.
    original.metadata["workspace_scope"] = {
        "project": str(tmp_path / "project"),
        "mode": "restricted",
    }
    sessions.save(original)
    # Make the scope dirty after the last save, as the live turn does.
    original.metadata["workspace_scope"]["project"] = str(tmp_path / "project-dirty")

    async def compact(key):
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
    assert retained.metadata["workspace_scope"]["project"] == str(tmp_path / "project-dirty")
