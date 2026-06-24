"""Append-only run records for background execution (cron + subagents).

Each record is one JSON file under a per-kind directory, named by run id.
Shared by cron (runs/) and subagents (subagents/) so both background
execution paths leave the same observability trail:

  - which model/provider actually ran   (matters once Phase 2 isolates
    background models via run_with_preset; the record is what tells you
    which preset a cron/subagent used)
  - token usage for the run             (previously lost for subagents;
    co-mingled with chat for crons)
  - prompt, params, status, timestamps

Source of truth stays the JSON files; nothing is re-derived. This module
is deliberately tiny — just safe-naming + atomic write + a usage builder.

ponytail: no DB, no index, no summary layer. JSON files + grep later.
Add a rollup command only when a rollup is actually wanted.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nanobot.utils.usage import normalize_usage

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def safe_record_name(name: str) -> str:
    """Sanitize a run id into a filesystem-safe record name (no path traversal)."""
    cleaned = _SAFE_NAME_RE.sub("_", name).strip("._-")
    return cleaned or "run"


def _utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* atomically with fsync (tmp + os.replace).

    Mirrors the pattern in nanobot.session.manager and nanobot.cron.service
    so a crash mid-write cannot leave a truncated record.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        # Best-effort cleanup of the temp file; never raises.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def write_run_record(
    records_dir: Path,
    run_id: str,
    record: dict[str, Any],
) -> Path:
    """Write one timestamped run record and return its path.

    ``record`` is merged with run_id + created/updated timestamps. Caller
    supplies the domain fields (kind, status, usage, model, prompt, ...).
    """
    now = _utc_now_ms()
    payload = {
        "run_id": run_id,
        "created_at_ms": record.get("created_at_ms") or now,
        "updated_at_ms": now,
        **record,
    }
    name = safe_record_name(run_id)
    path = records_dir / f"{name}.json"
    _atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=False))
    return path


def build_usage_block(
    raw_usage: dict[str, Any] | None,
    *,
    provider: str | None,
    model: str | None,
) -> dict[str, Any]:
    """Normalize a run's raw usage into a record-ready block.

    Always returns a dict (possibly empty) with provider/model attached so
    the record makes the model-that-ran obvious — the whole point of the
    ledger once background model isolation lands.
    """
    usage = normalize_usage(raw_usage)
    block: dict[str, Any] = {
        "provider": provider or "unknown",
        "model": model or "unknown",
    }
    if usage:
        block.update(usage)
    return block
