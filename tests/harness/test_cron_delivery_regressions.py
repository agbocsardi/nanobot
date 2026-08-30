"""Deterministic cron regressions: delivery (or postcondition) failures must be
recorded in the run history and persisted run records, and success must never
be announced.

Incident-derived: a cron reminder was reported as done after its delivery
channel had failed, leaving no error trail; the run must settle as error with
a delivery-failure record, and an in-band turn whose tool postcondition fails
must reply under the incomplete marker instead of a bare success.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.cron.bound_runner import run_bound_cron_job, run_isolated_cron_job
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob, CronJobState, CronPayload, CronSchedule
from nanobot.providers.base import LLMResponse, ToolCallRequest
from tests.harness.helpers import StaticTool, run_script


class FakeCronAgent:
    """Minimal BoundCronAgent: no cron tool, turn results scripted offline."""

    def __init__(self, turn):
        self._turn = turn
        self._last_usage: dict[str, int] = {}
        self.model = "fixture-model"
        self.provider = SimpleNamespace(_spec=SimpleNamespace(name="fixture-provider"))
        self.tools = SimpleNamespace(get=lambda name: None)

    @property
    def last_usage(self) -> dict[str, int]:
        return self._last_usage

    async def process_direct(self, *args: Any, **kwargs: Any) -> Any:
        content, usage = await self._turn()
        self._last_usage = dict(usage or {})
        return SimpleNamespace(content=content)

    async def submit_cron_turn(self, _msg: Any, **_kwargs: Any) -> Any:
        content, usage = await self._turn()
        self._last_usage = dict(usage or {})
        return SimpleNamespace(content=content)


def _job(job_id: str) -> CronJob:
    return CronJob(
        id=job_id,
        name="Standup reminder",
        schedule=CronSchedule(kind="every", every_ms=86_400_000),
        payload=CronPayload(
            message="post the standup notes",
            session_key="telegram:1",
            origin_channel="telegram",
            origin_chat_id="1",
        ),
        state=CronJobState(next_run_at_ms=1_000),
    )


def _make_service(tmp_path: Path) -> CronService:
    store_path = tmp_path / "cron" / "jobs.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    return CronService(store_path=store_path)


async def _success_turn() -> tuple[str, dict[str, int]]:
    # The agent turn itself succeeds; the failure is attributable to delivery.
    return "standup notes are posted", {}


async def _failed_postcondition_turn() -> tuple[str, dict[str, int]]:
    # In-band cron turn whose only tool reports a failed postcondition.
    tools = ToolRegistry()
    tools.register(StaticTool(
        "deliver",
        ToolResult(
            "delivered",
            side_effects=[{"kind": "message", "recipient": "user-1"}],
            postcondition="failed",
        ),
    ))
    result, _ = await run_script(
        [
            LLMResponse(
                content="sending",
                tool_calls=[ToolCallRequest(id="call_1", name="deliver", arguments={})],
            ),
            LLMResponse(content="reminder sent"),
        ],
        tools=tools,
    )
    return result.final_content or "", dict(result.usage)


def _latest_run_record(service: CronService) -> dict[str, Any]:
    records_dir = service.store_path.parent / "runs"
    files = sorted(records_dir.glob("*.json"))
    assert files, f"no run records written under {records_dir}"
    return json.loads(files[-1].read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_cron_delivery_failure_marks_state_records_and_never_announces_success(tmp_path) -> None:
    job = _job("reminder-1")
    service = _make_service(tmp_path)

    async def _delivery_failer(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("chat channel disconnected")

    async def on_job(job: CronJob) -> None:
        await run_isolated_cron_job(
            job,
            agent=FakeCronAgent(_success_turn),
            cron=service,
            deliver=_delivery_failer,
        )

    service.on_job = on_job
    await service._execute_job(job, scheduled_ms=1_000)

    # Terminal state reflects the delivery failure, not a successful run.
    assert job.state.last_status == "error"
    assert job.state.last_error == "chat channel disconnected"
    assert job.state.last_delivery_status == "failed"
    assert job.state.last_delivery_error == "chat channel disconnected"
    assert job.state.last_delivery_at_ms is not None
    assert not job.state.run_history or job.state.run_history[-1].delivery_status != "delivered"

    # Run-history entry carries the delivery failure.
    entry = job.state.run_history[-1]
    assert entry.status == "error"
    assert entry.delivery_status == "failed"
    assert entry.delivery_error == "chat channel disconnected"

    # Persisted run record carries the delivery failure.
    record = _latest_run_record(service)
    assert record["kind"] == "cron"
    assert record["status"] == "error"
    assert record["error"] == "delivery failed: chat channel disconnected"
    assert record["delivery"] == {
        "status": "failed",
        "error": "chat channel disconnected",
        "at_ms": entry.delivery_finished_at_ms,
    }
    # No record anywhere claims an ok run or delivered delivery.
    assert all(r["status"] != "ok" for r in _all_run_records(service))


def _all_run_records(service: CronService) -> list[dict[str, Any]]:
    records_dir = service.store_path.parent / "runs"
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(records_dir.glob("*.json"))
    ]


@pytest.mark.asyncio
async def test_isolated_cron_runner_rethrows_delivery_failure(tmp_path) -> None:
    job = _job("reminder-2")
    service = _make_service(tmp_path)

    async def _delivery_failer(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("chat channel disconnected")

    with pytest.raises(RuntimeError, match="chat channel disconnected"):
        await run_isolated_cron_job(
            job,
            agent=FakeCronAgent(_success_turn),
            cron=service,
            deliver=_delivery_failer,
        )

    # The caller gets an exception, not a success result, so no announcement.
    assert job.state.last_delivery_status == "failed"
    assert job.state.last_delivery_error == "chat channel disconnected"
    record = _latest_run_record(service)
    assert record["status"] == "error"
    assert record["delivery"]["status"] == "failed"


@pytest.mark.asyncio
async def test_cron_turn_with_failed_postcondition_replies_incomplete(tmp_path) -> None:
    job = _job("reminder-3")
    service = _make_service(tmp_path)
    captured: dict[str, Any] = {}

    async def on_job(job: CronJob) -> Any:
        result = await run_bound_cron_job(
            job,
            agent=FakeCronAgent(_failed_postcondition_turn),
            cron=service,
        )
        captured["result"] = result
        return result

    service.on_job = on_job
    await service._execute_job(job, scheduled_ms=1_000)

    # User-visible reply is suppressed: the incomplete marker prefixes any
    # bare success claim instead of announcing the reminder as sent.
    reply = str(captured["result"])
    assert reply.startswith(
        "Incomplete: one or more tool operations failed or could not be verified."
    )
    assert "reminder sent" in reply

    # Delivery routed fine in-band; the suppression lives in the reply and in
    # the persisted record, not in the routing state.
    assert job.state.last_status == "ok"
    assert job.state.last_delivery_status == "delivered"
    entry = job.state.run_history[-1]
    assert entry.status == "ok"
    assert entry.delivery_status == "delivered"

    record = _latest_run_record(service)
    assert record["status"] == "ok"
    assert record["delivery"]["status"] == "delivered"
    assert "Incomplete: one or more tool operations failed" in record["response"]
    assert "reminder sent" in record["response"]
