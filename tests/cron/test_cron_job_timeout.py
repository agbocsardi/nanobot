"""Per-job ``timeout_s``: one job may override the service-wide execution cap.

The default guardrail (``CronService.job_timeout_s``, 300s in production) stays
in force for every job that does not ask for more. A job that sets
``timeout_s`` gets that cap instead - both when it is longer (a workshop slot
that legitimately runs for minutes) and when it is shorter.
"""

import asyncio

import pytest

from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.cron import CronTool
from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule


def _due(service, job_id, now=1000):
    service._running = True
    store = service._load_store()
    job = next(j for j in store.jobs if j.id == job_id)
    job.state.next_run_at_ms = now
    service._save_store()
    return job


def _reload(service, job_id):
    return CronService(service.store_path).get_job(job_id)


@pytest.mark.asyncio
async def test_job_timeout_override_allows_a_longer_run(tmp_path, monkeypatch):
    """A job that asks for more time survives past the service default."""
    monkeypatch.setattr("nanobot.cron.service._now_ms", lambda: 1_000)
    ran = []

    async def slow(_job):
        await asyncio.sleep(0.05)
        ran.append(True)

    service = CronService(tmp_path / "jobs.json", on_job=slow, job_timeout_s=0.01)
    job = service.add_job(
        "lab", CronSchedule(kind="every", every_ms=100), "x", timeout_s=1.0
    )
    await service._execute_due_job(_due(service, job.id, 1_000), 1_000)

    stored = _reload(service, job.id)
    assert ran == [True]
    assert stored is not None
    assert stored.state.last_status == "ok"
    assert stored.state.last_error is None


@pytest.mark.asyncio
async def test_job_timeout_override_can_be_shorter_than_the_default(tmp_path, monkeypatch):
    monkeypatch.setattr("nanobot.cron.service._now_ms", lambda: 1_000)

    async def blocked(_job):
        await asyncio.Event().wait()

    service = CronService(tmp_path / "jobs.json", on_job=blocked, job_timeout_s=30)
    job = service.add_job(
        "tight", CronSchedule(kind="every", every_ms=100), "x", timeout_s=0.01
    )
    await service._execute_due_job(_due(service, job.id, 1_000), 1_000)

    stored = _reload(service, job.id)
    assert stored is not None
    assert stored.state.last_status == "error"
    assert stored.state.last_error == "job timed out after 0.01s"


@pytest.mark.asyncio
async def test_job_without_timeout_keeps_the_service_guardrail(tmp_path, monkeypatch):
    monkeypatch.setattr("nanobot.cron.service._now_ms", lambda: 1_000)

    async def blocked(_job):
        await asyncio.Event().wait()

    service = CronService(tmp_path / "jobs.json", on_job=blocked, job_timeout_s=0.01)
    job = service.add_job("plain", CronSchedule(kind="every", every_ms=100), "x")
    assert service.effective_timeout_s(job) == 0.01
    await service._execute_due_job(_due(service, job.id, 1_000), 1_000)

    stored = _reload(service, job.id)
    assert stored is not None
    assert stored.state.last_status == "error"
    assert stored.state.last_error == "job timed out after 0.01s"


def test_job_timeout_survives_a_store_round_trip(tmp_path):
    service = CronService(tmp_path / "jobs.json")
    job = service.add_job(
        "lab", CronSchedule(kind="every", every_ms=100), "x", timeout_s=900
    )
    stored = _reload(service, job.id)
    assert stored is not None
    assert stored.timeout_s == 900.0
    assert CronService(service.store_path).effective_timeout_s(stored) == 900.0


def test_job_without_a_timeout_loads_as_none(tmp_path):
    service = CronService(tmp_path / "jobs.json")
    job = service.add_job("plain", CronSchedule(kind="every", every_ms=100), "x")
    stored = _reload(service, job.id)
    assert stored is not None
    assert stored.timeout_s is None


def test_effective_timeout_falls_back_to_the_service_default(tmp_path):
    service = CronService(tmp_path / "jobs.json", job_timeout_s=45)
    job = service.add_job("plain", CronSchedule(kind="every", every_ms=100), "x")
    assert service.effective_timeout_s(job) == 45


@pytest.mark.parametrize(
    "bad", [0, -1, -0.5, 3600.1, float("inf"), float("nan"), "900", True]
)
def test_add_job_rejects_an_invalid_timeout(tmp_path, bad):
    service = CronService(tmp_path / "jobs.json")
    with pytest.raises(ValueError):
        service.add_job(
            "bad", CronSchedule(kind="every", every_ms=100), "x", timeout_s=bad
        )
    assert service.list_jobs(include_disabled=True) == []


def _tool(tmp_path):
    service = CronService(tmp_path / "cron" / "jobs.json")
    tool = CronTool(service)
    tool.set_context(
        RequestContext(
            channel="websocket", chat_id="chat-1", session_key="websocket:chat-1"
        )
    )
    return tool, service


def test_tool_add_passes_timeout_s_through(tmp_path):
    tool, service = _tool(tmp_path)
    out = asyncio.run(
        tool.execute(action="add", message="lab", every_seconds=3600, timeout_s=900)
    )
    assert "Created job" in out
    job = service.list_jobs(include_disabled=True)[0]
    assert job.timeout_s == 900.0


def test_tool_add_rejects_an_invalid_timeout(tmp_path):
    tool, service = _tool(tmp_path)
    out = asyncio.run(
        tool.execute(action="add", message="lab", every_seconds=3600, timeout_s=99999)
    )
    assert out.startswith("Error:")
    assert service.list_jobs(include_disabled=True) == []


def test_tool_list_shows_the_timeout(tmp_path):
    tool, _service = _tool(tmp_path)
    asyncio.run(
        tool.execute(action="add", message="lab", every_seconds=3600, timeout_s=900)
    )
    out = asyncio.run(tool.execute(action="list"))
    assert "900s per run" in out
