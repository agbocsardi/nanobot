"""Self-check for the shared background-run record writer.

Covers: safe naming (no path traversal), atomic write, timestamp fields,
and the usage-block builder (normalize + provider/model attachment).
"""

from __future__ import annotations

import json
from pathlib import Path

from nanobot.utils.run_records import (
    build_usage_block,
    safe_record_name,
    write_run_record,
)


def test_safe_record_name_blocks_traversal() -> None:
    assert safe_record_name("../etc/passwd") == "etc_passwd"
    assert safe_record_name("a/b\\c") == "a_b_c"
    assert safe_record_name("  ") == "run"  # empty after strip → fallback


def test_write_run_record_is_atomic_and_json(tmp_path: Path) -> None:
    records_dir = tmp_path / "runs"
    path = write_run_record(
        records_dir,
        "job-1:1700000000:ab",
        {"kind": "cron", "job_name": "daily", "status": "ok"},
    )
    assert path == records_dir / "job-1_1700000000_ab.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["run_id"] == "job-1:1700000000:ab"
    assert data["kind"] == "cron"
    assert data["status"] == "ok"
    # writer stamps created/updated timestamps; caller value wins for created
    assert data["created_at_ms"] > 0
    assert data["updated_at_ms"] >= data["created_at_ms"]


def test_write_run_record_idempotent_overwrite(tmp_path: Path) -> None:
    records_dir = tmp_path / "runs"
    write_run_record(records_dir, "rid", {"status": "queued"})
    path = write_run_record(records_dir, "rid", {"status": "ok"})
    # Same filename, latest content wins, no stray files.
    assert sorted(p.name for p in records_dir.glob("*.json")) == ["rid.json"]
    assert json.loads(path.read_text())["status"] == "ok"


def test_build_usage_block_attaches_model_and_provider() -> None:
    block = build_usage_block(
        {"prompt_tokens": 100, "completion_tokens": 40},
        provider="opencode-go",
        model="deepseek-v4-pro",
    )
    assert block["provider"] == "opencode-go"
    assert block["model"] == "deepseek-v4-pro"
    assert block["prompt_tokens"] == 100
    assert block["completion_tokens"] == 40
    assert block["total_tokens"] == 140  # derived when absent


def test_build_usage_block_handles_empty_and_garbage() -> None:
    # Empty / None usage still yields a block identifying the model that ran.
    empty = build_usage_block(None, provider="p", model="m")
    assert empty == {"provider": "p", "model": "m"}
    # Non-numeric usage is dropped, not crashed.
    junk = build_usage_block({"prompt_tokens": "oops"}, provider="p", model="m")
    assert junk == {"provider": "p", "model": "m"}


if __name__ == "__main__":
    # ponytail: runnable self-check without pytest
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        test_safe_record_name_blocks_traversal()
        test_write_run_record_is_atomic_and_json(Path(d))
        test_write_run_record_idempotent_overwrite(Path(d))
        test_build_usage_block_attaches_model_and_provider()
        test_build_usage_block_handles_empty_and_garbage()
    print("run_records self-check OK")
