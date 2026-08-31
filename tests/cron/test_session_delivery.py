import pytest

from nanobot.cron.session_delivery import origin_delivery_context
from nanobot.cron.types import CronJob, CronPayload


def test_origin_delivery_context_uses_explicit_origin_fields() -> None:
    metadata = {
        "context_chat_id": "456",
        "parent_channel_id": "456",
        "thread_id": "777",
    }
    job = CronJob(
        id="thread-check",
        name="Thread check",
        payload=CronPayload(
            message="check",
            session_key="discord:456:thread:777",
            origin_channel="discord",
            origin_chat_id="777",
            origin_metadata=metadata,
        ),
    )

    channel, chat_id, returned_metadata = origin_delivery_context(job)

    assert channel == "discord"
    assert chat_id == "777"
    assert returned_metadata == metadata
    assert returned_metadata is not metadata


def test_origin_delivery_context_rejects_missing_origin_fields() -> None:
    job = CronJob(
        id="old-bound",
        name="Old bound job",
        payload=CronPayload(
            message="check",
            session_key="websocket:chat-1",
        ),
    )

    with pytest.raises(ValueError, match="missing origin delivery context"):
        origin_delivery_context(job)


@pytest.mark.asyncio
async def test_bound_cron_run_stamps_cron_mode() -> None:
    """Bound cron turns reach the agent with mode=cron stamped on metadata."""
    from nanobot.bus.events import OutboundMessage
    from nanobot.cron.bound_runner import run_bound_cron_job
    from nanobot.cron.types import CronSchedule

    class _FakeAgent:
        tools = type("Tools", (), {"get": staticmethod(lambda _name: None)})()
        provider = type("Prov", (), {})()
        model = "test-model"
        last_usage = {"prompt_tokens": 1}

        def __init__(self):
            self.seen_metadata = None

        def cron_run_snapshot(self):
            return None

        async def submit_cron_turn(self, msg):
            self.seen_metadata = msg.metadata
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content="ok")

    class _FakeCron:
        def write_run_record(self, *_args, **_kwargs):
            pass

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

    assert agent.seen_metadata.get("_interaction_mode") == "cron"
