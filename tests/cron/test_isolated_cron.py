"""Isolated cron execution: runs in a dedicated session, never the live chat.

Verifies run_isolated_cron_job:
- executes via process_direct (not submit_cron_turn), in a fresh per-run session,
- suppresses message-tool delivery and toggles the cron context for the turn,
- delivers the final reply to the origin chat only when the job is not silent,
- writes queued -> ok run records, and an error record when the turn raises.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.message import MessageTool
from nanobot.bus.events import OutboundMessage
from nanobot.cron.bound_runner import run_isolated_cron_job
from nanobot.cron.types import CronJob, CronPayload, CronSchedule


def _spied_cron_tool() -> CronTool:
    tool = CronTool(cron_service=None, default_timezone="UTC")
    tokens: list[bool] = []
    resets: list[bool] = []
    orig_set = tool.set_cron_context
    orig_reset = tool.reset_cron_context

    def set_ctx(active: bool) -> bool:
        tokens.append(active)
        return orig_set(active)

    def reset_ctx(token: bool) -> None:
        resets.append(token)
        orig_reset(token)

    tool.set_cron_context = set_ctx  # type: ignore[method-assign]
    tool.reset_cron_context = reset_ctx  # type: ignore[method-assign]
    tool._spy_tokens = tokens  # type: ignore[attr-defined]
    tool._spy_resets = resets  # type: ignore[attr-defined]
    return tool


def _spied_message_tool() -> MessageTool:
    tool = MessageTool(workspace=".", restrict_to_workspace=False)
    set_calls: list[bool] = []
    resets: list[bool] = []
    orig_set = tool.set_suppress_delivery
    orig_reset = tool.reset_suppress_delivery

    def set_suppress(active: bool) -> bool:
        set_calls.append(active)
        return orig_set(active)

    def reset_suppress(token: bool) -> None:
        resets.append(token)
        orig_reset(token)

    tool.set_suppress_delivery = set_suppress  # type: ignore[method-assign]
    tool.reset_suppress_delivery = reset_suppress  # type: ignore[method-assign]
    tool._spy_set_calls = set_calls  # type: ignore[attr-defined]
    tool._spy_resets = resets  # type: ignore[attr-defined]
    return tool


class _FakeTools:
    def __init__(self) -> None:
        self.cron = _spied_cron_tool()
        self.message = _spied_message_tool()

    def get(self, name: str) -> Any:
        if name == "cron":
            return self.cron
        if name == "message":
            return self.message
        return None


class _FakeAgent:
    """Minimal agent satisfying the BoundCronAgent protocol."""

    def __init__(self, *, content: str = "done", exc: BaseException | None = None) -> None:
        self.tools = _FakeTools()
        self.provider = type("Prov", (), {})()
        self.model = "test-model"
        self.last_usage: dict[str, int] = {"prompt_tokens": 1, "completion_tokens": 2}
        self._content = content
        self._exc = exc
        self.process_direct_calls: list[dict[str, Any]] = []

    def cron_run_snapshot(self) -> dict[str, Any] | None:
        return None

    def cron_run_snapshot_for_preset(self, name: str) -> dict[str, Any] | None:
        return None

    async def process_direct(self, content: str, **kwargs: Any) -> OutboundMessage | None:
        self.process_direct_calls.append({"content": content, **kwargs})
        if self._exc is not None:
            raise self._exc
        return OutboundMessage(
            channel=kwargs.get("channel", "cli"),
            chat_id=kwargs.get("chat_id", "direct"),
            content=self._content,
        )


class _FakeRecorder:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def write_run_record(self, run_id: str, record: dict[str, Any]) -> None:
        self.records.append((run_id, record))


def _job(*, silent: bool = False, isolated: bool = True) -> CronJob:
    return CronJob(
        id="job1",
        name="daily-biometrics",
        enabled=True,
        schedule=CronSchedule(kind="every", every_ms=60_000),
        payload=CronPayload(
            kind="agent_turn",
            message="Check biometrics and append to daily note",
            session_key="telegram:42",
            origin_channel="telegram",
            origin_chat_id="42",
            silent=silent,
            isolated=isolated,
        ),
    )


def _make_deliver() -> tuple[list[dict[str, Any]], Any]:
    calls: list[dict[str, Any]] = []

    async def deliver(msg: OutboundMessage, *, record: bool = False, session_key: str | None = None) -> None:
        calls.append({"channel": msg.channel, "chat_id": msg.chat_id, "content": msg.content, "record": record})

    return calls, deliver


@pytest.mark.asyncio
async def test_isolated_runs_via_process_direct_and_delivers_when_not_silent() -> None:
    agent = _FakeAgent(content="biometrics ok")
    recorder = _FakeRecorder()
    delivered, deliver = _make_deliver()

    resp = await run_isolated_cron_job(_job(silent=False), agent=agent, cron=recorder, deliver=deliver)

    assert resp == "biometrics ok"
    # process_direct, never submit_cron_turn
    assert len(agent.process_direct_calls) == 1
    call = agent.process_direct_calls[0]
    assert call["session_key"].startswith("cron:job1:")
    assert call["ephemeral"] is True
    assert call["channel"] == "telegram"
    assert call["chat_id"] == "42"
    # message-tool delivery suppressed around the turn, then restored
    assert agent.tools.message._spy_set_calls == [True]
    assert len(agent.tools.message._spy_resets) == 1
    # cron context toggled for the turn, then restored
    assert agent.tools.cron._spy_tokens == [True]
    assert len(agent.tools.cron._spy_resets) == 1
    # final reply delivered to origin chat, recorded
    assert delivered == [{"channel": "telegram", "chat_id": "42", "content": "biometrics ok", "record": True}]
    # run records: queued -> ok
    statuses = [r["status"] for _, r in recorder.records]
    assert statuses == ["queued", "ok"]
    ok = recorder.records[-1][1]
    assert ok["isolated"] is True
    assert ok["silent"] is False
    assert ok["response"] == "biometrics ok"
    assert ok["usage"]["provider"]


@pytest.mark.asyncio
async def test_isolated_silent_job_does_not_deliver() -> None:
    agent = _FakeAgent(content="nothing to report")
    recorder = _FakeRecorder()
    delivered, deliver = _make_deliver()

    resp = await run_isolated_cron_job(_job(silent=True), agent=agent, cron=recorder, deliver=deliver)

    assert resp == "nothing to report"
    assert delivered == []  # silent swallows the final reply
    assert recorder.records[-1][1]["silent"] is True
    assert recorder.records[-1][1]["status"] == "ok"
    # suppression still toggled even when silent
    assert agent.tools.message._spy_set_calls == [True]
    assert len(agent.tools.message._spy_resets) == 1


@pytest.mark.asyncio
async def test_isolated_records_error_and_reraises_when_turn_raises() -> None:
    agent = _FakeAgent(exc=RuntimeError("boom"))
    recorder = _FakeRecorder()
    delivered, deliver = _make_deliver()

    with pytest.raises(RuntimeError, match="boom"):
        await run_isolated_cron_job(_job(), agent=agent, cron=recorder, deliver=deliver)

    assert delivered == []
    statuses = [r["status"] for _, r in recorder.records]
    assert statuses == ["queued", "error"]
    assert recorder.records[-1][1]["error"] == "boom"
    # tokens restored despite the exception
    assert len(agent.tools.message._spy_resets) == 1
    assert len(agent.tools.cron._spy_resets) == 1


def test_default_isolated_true_for_legacy_payload() -> None:
    # A payload constructed without an explicit isolated field (e.g. legacy
    # jobs deserialized from the store) must default to isolated.
    legacy = CronPayload(
        kind="agent_turn",
        message="m",
        session_key="telegram:42",
        origin_channel="telegram",
        origin_chat_id="42",
    )
    assert legacy.isolated is True


if __name__ == "__main__":
    asyncio.run(test_isolated_runs_via_process_direct_and_delivers_when_not_silent())
    asyncio.run(test_isolated_silent_job_does_not_deliver())
    asyncio.run(test_isolated_records_error_and_reraises_when_turn_raises())
    test_default_isolated_true_for_legacy_payload()
    print("OK")
