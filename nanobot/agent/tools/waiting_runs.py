"""Durable wait-and-resume run state (issue #29).

When an agent workflow reaches an ``ask_user`` decision boundary, it stops
consuming runtime and a :class:`WaitingRun` is persisted. State machine:

    running -> waiting -> resuming -> completed
              waiting -> cancelled | expired
              resuming -> failed | cancelled

The envelope deliberately stores only what a NEW bounded agent turn needs:
owner identity, the linked question id, a short continuation note, completed
tool references, and the original bounded model budget. The session's own
history (persisted before the wait) is the source of truth for "what is done
and what must not be repeated" — resume does not replay Python control flow.

Resume is claimed atomically (``waiting -> resuming``) with an in-process
source-key dedup window, so duplicate answers or duplicate inbound deliveries
can never start a second continuation. Terminal records are retained on disk
(evidence), filtered out of claim/list.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_DEDUP_WINDOW = 512


def _now_ms() -> int:
    return int(time.time() * 1000)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        with suppress(PermissionError):
            fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def resume_source_digest(*parts: Any) -> str:
    raw = "|".join(str(p) for p in parts if p is not None)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


@dataclass
class WaitingRun:
    run_id: str
    question_id: str
    sender_id: str
    channel: str
    chat_id: str
    session_key: str
    created_at_ms: int
    expires_at_ms: int
    status: str = "waiting"  # waiting | resuming | completed | cancelled | expired | failed
    note: str = ""
    budgets: dict[str, Any] = None
    completed_tool_names: list[str] = None
    resumed_at_ms: int | None = None
    finished_at_ms: int | None = None

    def __post_init__(self) -> None:
        if self.budgets is None:
            self.budgets = {}
        if self.completed_tool_names is None:
            self.completed_tool_names = []

    def expired(self, now_ms: int | None = None) -> bool:
        return (now_ms if now_ms is not None else _now_ms()) > self.expires_at_ms


class WaitingRunStore:
    """Durable waiting-run state under ``workspace/waiting_runs.json``."""

    def __init__(self, workspace: Path | None = None):
        self._path = (workspace or Path(".")) / "waiting_runs.json"
        self._lock = threading.Lock()
        self._dedup: set[str] = set()
        self._dedup_order: list[str] = []

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> list[WaitingRun]:
        if not self._path.exists():
            return []
        data = json.loads(self._path.read_text(encoding="utf-8"))
        items = data.get("runs", []) if isinstance(data, dict) else data
        return [WaitingRun(**item) for item in items if isinstance(item, dict)]

    def _load_safe(self) -> list[WaitingRun]:
        try:
            return self._load()
        except (OSError, json.JSONDecodeError, TypeError):
            backup = self._path.with_suffix(self._path.suffix + f".corrupt-{int(time.time())}")
            with suppress(OSError):
                self._path.rename(backup)
            return []

    def _write(self, runs: list[WaitingRun]) -> None:
        _atomic_write(
            self._path,
            json.dumps({"version": 1, "runs": [asdict(r) for r in runs]},
                       indent=2, ensure_ascii=False),
        )

    def create(
        self,
        *,
        question_id: str,
        sender_id: str,
        channel: str,
        chat_id: str,
        session_key: str,
        expires_at_ms: int,
    ) -> WaitingRun:
        now = _now_ms()
        run = WaitingRun(
            run_id=question_id,  # 1:1 linkage; single resume per question
            question_id=question_id,
            sender_id=sender_id,
            channel=channel,
            chat_id=str(chat_id),
            session_key=session_key,
            created_at_ms=now,
            expires_at_ms=expires_at_ms,
        )
        with self._lock:
            runs = self._load_safe()
            runs = [r for r in runs if r.run_id != question_id]  # idempotent create
            runs.append(run)
            self._write(runs)
        return run

    def augment(self, run_id: str, *, note: str = "", budgets: dict[str, Any] | None = None,
                completed_tool_names: list[str] | None = None) -> bool:
        """Fill the continuation envelope only while the run is still waiting."""
        with self._lock:
            runs = self._load_safe()
            run = next((r for r in runs if r.run_id == run_id), None)
            if run is None or run.status != "waiting":
                return False
            if note:
                run.note = note[:2000]
            if budgets:
                run.budgets = dict(budgets)
            if completed_tool_names:
                run.completed_tool_names = completed_tool_names[:50]
            self._write(runs)
            return True

    def claim_resume(
        self,
        run_id: str,
        *,
        session_key: str,
        sender_id: str,
        source_key: str,
        now_ms: int | None = None,
    ) -> tuple[bool, WaitingRun | None]:
        """Atomically claim a waiting run for resume.

        Returns ``(claimed, run)``. Claims only succeed once (waiting ->
        resuming); duplicate source keys within the dedup window are ignored.
        """
        now = now_ms if now_ms is not None else _now_ms()
        with self._lock:
            if source_key in self._dedup:
                return False, None
            self._dedup.add(source_key)
            self._dedup_order.append(source_key)
            while len(self._dedup_order) > _DEDUP_WINDOW:
                self._dedup.discard(self._dedup_order.pop(0))

            runs = self._load_safe()
            run = next((r for r in runs if r.run_id == run_id), None)
            if run is None:
                return False, None
            if run.session_key != session_key or run.sender_id != sender_id:
                return False, run
            if run.status == "expired":
                return False, run
            if run.status == "cancelled":
                return False, run
            if run.status != "waiting":
                return False, run
            if run.expired(now):
                run.status = "expired"
                run.finished_at_ms = now
                self._write(runs)
                return False, run
            run.status = "resuming"
            run.resumed_at_ms = now
            self._write(runs)
            return True, run

    def mark_complete(self, run_id: str, *, ok: bool = True) -> bool:
        with self._lock:
            runs = self._load_safe()
            run = next((r for r in runs if r.run_id == run_id), None)
            if run is None or run.status != "resuming":
                return False
            run.status = "completed" if ok else "failed"
            run.finished_at_ms = _now_ms()
            self._write(runs)
            return True

    def cancel(self, run_id: str, *, session_key: str) -> bool:
        with self._lock:
            runs = self._load_safe()
            run = next((r for r in runs if r.run_id == run_id), None)
            if run is None or run.session_key != session_key or run.status != "waiting":
                return False
            run.status = "cancelled"
            run.finished_at_ms = _now_ms()
            self._write(runs)
            return True

    def get(self, run_id: str) -> WaitingRun | None:
        with self._lock:
            return next((r for r in self._load_safe() if r.run_id == run_id), None)

    def list_for_owner(self, session_key: str, *, sender_id: str | None = None) -> list[WaitingRun]:
        with self._lock:
            runs = self._load_safe()
        return [
            r for r in runs
            if r.session_key == session_key
            and (sender_id is None or r.sender_id == sender_id)
        ]
