"""Coordination for scheduled cron turns."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable, Iterable

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.cron.session_turns import (
    cron_run_id,
    cron_trigger,
    defer_cron_until_session_idle,
)


class CronTurnCoordinator:
    """Manage scheduled cron turns without mixing them into live injections."""

    def __init__(
        self,
        *,
        publish_inbound: Callable[[InboundMessage], Awaitable[None]],
        dispatch: Callable[[InboundMessage], Awaitable[object]],
        is_running: Callable[[], bool],
    ) -> None:
        self._publish_inbound = publish_inbound
        self._dispatch = dispatch
        self._is_running = is_running
        self.deferred_queues: dict[str, list[InboundMessage]] = {}
        self._waiters: dict[str, asyncio.Future[OutboundMessage | None]] = {}
        self._pending_messages_by_run_id: dict[str, InboundMessage] = {}
        # Run ids whose submit waiter ended without a completion (cancelled
        # or timed out): any still-queued deferred copy must never execute.
        self._cancelled_run_ids: set[str] = set()

    async def submit(self, msg: InboundMessage) -> OutboundMessage | None:
        """Submit a scheduled cron turn and wait for its session response."""
        run_id = cron_run_id(msg.metadata)
        if not run_id:
            raise ValueError("cron turn metadata must include a run_id")
        if run_id in self._waiters:
            raise RuntimeError(f"cron run {run_id!r} is already pending")

        loop = asyncio.get_running_loop()
        future: asyncio.Future[OutboundMessage | None] = loop.create_future()
        self._waiters[run_id] = future
        self._pending_messages_by_run_id[run_id] = msg
        self._cancelled_run_ids.discard(run_id)
        try:
            if self._is_running():
                await self._publish_inbound(msg)
            else:
                await self._dispatch(msg)
            return await future
        except asyncio.CancelledError:
            # The awaiting cron job was cancelled or timed out (or the turn
            # itself failed with cancellation): any still-queued deferred copy
            # must never execute an orphaned run. Task.cancel() cancels the
            # future being awaited, so future.done() cannot distinguish this.
            self._cancelled_run_ids.add(run_id)
            logger.warning(
                "Cron run {} ended without completion; deferred turn invalidated",
                run_id,
            )
            raise
        finally:
            self._waiters.pop(run_id, None)
            self._pending_messages_by_run_id.pop(run_id, None)

    def is_cancelled(self, msg: InboundMessage) -> bool:
        """True when *msg*'s cron run lost its waiter without completing."""
        run_id = cron_run_id(msg.metadata)
        return bool(run_id) and run_id in self._cancelled_run_ids

    def should_defer(
        self,
        msg: InboundMessage,
        *,
        session_key: str,
        active_session_keys: Iterable[str],
    ) -> bool:
        return (
            defer_cron_until_session_idle(msg.metadata)
            and session_key in active_session_keys
        )

    def defer_if_active(
        self,
        msg: InboundMessage,
        *,
        session_key: str,
        active_session_keys: Iterable[str],
    ) -> bool:
        """Defer a cron turn when its target session is already active."""
        if not self.should_defer(
            msg,
            session_key=session_key,
            active_session_keys=active_session_keys,
        ):
            return False
        pending_msg = msg
        if session_key != msg.session_key:
            pending_msg = dataclasses.replace(
                msg,
                session_key_override=session_key,
            )
        self.defer(session_key, pending_msg)
        return True

    def complete(
        self,
        msg: InboundMessage,
        *,
        response: OutboundMessage | None = None,
        error: BaseException | None = None,
    ) -> None:
        run_id = cron_run_id(msg.metadata)
        if not run_id:
            return
        future = self._waiters.get(run_id)
        if future is None or future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(response)

    def defer(self, session_key: str, msg: InboundMessage) -> None:
        self.deferred_queues.setdefault(session_key, []).append(msg)

    def pending_job_ids_for_session(self, session_key: str) -> set[str]:
        """Return cron jobs that are waiting for or running in *session_key*.

        Deferred-but-invalidated copies still count until the next idle drain
        actually drops them, so a job never looks idle while its turn sits in
        the queue.
        """
        job_ids: set[str] = set()
        for msg in self.deferred_queues.get(session_key, []):
            job_id = _cron_job_id(msg)
            if job_id:
                job_ids.add(job_id)
        for msg in self._pending_messages_by_run_id.values():
            if msg.session_key != session_key:
                continue
            job_id = _cron_job_id(msg)
            if job_id:
                job_ids.add(job_id)
        return job_ids

    def _deferred_is_live(self, msg: InboundMessage) -> bool:
        """A deferred turn is publishable unless its run was invalidated.

        ``submit()`` adds the run id to the cancelled set when its waiter ends
        without a completion; that is the only authoritative dead signal. A
        run id the coordinator never saw stays publishable.
        """
        run_id = cron_run_id(msg.metadata)
        return not run_id or run_id not in self._cancelled_run_ids

    async def publish_next_deferred(self, session_key: str) -> None:
        queue = self.deferred_queues.get(session_key)
        if not queue:
            return
        publishable: InboundMessage | None = None
        while queue:
            candidate = queue.pop(0)
            if not queue:
                self.deferred_queues.pop(session_key, None)
            if self._deferred_is_live(candidate):
                publishable = candidate
                break
            logger.info(
                "Dropping deferred cron turn for ended run {} in session {}",
                cron_run_id(candidate.metadata), session_key,
            )
            run_id = cron_run_id(candidate.metadata)
            if run_id:
                self._cancelled_run_ids.discard(run_id)
        if publishable is not None:
            await self._publish_inbound(publishable)


def _cron_job_id(msg: InboundMessage) -> str | None:
    trigger = cron_trigger(msg.metadata)
    if not trigger:
        return None
    value = trigger.get("job_id")
    return value if isinstance(value, str) and value else None
