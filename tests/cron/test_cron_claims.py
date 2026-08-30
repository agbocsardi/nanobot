import asyncio
import json

import pytest

from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule


def _service(tmp_path):
    return CronService(tmp_path / "jobs.json")

def _due(service, job_id, now=1000):
    service._running = True
    store = service._load_store()
    job = next(j for j in store.jobs if j.id == job_id)
    job.state.next_run_at_ms = now
    service._save_store()
    return job


@pytest.mark.asyncio
async def test_two_services_claim_once_and_expired_claim_recovers(tmp_path, monkeypatch):
    now = 1000
    monkeypatch.setattr("nanobot.cron.service._now_ms", lambda: now)
    first, second = _service(tmp_path), _service(tmp_path)
    first._running = True
    job = first.add_job("x", CronSchedule(kind="every", every_ms=100), "x")
    _due(first, job.id, now)
    a = first._claim_occurrence(job.id, now)
    assert a is not None
    assert second._claim_occurrence(job.id, now) is None
    now = 400_001
    b = second._claim_occurrence(job.id, 1000)
    assert b is not None and b[1] != a[1]


@pytest.mark.asyncio
async def test_stale_completion_is_token_fenced(tmp_path, monkeypatch):
    now = 1000
    monkeypatch.setattr("nanobot.cron.service._now_ms", lambda: now)
    first, second = _service(tmp_path), _service(tmp_path)
    first._running = True
    job = first.add_job("x", CronSchedule(kind="every", every_ms=100), "x")
    _due(first, job.id, now)
    old = first._claim_occurrence(job.id, now)
    now = 400_001
    new = second._claim_occurrence(job.id, 1000)
    assert old and new
    assert not first._finalize_claim(old[0], 1000, old[1])
    assert second._finalize_claim(new[0], 1000, new[1])


@pytest.mark.asyncio
async def test_manual_and_timer_overlap_runs_once(tmp_path, monkeypatch):
    monkeypatch.setattr("nanobot.cron.service._now_ms", lambda: 1000)
    calls = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def callback(job):
        calls.append(job.id)
        entered.set()
        await release.wait()

    first = CronService(tmp_path / "jobs.json", on_job=callback)
    first._running = True
    job = first.add_job("x", CronSchedule(kind="every", every_ms=100), "x")
    _due(first, job.id)
    second = CronService(tmp_path / "jobs.json", on_job=callback)
    timer = asyncio.create_task(first._execute_due_job(job, 1000))
    await entered.wait()
    assert await second.run_job(job.id) is False
    release.set()
    await timer
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_concurrent_finalizations_merge_and_preserve_user_edit(tmp_path, monkeypatch):
    now = 1000
    monkeypatch.setattr("nanobot.cron.service._now_ms", lambda: now)
    service = _service(tmp_path)
    service._running = True
    one = service.add_job("one", CronSchedule(kind="every", every_ms=100), "x")
    two = service.add_job("two", CronSchedule(kind="every", every_ms=100), "x")
    _due(service, one.id, now)
    _due(service, two.id, now)
    a = service._claim_occurrence(one.id, now)
    b = service._claim_occurrence(two.id, now)
    assert a and b
    # A separate writer changes job two while both workers are running.
    editor = CronService(tmp_path / "jobs.json")
    editor._running = True
    edited = editor.update_job(two.id, name="edited")
    assert edited != "not_found"
    a[0].state.last_status = "ok"
    b[0].state.last_status = "ok"
    assert service._finalize_claim(a[0], now, a[1])
    assert service._finalize_claim(b[0], now, b[1])
    check = CronService(tmp_path / "jobs.json")
    assert check.get_job(one.id).state.last_status == "ok"
    assert check.get_job(two.id).state.last_status == "ok"
    assert check.get_job(two.id).name == "edited"


def test_claim_store_corruption_and_invalid_shape_fail_closed(tmp_path):
    service = _service(tmp_path)
    service._claims_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(RuntimeError, match="claims store is corrupt"):
        service._read_claims()

    service._claims_path.write_text("[]", encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid shape"):
        service._read_claims()


def test_claiming_prunes_expired_occurrences(tmp_path, monkeypatch):
    now = 1_000
    monkeypatch.setattr("nanobot.cron.service._now_ms", lambda: now)
    service = _service(tmp_path)
    job = service.add_job("x", CronSchedule(kind="every", every_ms=100), "x")
    _due(service, job.id, now)
    service._claims_path.write_text(
        json.dumps({"old:1": {"token": "old", "lease_expires_at_ms": now - 1}}),
        encoding="utf-8",
    )

    claimed = service._claim_occurrence(job.id, now)

    assert claimed is not None
    claims = service._read_claims()
    assert "old:1" not in claims
    assert list(claims) == [f"{job.id}:{now}"]


def test_claim_renewal_extends_the_current_token_only(tmp_path, monkeypatch):
    now = 1_000
    monkeypatch.setattr("nanobot.cron.service._now_ms", lambda: now)
    service = _service(tmp_path)
    job = service.add_job("x", CronSchedule(kind="every", every_ms=100), "x")
    _due(service, job.id, now)
    claimed = service._claim_occurrence(job.id, now)
    assert claimed is not None
    token = claimed[1]
    key = f"{job.id}:{now}"
    first_expiry = service._read_claims()[key]["lease_expires_at_ms"]

    now = 2_000
    assert service._renew_claim(job.id, 1_000, token)
    assert service._read_claims()[key]["lease_expires_at_ms"] > first_expiry
    assert not service._renew_claim(job.id, 1_000, "stale-token")


def test_recurring_delete_after_run_is_not_deleted_by_finalizer(tmp_path, monkeypatch):
    now = 1_000
    monkeypatch.setattr("nanobot.cron.service._now_ms", lambda: now)
    service = _service(tmp_path)
    job = service.add_job(
        "recurring",
        CronSchedule(kind="every", every_ms=100),
        "x",
        delete_after_run=True,
    )
    _due(service, job.id, now)
    claimed = service._claim_occurrence(job.id, now)
    assert claimed is not None
    claimed_job, token = claimed
    claimed_job.state.last_status = "ok"
    claimed_job.state.next_run_at_ms = 1_100

    assert service._finalize_claim(claimed_job, now, token)
    assert CronService(service.store_path).get_job(job.id) is not None


def test_stale_finalizer_does_not_consume_pending_actions(tmp_path, monkeypatch):
    now = 1_000
    monkeypatch.setattr("nanobot.cron.service._now_ms", lambda: now)
    first = _service(tmp_path)
    job = first.add_job("x", CronSchedule(kind="every", every_ms=100), "x")
    _due(first, job.id, now)
    old = first._claim_occurrence(job.id, now)
    assert old is not None

    now = 400_001
    second = _service(tmp_path)
    new = second._claim_occurrence(job.id, 1_000)
    assert new is not None
    editor = _service(tmp_path)
    assert editor.update_job(job.id, name="queued edit") != "not_found"
    action_before = editor._action_path.read_text(encoding="utf-8")

    assert not first._finalize_claim(old[0], 1_000, old[1])
    assert editor._action_path.read_text(encoding="utf-8") == action_before
