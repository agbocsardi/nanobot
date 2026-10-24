"""Regression tests for resolving a cron run's response from its run record.

Guards issue #62: ``CronRunRecord`` carried no ``run_id``, so the WebUI route read
an attribute that did not exist and raised ``AttributeError``.
"""

from __future__ import annotations

import json
from pathlib import Path

from nanobot.cron.types import CronJob, CronPayload, CronRunRecord, CronRunResult
from nanobot.utils.run_records import safe_run_record_name
from nanobot.webui.automation_results import cron_run_response

RUN_ID = "job1:1792792800000:abcd1234"


def _write_record(runs_dir: Path, *, run_id: str, response: str, status: str = "ok") -> None:
    name = safe_run_record_name(run_id)
    (runs_dir / f"{name}.json").write_text(
        json.dumps({
            "kind": "cron",
            "run_id": run_id,
            "job_id": "job1",
            "session_key": "session-1",
            "status": status,
            "response": response,
        }),
        encoding="utf-8",
    )


def _job() -> CronJob:
    return CronJob(id="job1", name="job", payload=CronPayload(session_key="session-1"))


def test_run_record_always_exposes_run_id() -> None:
    """The attribute the route reads must exist, including on legacy records."""
    assert CronRunRecord().run_id is None
    assert CronRunRecord(run_id=RUN_ID).run_id == RUN_ID


def test_cron_run_result_carries_run_id() -> None:
    assert CronRunResult("body", run_id=RUN_ID).run_id == RUN_ID


def test_response_resolved_exactly_from_run_id(tmp_path: Path) -> None:
    _write_record(tmp_path, run_id=RUN_ID, response="exact response")
    job = _job()
    record = CronRunRecord(run_at_ms=1792792800000, run_id=RUN_ID, status="ok", duration_ms=1000)
    job.state.run_history.append(record)

    assert cron_run_response(tmp_path, job, record) == "exact response"


def test_legacy_record_without_run_id_falls_back_to_interval_match(tmp_path: Path) -> None:
    """A record written before this fix resolves via the timestamp-overlap path."""
    _write_record(tmp_path, run_id=RUN_ID, response="interval response")
    job = _job()
    record = CronRunRecord(run_at_ms=1792792800000, status="ok", duration_ms=1000)
    job.state.run_history.append(record)

    assert cron_run_response(tmp_path, job, record) == "interval response"


def test_run_id_pointing_at_missing_record_returns_none(tmp_path: Path) -> None:
    job = _job()
    record = CronRunRecord(
        run_at_ms=1792792800000, run_id="job1:1:missing", status="ok", duration_ms=1000,
    )
    job.state.run_history.append(record)

    assert cron_run_response(tmp_path, job, record) is None
