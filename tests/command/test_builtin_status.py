"""Tests for /status operational sections (cron jobs + effective model chain)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanobot.bus.events import InboundMessage
from nanobot.command.builtin import (
    BUILTIN_COMMAND_SPECS,
    cmd_status,
    register_builtin_commands,
)
from nanobot.command.router import CommandContext, CommandRouter
from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule


class _Session:
    def get_history(self, max_messages: int = 0) -> list:
        return []


def _loop(tmp_path, *, with_cron: bool = True) -> SimpleNamespace:
    cron = CronService(tmp_path / "cron" / "jobs.json") if with_cron else None
    return SimpleNamespace(
        workspace=str(tmp_path),
        sessions=SimpleNamespace(get_or_create=lambda key: _Session()),
        consolidator=SimpleNamespace(
            estimate_session_prompt_tokens=lambda session: (0, None)
        ),
        _last_usage={"prompt_tokens": 0},
        web_config=None,
        _active_tasks={},
        subagents=SimpleNamespace(get_running_count_by_session=lambda key: 0),
        model="gpt-test",
        model_preset="default",
        model_presets={},
        _start_time=1_700_000_000.0,
        context_window_tokens=65_536,
        provider=SimpleNamespace(generation=SimpleNamespace(max_tokens=4096)),
        cron_service=cron,
        _cron_run_snapshot=None,
        _subagent_run_snapshot=None,
    )


def _ctx(tmp_path, *, with_cron: bool = True) -> CommandContext:
    loop = _loop(tmp_path, with_cron=with_cron)
    msg = InboundMessage(channel="cli", sender_id="u1", chat_id="direct", content="/status")
    return CommandContext(
        msg=msg, session=None, key=msg.session_key, raw="/status", args="", loop=loop
    )


@pytest.mark.asyncio
async def test_status_renders_cron_jobs_with_run_state(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    job = ctx.loop.cron_service.add_job(
        name="test job",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="hello",
    )
    await ctx.loop.cron_service.run_job(job.id)

    out = await cmd_status(ctx)

    assert "## Cron jobs" in out.content
    assert "test job" in out.content
    assert "every 1m" in out.content
    assert "last run:" in out.content
    assert "(ok)" in out.content
    assert "next:" in out.content


@pytest.mark.asyncio
async def test_status_empty_cron_store_does_not_crash(tmp_path) -> None:
    out = await cmd_status(_ctx(tmp_path))

    assert "## Cron jobs" in out.content
    assert "no scheduled jobs" in out.content
    assert "## Model chain" in out.content


@pytest.mark.asyncio
async def test_status_without_cron_service_skips_cron_section(tmp_path) -> None:
    out = await cmd_status(_ctx(tmp_path, with_cron=False))

    assert "## Cron jobs" not in out.content
    assert "## Model chain" in out.content
    assert "- foreground: default (gpt-test)" in out.content
    assert "- cron: foreground preset" in out.content
    assert "- subagents: foreground preset" in out.content
    assert "fallback order:" in out.content


@pytest.mark.asyncio
async def test_status_model_chain_lists_no_secrets(tmp_path) -> None:
    out = await cmd_status(_ctx(tmp_path))

    # The chain section prints preset/model names only; nothing secret-shaped.
    chain = out.content.split("## Model chain", 1)[1]
    for line in chain.splitlines():
        assert "token" not in line.lower()
        assert "key" not in line.lower()
        assert "api" not in line.lower()


@pytest.mark.asyncio
async def test_status_registered_and_dispatchable(tmp_path) -> None:
    specs = {spec.command: spec for spec in BUILTIN_COMMAND_SPECS}
    assert "/status" in specs

    router = CommandRouter()
    register_builtin_commands(router)
    ctx = _ctx(tmp_path)
    out = await router.dispatch(ctx)
    assert out is not None
    assert "## Model chain" in out.content
