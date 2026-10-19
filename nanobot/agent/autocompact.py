"""Schedule no-LLM idle microcompaction without changing stored conversation text."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from time import monotonic
from typing import TYPE_CHECKING, Callable, Coroutine

from loguru import logger

from nanobot.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from nanobot.agent.memory import Consolidator


class AutoCompact:
    _INTERNAL_SESSION_PREFIXES = ("dream:", "cron:")
    _START_INTERVAL_S = 60
    _MAX_BACKOFF_S = 3600

    def __init__(self, sessions: SessionManager, consolidator: Consolidator,
                 session_ttl_minutes: int = 0):
        self.sessions = sessions
        self.consolidator = consolidator
        self._ttl = session_ttl_minutes
        self._archiving: set[str] = set()
        self._summaries: dict[str, tuple[str, datetime]] = {}
        self._completed: dict[str, str] = {}
        self._failures: dict[str, tuple[int, float]] = {}
        self._next_start = 0.0

    def _is_expired(self, ts: datetime | str | None,
                    now: datetime | None = None) -> bool:
        if self._ttl <= 0 or not ts:
            return False
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        return ((now or datetime.now()) - ts).total_seconds() >= self._ttl * 60

    @staticmethod
    def _format_summary(text: str, last_active: datetime) -> str:
        return f"Previous conversation summary (last active {last_active.isoformat()}):\n{text}"

    @classmethod
    def _is_internal_session(cls, key: str) -> bool:
        return key.startswith(cls._INTERNAL_SESSION_PREFIXES)

    def check_expired(self, schedule_background: Callable[[Coroutine], None],
                      active_session_keys: Collection[str] = ()) -> None:
        """Start at most one idle compaction per minute, with no queued backlog."""
        clock = monotonic()
        if self._ttl <= 0 or self._archiving or clock < self._next_start:
            return
        now = datetime.now()
        for info in self.sessions.list_sessions():
            key = info.get("key", "")
            if not key or self._is_internal_session(key) or key in self._archiving:
                continue
            if key in active_session_keys:
                continue
            if self._completed.get(key) == info.get("updated_at"):
                continue
            if clock < self._failures.get(key, (0, 0.0))[1]:
                continue
            if self._is_expired(info.get("updated_at"), now):
                self._archiving.add(key)
                self._next_start = clock + self._START_INTERVAL_S
                work = self._archive(key)
                try:
                    schedule_background(work)
                except BaseException:
                    work.close()
                    self._archiving.discard(key)
                    raise
                return

    async def _archive(self, key: str) -> None:
        if self._is_internal_session(key):
            self._archiving.discard(key)
            return
        revision = None
        succeeded = False
        try:
            session = self.sessions.get_or_create(key)
            revision = session.updated_at.isoformat()
            await self.consolidator.microcompact_idle_session(key)
            succeeded = True
        except Exception:
            logger.exception("Auto-compact: failed for {}", key)
        finally:
            if succeeded:
                if revision is not None:
                    self._completed[key] = revision
                self._failures.pop(key, None)
            else:
                failures = min(self._failures.get(key, (0, 0.0))[0] + 1, 7)
                delay = min(self._START_INTERVAL_S * 2 ** (failures - 1), self._MAX_BACKOFF_S)
                self._failures[key] = (failures, monotonic() + delay)
                logger.warning("Auto-compact: retry for {} deferred by {}s", key, delay)
            self._archiving.discard(key)

    def prepare_session(self, session: Session, key: str) -> tuple[Session, str | None]:
        if self._is_internal_session(key):
            self._archiving.discard(key)
            self._summaries.pop(key, None)
            return session, None
        if key in self._archiving or self._is_expired(session.updated_at):
            logger.info("Auto-compact: reloading session {} (archiving={})", key, key in self._archiving)
            session = self.sessions.get_or_create(key)
        from nanobot.session.compaction import latest_checkpoint

        if latest_checkpoint(session.metadata):
            self._summaries.pop(key, None)
            return session, None  # get_history owns deterministic summary projection.
        # Hot path: summary from in-memory dict (process hasn't restarted).
        entry = self._summaries.pop(key, None)
        if entry:
            return session, self._format_summary(entry[0], entry[1])
        # Cold path: summary persisted in session metadata (process restarted).
        meta = session.metadata.get("_last_summary")
        if isinstance(meta, dict):
            return session, self._format_summary(meta["text"], datetime.fromisoformat(meta["last_active"]))
        return session, None
