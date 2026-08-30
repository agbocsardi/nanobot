"""Deterministic timing tests for separated agent/delivery timestamps.

The agent turn finish and delivery finish are recorded separately everywhere
they surface: the runner's run record (runs/*.json), the job state
(``last_delivery_at_ms``), the persisted run history
(``agentFinishedAtMs``/``deliveryFinishedAtMs``), and the service-side
``CronRunRecord`` built from callback result metadata.
"""

from __future__ import annotations

import json

import pytest

from nanobot.cron.service import CronService
from nanobot.cron.types import CronRunResult, CronSchedule


@pytest.mark.asyncio
async def test_run_history_captures_agent_and_delivery_finish_from_callback_metadata(
    tmp_path,
) -> None:
    """The service reads agent_finished_at_ms / delivery_finished_at_ms from
    the callback's structured result and persists both into jobs.json."""

    async def callback(_job):
        return CronRunResult(
            "ok",
            agent_finished_at_ms=123_400,
            delivery_finished_at_ms=123_750,
        )

    service = CronService(tmp_path / "jobs.json", on_job=callback)
    job = service.add_job(
        name="x", schedule=CronSchedule(kind="every", every_ms=60_000), message="x"
    )

    assert await service.run_job(job.id) is True

    rec = service.get_job(job.id).state.run_history[0]
    assert rec.agent_finished_at_ms == 123_400
    assert rec.delivery_finished_at_ms == 123_750
    assert rec.agent_finished_at_ms < rec.delivery_finished_at_ms

    raw = json.loads((tmp_path / "jobs.json").read_text(encoding="utf-8"))
    history = raw["jobs"][0]["state"]["runHistory"][0]
    assert history["agentFinishedAtMs"] == 123_400
    assert history["deliveryFinishedAtMs"] == 123_750
    assert raw["jobs"][0]["state"]["lastDeliveryAtMs"] == 123_750

    reloaded = CronService(tmp_path / "jobs.json").get_job(job.id)
    assert reloaded is not None
    assert reloaded.state.last_delivery_at_ms == 123_750
    assert reloaded.state.run_history[0].agent_finished_at_ms == 123_400
    assert reloaded.state.run_history[0].delivery_finished_at_ms == 123_750


@pytest.mark.asyncio
async def test_delivery_failure_still_records_delivery_finished_at_ms(tmp_path) -> None:
    """When the runner re-raises after a delivery failure it stamps
    job.state.last_delivery_at_ms; the service-side run record must carry
    delivery_finished_at_ms even though the run itself is an error."""

    async def callback(job):
        # Mirrors run_isolated_cron_job's delivery-failure path.
        job.state.last_delivery_at_ms = 55_000
        raise RuntimeError("channel unavailable")

    service = CronService(tmp_path / "jobs.json", on_job=callback)
    job = service.add_job(
        name="x", schedule=CronSchedule(kind="every", every_ms=60_000), message="x"
    )

    assert await service.run_job(job.id) is True

    stored = service.get_job(job.id)
    assert stored is not None
    assert stored.state.last_status == "error"
    assert stored.state.last_error == "channel unavailable"
    rec = stored.state.run_history[0]
    assert rec.status == "error"
    assert rec.delivery_finished_at_ms == 55_000
    assert stored.state.last_delivery_at_ms == 55_000


def test_old_history_loads_with_new_fields_none(tmp_path) -> None:
    """Pre-transactional/pre-timestamp stores have no new keys: loading must
    produce None (not crash), and saving must write the new fields back."""
    store_path = tmp_path / "jobs.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "old",
                        "name": "old",
                        "enabled": True,
                        "schedule": {"kind": "every", "everyMs": 60_000},
                        "payload": {"kind": "agent_turn", "message": "x"},
                        "state": {
                            "lastDeliveryStatus": "delivered",
                            "runHistory": [{"runAtMs": 7, "startedAtMs": 7, "status": "ok"}],
                        },
                        "createdAtMs": 1,
                        "updatedAtMs": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    service = CronService(store_path)
    job = service.get_job("old")
    assert job is not None
    assert job.state.last_delivery_at_ms is None
    rec = job.state.run_history[0]
    assert rec.agent_finished_at_ms is None
    assert rec.delivery_finished_at_ms is None

    # The serializer writes the new fields for the legacy record too.
    service._save_store()
    raw = json.loads(store_path.read_text(encoding="utf-8"))
    state = raw["jobs"][0]["state"]
    assert "lastDeliveryAtMs" in state
    assert "agentFinishedAtMs" in state["runHistory"][0]
    assert "deliveryFinishedAtMs" in state["runHistory"][0]

    # And the round trip is stable.
    again = CronService(store_path).get_job("old")
    assert again is not None
    assert again.state.last_delivery_at_ms is None
    assert again.state.run_history[0].agent_finished_at_ms is None
