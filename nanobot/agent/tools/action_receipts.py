"""Durable action receipts for at-least-once side effects (issue #31).

Execution identity is ``<exec_id>`` (a stable ``run/tool-call`` id). Only
tools annotated with ``effect != "read"`` participate in replay suppression.
Receipt states: planned -> started -> succeeded | failed; a crash after
dispatch leaves ``started`` which resolves lazily to ``unknown`` (never
auto-repeated unless the tool declares ``replay = idempotency_key`` or a
verified reconciliation strategy exists).

This is evidence of execution state, NOT postcondition verification:
a receipt does not prove the external world changed correctly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Retention: keep at most this many receipts per owner session.
RETAIN_PER_OWNER = 200
# A started receipt older than this is treated as unknown (crash after dispatch).
UNKNOWN_AFTER_S = 3600

_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|token|password|secret|credential|private[_-]?key)"
)


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


def canonical_arg_hash(arguments: Any) -> str:
    """Stable hash over canonicalized tool arguments."""
    payload = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def redact_preview(value: Any, *, max_chars: int = 240) -> str:
    """Redact secret-like substrings and bound sizes for the ledger."""
    try:
        text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    text = _SECRET_RE.sub("***", text)
    text = " ".join(text.split())
    return text[:max_chars] + ("..." if len(text) > max_chars else "")


@dataclass
class ActionReceipt:
    exec_id: str
    session_key: str
    sender_id: str
    tool: str
    arg_hash: str
    effect: str
    replay: str
    status: str = "planned"  # planned|started|succeeded|failed|unknown
    attempts: int = 0
    created_at_ms: int = 0
    updated_at_ms: int = 0
    outcome: str = ""  # redacted, bounded terminal outcome

    def _touch(self, now_ms: int | None = None) -> None:
        self.updated_at_ms = now_ms if now_ms is not None else _now_ms()


class ActionReceiptStore:
    """Durable audit receipt ledger under ``workspace/action_receipts.json``."""

    def __init__(self, workspace: Path | None = None):
        self._path = (workspace or Path(".")) / "action_receipts.json"
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> list[ActionReceipt]:
        if not self._path.exists():
            return []
        data = json.loads(self._path.read_text(encoding="utf-8"))
        items = data.get("receipts", []) if isinstance(data, dict) else data
        return [ActionReceipt(**item) for item in items if isinstance(item, dict)]

    def _load_safe(self) -> list[ActionReceipt]:
        try:
            return self._load()
        except (OSError, json.JSONDecodeError, TypeError):
            backup = self._path.with_suffix(self._path.suffix + f".corrupt-{int(time.time())}")
            with suppress(OSError):
                self._path.rename(backup)
            return []

    def _write(self, receipts: list[ActionReceipt]) -> None:
        _atomic_write(
            self._path,
            json.dumps({"version": 1, "receipts": [asdict(r) for r in receipts]},
                       indent=2, ensure_ascii=False),
        )

    def _prune(self, receipts: list[ActionReceipt], owner: str) -> list[ActionReceipt]:
        count = sum(1 for r in receipts if r.session_key == owner)
        if count <= RETAIN_PER_OWNER:
            return receipts
        overflow = [r for r in receipts if r.session_key == owner and r.status != "started"]
        overflow.sort(key=lambda r: r.updated_at_ms)
        drop_count = count - RETAIN_PER_OWNER
        dropped = set(id(r) for r in overflow[:drop_count])
        return [r for r in receipts if id(r) not in dropped]

    def begin(
        self,
        *,
        exec_id: str,
        session_key: str,
        sender_id: str,
        tool: str,
        arg_hash: str,
        effect: str,
        replay: str,
        now_ms: int | None = None,
    ) -> tuple[str, ActionReceipt | None]:
        """Register (or reconcile) an execution intent.

        Returns ``(decision, receipt)`` where decision is one of:
        ``new`` (proceed and dispatch), ``already_succeeded`` (duplicate of a
        confirmed success), ``mismatch`` (id reused with different args),
        ``in_flight`` (another attempt is live), ``unknown`` (crash after
        dispatch; do not auto-repeat unless replay allows it), or ``retryable``
        (previous attempt failed before confirmation; safe to retry).
        """
        now = now_ms if now_ms is not None else _now_ms()
        with self._lock:
            receipts = self._load_safe()
            existing = next((r for r in receipts if r.exec_id == exec_id), None)
            if existing is None:
                receipt = ActionReceipt(
                    exec_id=exec_id, session_key=session_key, sender_id=sender_id,
                    tool=tool, arg_hash=arg_hash, effect=effect, replay=replay,
                    status="started", attempts=1, created_at_ms=now,
                )
                receipt._touch(now)
                receipts.append(receipt)
                self._write(self._prune(receipts, session_key))
                return "new", receipt

            if existing.arg_hash != arg_hash or existing.tool != tool:
                existing._touch(now)
                self._write(receipts)
                return "mismatch", existing

            if existing.status == "succeeded":
                existing._touch(now)
                self._write(receipts)
                return "already_succeeded", existing

            if existing.status == "started":
                age_s = (now - existing.updated_at_ms) / 1000
                if age_s <= UNKNOWN_AFTER_S:
                    # A live attempt exists; never dispatch a concurrent twin.
                    return "in_flight", existing
                # Stale started = crash after dispatch but before confirmation.
                existing.status = "unknown"
                existing._touch(now)
                self._write(receipts)
                if existing.replay == "idempotency_key":
                    # The external effect is safely repeatable via its key.
                    existing.status = "started"
                    existing.attempts += 1
                    existing._touch(now)
                    self._write(receipts)
                    return "retryable", existing
                return "unknown", existing

            if existing.status == "unknown":
                return "unknown", existing

            # failed or planned -> safe retry.
            existing.status = "started"
            existing.attempts += 1
            existing._touch(now)
            self._write(receipts)
            return "retryable", existing

    def complete(
        self,
        exec_id: str,
        *,
        status: str,
        outcome: str = "",
        ok: bool = True,
    ) -> bool:
        """Persist a terminal outcome. ``ok=False`` records failed."""
        with self._lock:
            receipts = self._load_safe()
            receipt = next((r for r in receipts if r.exec_id == exec_id), None)
            if receipt is None:
                return False
            if ok:
                receipt.status = "succeeded"
            else:
                receipt.status = "failed"
            receipt.outcome = (outcome or "")[:4000]
            receipt._touch()
            self._write(receipts)
            return True

    def get(self, exec_id: str) -> ActionReceipt | None:
        with self._lock:
            return next((r for r in self._load_safe() if r.exec_id == exec_id), None)

    def list_for_owner(self, session_key: str, *, sender_id: str | None = None) -> list[ActionReceipt]:
        with self._lock:
            receipts = self._load_safe()
        return [
            r for r in receipts
            if r.session_key == session_key
            and (sender_id is None or r.sender_id == sender_id)
        ][-RETAIN_PER_OWNER:]
