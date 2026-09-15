"""CronTool action='add' must surface service-side schedule validation as tool
error text: an invalid schedule never produces a 'Created job' claim and never
persists a job. The service validates before persistence, so any ValueError
from add_job is a hard rejection of the whole request."""

import asyncio

from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.cron import CronTool
from nanobot.cron.service import CronService


def _tool(tmp_path) -> tuple[CronTool, CronService]:
    service = CronService(tmp_path / "cron" / "jobs.json")
    tool = CronTool(service)
    tool.set_context(
        RequestContext(
            channel="websocket", chat_id="chat-1", session_key="websocket:chat-1"
        )
    )
    return tool, service


def _add(tool: CronTool, **params) -> str:
    return asyncio.run(tool.execute(action="add", message="hello", **params))


def test_add_rejects_invalid_cron_expression(tmp_path):
    tool, service = _tool(tmp_path)
    out = _add(tool, cron_expr="definitely not cron")
    assert out.startswith("Error:")
    assert "Created job" not in out
    assert service.list_jobs(include_disabled=True) == []


def test_add_rejects_non_positive_interval(tmp_path):
    tool, service = _tool(tmp_path)
    out = _add(tool, every_seconds=-5)
    assert out.startswith("Error:")
    assert "Created job" not in out
    assert service.list_jobs(include_disabled=True) == []


def test_add_rejects_past_one_shot(tmp_path):
    tool, service = _tool(tmp_path)
    out = _add(tool, at="2001-02-03T04:05:06")
    assert out.startswith("Error:")
    assert "in the future" in out
    assert "Created job" not in out
    assert service.list_jobs(include_disabled=True) == []


def test_add_valid_schedule_still_creates_job(tmp_path):
    tool, service = _tool(tmp_path)
    out = _add(tool, every_seconds=3600)
    assert "Created job" in out
    jobs = service.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].state.next_run_at_ms is not None
