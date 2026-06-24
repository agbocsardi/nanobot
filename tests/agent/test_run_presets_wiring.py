"""Wiring for designated run presets (subagent / cron)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import Config
from nanobot.cron.bound_runner import run_bound_cron_job
from nanobot.cron.session_turns import CRON_RUN_SNAPSHOT_META
from nanobot.cron.types import CronJob, CronPayload, CronSchedule
from nanobot.providers.factory import ProviderSnapshot


def _config() -> Config:
    return Config(
        agents={
            "defaults": {
                "workspace": "/tmp/nanobot-test",
                "runPresets": {"subagent": "cheap", "cron": "cheap"},
            }
        },
        modelPresets={"cheap": {"model": "cheap-model", "provider": "custom"}},
    )


def test_agent_loop_from_config_wires_subagent_and_cron_snapshots() -> None:
    cfg = _config()
    main_provider = MagicMock()
    main_provider.get_default_model.return_value = "main-model"
    bg_provider = MagicMock()
    bg_snapshot = ProviderSnapshot(
        provider=bg_provider,
        model="cheap-model",
        context_window_tokens=1234,
        signature=("test",),
    )

    with patch("nanobot.providers.factory.make_provider", return_value=main_provider), patch(
        "nanobot.agent.loop.preset_helpers.build_run_provider_snapshot",
        return_value=bg_snapshot,
    ) as build:
        loop = AgentLoop.from_config(cfg, bus=MessageBus())

    assert build.call_count == 2
    assert loop.cron_run_snapshot()["model"] == "cheap-model"
    assert loop.subagents.run_provider is bg_provider
    assert loop.subagents.run_model == "cheap-model"


class _FakeTools:
    def get(self, _name):
        return None


class _FakeAgent:
    tools = _FakeTools()
    provider = MagicMock()
    model = "main-model"
    last_usage = {"prompt_tokens": 1}

    def __init__(self):
        self.seen_metadata = None

    def cron_run_snapshot(self):
        return {"provider": "provider-object", "model": "cheap-model", "context_window_tokens": 1234}

    async def submit_cron_turn(self, msg):
        self.seen_metadata = msg.metadata
        from nanobot.bus.events import OutboundMessage

        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content="ok")


class _FakeCron:
    def write_run_record(self, *_args, **_kwargs):
        pass


@pytest.mark.asyncio
async def test_bound_cron_turn_carries_designated_run_snapshot() -> None:
    agent = _FakeAgent()
    job = CronJob(
        id="j1",
        name="check",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        payload=CronPayload(
            kind="agent_turn",
            message="check things",
            session_key="cli:direct",
            origin_channel="cli",
            origin_chat_id="direct",
        ),
    )

    await run_bound_cron_job(job, agent=agent, cron=_FakeCron())

    assert agent.seen_metadata[CRON_RUN_SNAPSHOT_META]["model"] == "cheap-model"
