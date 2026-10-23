"""Silence must survive delegation.

A silent cron run that spawns subagents leaks their results into the bound chat:
the announcement turn is a fresh inbound message with no cron metadata, so the
cron-only silent check never sees it. The spawn origin now carries the silent
flag and the announcement turn is tagged SILENT_TURN_META.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.cron.session_turns import (
    CRON_SILENT_META,
    CRON_TRIGGER_META,
    SILENT_TURN_META,
)

_MAX_TOOL_RESULT_CHARS = 4000


@pytest.mark.asyncio
async def test_spawn_tool_propagates_silent_from_cron_turn() -> None:
    from nanobot.agent.tools.context import RequestContext
    from nanobot.agent.tools.spawn import SpawnTool

    mgr = MagicMock()
    mgr.spawn = AsyncMock(return_value="ok")
    tool = SpawnTool(mgr)
    tool.set_context(
        RequestContext(
            channel="discord",
            chat_id="123",
            session_key="cron:job",
            metadata={CRON_TRIGGER_META: {"job_id": "j1"}, CRON_SILENT_META: True},
        )
    )

    await tool.execute("do the thing")

    assert mgr.spawn.await_args.kwargs["silent"] is True


@pytest.mark.asyncio
async def test_spawn_tool_normal_turn_is_not_silent() -> None:
    from nanobot.agent.tools.context import RequestContext
    from nanobot.agent.tools.spawn import SpawnTool

    mgr = MagicMock()
    mgr.spawn = AsyncMock(return_value="ok")
    tool = SpawnTool(mgr)
    tool.set_context(
        RequestContext(channel="telegram", chat_id="1", session_key="telegram:1", metadata={})
    )

    await tool.execute("do the thing")

    assert mgr.spawn.await_args.kwargs["silent"] is False


@pytest.mark.asyncio
async def test_silent_spawn_announces_without_publishing(tmp_path) -> None:
    """The announcement turn must be tagged silent so the loop suppresses it."""
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )

    async def fake_run(spec):
        return SimpleNamespace(
            stop_reason="done", final_content="done", error=None, tool_events=[],
        )

    mgr.runner.run = AsyncMock(side_effect=fake_run)

    published: list = []

    async def capture(msg):
        published.append(msg)

    bus.publish_inbound = capture  # type: ignore[method-assign]

    await mgr.spawn(
        task="do task",
        origin_channel="discord",
        origin_chat_id="123",
        session_key="cron:job",
        silent=True,
    )
    await asyncio.gather(*mgr._running_tasks.values(), return_exceptions=True)

    assert published, "announcement was never injected"
    assert published[-1].metadata.get(SILENT_TURN_META) is True


@pytest.mark.asyncio
async def test_normal_spawn_announcement_is_untagged(tmp_path) -> None:
    """A normal chat's subagent result must still reach the user."""
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )

    async def fake_run(spec):
        return SimpleNamespace(
            stop_reason="done", final_content="done", error=None, tool_events=[],
        )

    mgr.runner.run = AsyncMock(side_effect=fake_run)

    published: list = []

    async def capture(msg):
        published.append(msg)

    bus.publish_inbound = capture  # type: ignore[method-assign]

    await mgr.spawn(
        task="do task",
        origin_channel="telegram",
        origin_chat_id="1",
        session_key="telegram:1",
    )
    await asyncio.gather(*mgr._running_tasks.values(), return_exceptions=True)

    assert published, "announcement was never injected"
    assert SILENT_TURN_META not in published[-1].metadata
