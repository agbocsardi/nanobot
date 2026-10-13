"""Cron reports must use turn-local data, never the foreground runtime."""
from types import SimpleNamespace

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.cron.bound_runner import run_bound_cron_job, run_isolated_cron_job
from nanobot.cron.types import CronJob, CronPayload, CronSchedule


class Recorder:
    def __init__(self):
        self.records = []

    def write_run_record(self, run_id, record):
        self.records.append(record)


class Agent:
    model = "frontier-model"
    provider = SimpleNamespace(_spec=SimpleNamespace(name="frontier-provider"))
    last_usage = {"prompt_tokens": 999999}
    tools = SimpleNamespace(get=lambda name: None)

    def __init__(self, captured=True, empty=False):
        self.captured = captured
        self.empty = empty

    def cron_run_snapshot(self):
        return {
            "model": "background-model",
            "provider": SimpleNamespace(_spec=SimpleNamespace(name="background-provider")),
        }

    async def _turn(self, metadata):
        if self.captured:
            metadata["_cron_run_usage"].update({
                "provider": "background-provider",
                "model": "background-model",
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
            })
        if self.empty:
            return None
        return OutboundMessage(channel="cli", chat_id="direct", content="ok")

    async def submit_cron_turn(self, msg):
        return await self._turn(msg.metadata)

    async def process_direct(self, prompt, **kwargs):
        assert kwargs["run_model"] == "background-model"
        return await self._turn(kwargs["metadata"])


@pytest.mark.asyncio
@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("captured", [False, True])
async def test_cron_usage_ignores_foreground_state(isolated, empty, captured):
    agent = Agent(captured=captured, empty=empty)
    recorder = Recorder()
    job = CronJob(
        id="usage-test", name="usage-test",
        schedule=CronSchedule(kind="every", every_ms=60000),
        payload=CronPayload(
            message="test", session_key="cli:direct",
            origin_channel="cli", origin_chat_id="direct",
            isolated=isolated, silent=True,
        ),
    )
    if isolated:
        async def deliver(*args, **kwargs):
            pytest.fail("Silent job must not deliver")
        await run_isolated_cron_job(job, agent=agent, cron=recorder, deliver=deliver)
    else:
        await run_bound_cron_job(job, agent=agent, cron=recorder)

    usage = recorder.records[-1]["usage"]
    assert usage["model"] == "background-model"
    assert usage["provider"] == "background-provider"
    assert usage.get("prompt_tokens") == (10 if captured else None)
    assert agent.model == "frontier-model"
