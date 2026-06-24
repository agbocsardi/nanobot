"""Cron run records carry kind + model/provider + usage (Phase 3 observability).

CronService.write_run_record delegates to the shared writer; this test pins
the on-disk shape so 'which model ran this cron + what did it cost' is
answerable from runs/*.json alone.
"""

from __future__ import annotations

import json

from nanobot.cron.service import CronService


def test_cron_run_record_carries_kind_usage_and_model(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    service.write_run_record(
        "cron-1:1700000000:ab",
        {
            "kind": "cron",
            "job_id": "abc12345",
            "job_name": "news-sweep",
            "session_key": "telegram:111",
            "status": "ok",
            "response": "nothing notable",
            "silent": True,
            "usage": {
                "provider": "opencode-go",
                "model": "deepseek-v4-pro",
                "prompt_tokens": 1200,
                "completion_tokens": 300,
                "total_tokens": 1500,
            },
        },
    )

    records = list((tmp_path / "cron" / "runs").glob("*.json"))
    assert len(records) == 1
    data = json.loads(records[0].read_text(encoding="utf-8"))

    assert data["kind"] == "cron"
    assert data["job_name"] == "news-sweep"
    assert data["silent"] is True
    assert data["usage"]["model"] == "deepseek-v4-pro"
    assert data["usage"]["provider"] == "opencode-go"
    assert data["usage"]["total_tokens"] == 1500
    # writer stamps identity + timestamps
    assert data["run_id"] == "cron-1:1700000000:ab"
    assert data["created_at_ms"] > 0


def test_cron_run_record_uses_safe_filename(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    service.write_run_record("../escape", {"kind": "cron", "status": "ok"})

    # No traversal: path separators sanitized, lands inside runs/.
    escaped = tmp_path / "cron" / "runs" / ".." / "escape.json"
    assert not escaped.exists()
    assert (tmp_path / "cron" / "runs").glob("*.json")
