"""Transactional mutations: every cron write goes through jobs.json.

Stopped-service mutations previously queued into ``action.jsonl`` and were
merged on load; a crash or a second service could lose them. Today every
mutation (add/update/remove/enable/disable/register_system_job) reloads the
fresh ``jobs.json`` under the inter-process FileLock, applies the change, and
atomically writes it back. These tests pin that contract: concurrent services
never lose each other's writes, a crash window cannot lose an update, and a
leftover legacy action log is ignored and removed.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule


def test_stopped_service_add_update_remove_all_land_in_jobs_json(tmp_path) -> None:
    path = tmp_path / "cron" / "jobs.json"
    service = CronService(path)
    assert not path.exists()

    job = service.add_job(
        name="alpha",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="x",
    )
    service.update_job(job.id, name="beta")
    service.enable_job(job.id, enabled=False)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert [j["name"] for j in data["jobs"]] == ["beta"]
    assert data["jobs"][0]["enabled"] is False

    assert service.remove_job(job.id) == "removed"
    assert json.loads(path.read_text(encoding="utf-8"))["jobs"] == []
    assert not (tmp_path / "cron" / "action.jsonl").exists()


def test_concurrent_mutations_across_instances_all_preserved(tmp_path) -> None:
    """Interleaved add/update/remove from distinct CronService instances.

    Each mutation must start from the last committed jobs.json, so no instance
    can clobber another's committed write regardless of order.
    """
    path = tmp_path / "cron" / "jobs.json"
    services = [CronService(path) for _ in range(4)]
    jobs = [services[i].add_job(f"job-{i}", CronSchedule(kind="every", every_ms=60_000), "x")
            for i in range(4)]

    # Every instance updates the next instance's job.
    for i in range(4):
        updated = services[i].update_job(jobs[(i + 1) % 4].id, name=f"renamed-{i}")
        assert updated is not None and updated.name == f"renamed-{i}"

    check = CronService(path)
    assert {j.name for j in check.list_jobs(include_disabled=True)} == {
        "renamed-0", "renamed-1", "renamed-2", "renamed-3",
    }

    # Distinct deletes through two different instances.
    assert services[0].remove_job(jobs[0].id) == "removed"
    assert services[2].remove_job(jobs[2].id) == "removed"
    remaining = CronService(path).list_jobs(include_disabled=True)
    # Job i was renamed by the instance (i - 1) % 4: job 1 -> renamed-0,
    # job 3 -> renamed-2; jobs 0 and 2 are gone.
    assert {j.id for j in remaining} == {jobs[1].id, jobs[3].id}
    assert {j.name for j in remaining} == {"renamed-0", "renamed-2"}


def test_threaded_concurrent_mutations_across_instances_all_preserved(tmp_path) -> None:
    """Genuinely concurrent mutations (barrier + threads) serialize on the
    FileLock and every mutation still lands."""
    path = tmp_path / "cron" / "jobs.json"
    n = 4
    services = [CronService(path) for _ in range(n)]
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []
    job_ids: list[str] = []

    def add_phase(i: int) -> None:
        try:
            barrier.wait(timeout=10)
            job = services[i].add_job(
                f"job-{i}", CronSchedule(kind="every", every_ms=60_000), "x"
            )
            job_ids.append(job.id)
        except BaseException as exc:  # pragma: no cover - failure diagnostics
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=n) as pool:
        for fut in [pool.submit(add_phase, i) for i in range(n)]:
            fut.result()
    assert not errors
    assert len(job_ids) == n
    assert {j.name for j in CronService(path).list_jobs(include_disabled=True)} == {
        f"job-{i}" for i in range(n)
    }

    def update_phase(i: int) -> None:
        try:
            barrier.wait(timeout=10)
            updated = services[i].update_job(job_ids[(i + 1) % n], name=f"renamed-{i}")
            assert updated is not None, f"thread {i} could not find target job"
        except BaseException as exc:  # pragma: no cover - failure diagnostics
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=n) as pool:
        for fut in [pool.submit(update_phase, i) for i in range(n)]:
            fut.result()
    assert not errors
    assert {j.name for j in CronService(path).list_jobs(include_disabled=True)} == {
        f"renamed-{i}" for i in range(n)
    }

    def delete_phase(i: int) -> None:
        try:
            barrier.wait(timeout=10)
            removed = services[i].remove_job(job_ids[(i + 2) % n])
            assert removed == "removed", f"thread {i} removal returned {removed!r}"
        except BaseException as exc:  # pragma: no cover - failure diagnostics
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=n) as pool:
        for fut in [pool.submit(delete_phase, i) for i in range(n)]:
            fut.result()
    assert not errors
    assert CronService(path).list_jobs(include_disabled=True) == []


def test_crash_window_no_lost_update_from_stale_snapshot(tmp_path) -> None:
    """A service that cached a store before another service committed a change
    must not resurrect stale jobs: the next mutation starts over from the last
    saved jobs.json instead of the cache."""
    path = tmp_path / "cron" / "jobs.json"
    first = CronService(path)
    job = first.add_job(
        name="stale", schedule=CronSchedule(kind="every", every_ms=60_000), message="x"
    )
    # Simulate a crash after load, before save: first caches the store...
    assert first._load_store() is not None

    # ...while another service commits a removal in the meantime.
    other = CronService(path)
    assert other.remove_job(job.id) == "removed"

    # The next mutation through the stale instance must not resurrect "stale".
    first.add_job(name="fresh", schedule=CronSchedule(kind="every", every_ms=60_000), message="y")
    names = {j.name for j in CronService(path).list_jobs(include_disabled=True)}
    assert names == {"fresh"}


def test_crash_window_leftover_tmp_file_does_not_lose_jobs(tmp_path) -> None:
    """A crashed concurrent writer leaves a partial .tmp behind; the next
    transaction still saves a clean jobs.json that includes both jobs."""
    path = tmp_path / "cron" / "jobs.json"
    service = CronService(path)
    service.add_job(
        name="survivor", schedule=CronSchedule(kind="every", every_ms=60_000), message="x"
    )
    # Simulate the crash: a partial temp write next to the committed file.
    (tmp_path / "cron" / "jobs.json.tmp").write_text("{partial", encoding="utf-8")

    other = CronService(path)
    other.add_job(
        name="new", schedule=CronSchedule(kind="every", every_ms=60_000), message="y"
    )

    names = {j.name for j in CronService(path).list_jobs(include_disabled=True)}
    assert names == {"survivor", "new"}
    assert list((tmp_path / "cron").glob("*.tmp")) == []


def test_leftover_action_jsonl_is_ignored_and_removed_on_load(tmp_path) -> None:
    """A legacy action.jsonl from a pre-transactional version must never be
    replayed (its actions are already folded into jobs.json) and is removed on
    load so it cannot be mistaken for a live log."""
    action_path = tmp_path / "cron" / "action.jsonl"
    action_path.parent.mkdir(parents=True, exist_ok=True)
    action_path.write_text(
        json.dumps(
            {
                "action": "add",
                "params": {
                    "id": "ghost",
                    "name": "ghost",
                    "enabled": True,
                    "schedule": {"kind": "every", "everyMs": 60_000},
                    "payload": {"kind": "agent_turn", "message": "x"},
                    "state": {},
                    "createdAtMs": 1,
                    "updatedAtMs": 1,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    service = CronService(tmp_path / "cron" / "jobs.json")
    store = service._load_store()
    assert store is not None
    # The leftover log contents are ignored, not merged into the store.
    assert [j.id for j in store.jobs] == []
    assert not action_path.exists()

    # The store remains fully writable afterwards.
    job = service.add_job(
        name="real", schedule=CronSchedule(kind="every", every_ms=60_000), message="x"
    )
    assert service.get_job(job.id) is not None
    assert not action_path.exists()
