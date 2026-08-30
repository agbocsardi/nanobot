"""Cron service for scheduling agent tasks."""

import asyncio
import copy
import json
import os
import time
import uuid
from contextlib import suppress
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine, Literal

from filelock import FileLock
from loguru import logger

from nanobot.cron.session_turns import is_bound_cron_job
from nanobot.cron.types import (
    CronJob,
    CronJobState,
    CronPayload,
    CronRunRecord,
    CronSchedule,
    CronStore,
)
from nanobot.utils.run_records import write_run_record


class CronJobSkippedError(Exception):
    """Raised by cron callbacks when a job was intentionally skipped."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _compute_next_run(schedule: CronSchedule, now_ms: int) -> int | None:
    """Compute next run time in ms."""
    if schedule.kind == "at":
        return schedule.at_ms if schedule.at_ms and schedule.at_ms > now_ms else None

    if schedule.kind == "every":
        if not schedule.every_ms or schedule.every_ms <= 0:
            return None
        # Next interval from now
        return now_ms + schedule.every_ms

    if schedule.kind == "cron" and schedule.expr:
        try:
            from zoneinfo import ZoneInfo

            from croniter import croniter
            # Use caller-provided reference time for deterministic scheduling
            base_time = now_ms / 1000
            tz = ZoneInfo(schedule.tz) if schedule.tz else datetime.now().astimezone().tzinfo
            base_dt = datetime.fromtimestamp(base_time, tz=tz)
            cron = croniter(schedule.expr, base_dt)
            next_dt = cron.get_next(datetime)
            return int(next_dt.timestamp() * 1000)
        except Exception:
            return None

    return None


def _validate_schedule_for_add(schedule: CronSchedule) -> None:
    """Validate schedule fields that would otherwise create non-runnable jobs."""
    if schedule.tz and schedule.kind != "cron":
        raise ValueError("tz can only be used with cron schedules")

    if schedule.kind == "cron" and schedule.tz:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(schedule.tz)
        except Exception:
            raise ValueError(f"unknown timezone '{schedule.tz}'") from None


def _has_legacy_delivery_context(payload: CronPayload) -> bool:
    return bool(payload.deliver or payload.channel or payload.to or payload.channel_meta)


def _legacy_session_key(payload: CronPayload) -> str | None:
    if payload.session_key:
        return payload.session_key
    if payload.channel and payload.to:
        return f"{payload.channel}:{payload.to}"
    return None


def _disable_malformed_legacy_job(job: CronJob) -> None:
    reason = "legacy cron payload is missing channel/to; recreate it from a chat session"
    job.payload.deliver = False
    job.payload.channel = None
    job.payload.to = None
    job.payload.channel_meta = {}
    job.enabled = False
    job.state.next_run_at_ms = None
    job.state.last_status = "error"
    job.state.last_error = reason
    logger.warning("Cron: disabled malformed legacy job '{}' ({}): {}", job.name, job.id, reason)


def _normalize_agent_turn_job(job: CronJob) -> bool:
    """Migrate legacy user cron payloads into session-bound payloads.

    Pre-bound user cron jobs stored their delivery target in ``channel``/``to``.
    Normal user-created legacy jobs always have those fields; if they are
    missing, keep the record for inspection but disable it instead of preserving
    a runtime legacy execution path.
    """
    payload = job.payload
    if payload.kind != "agent_turn" or not _has_legacy_delivery_context(payload):
        return False

    if not payload.channel or not payload.to:
        _disable_malformed_legacy_job(job)
        return True

    payload.session_key = _legacy_session_key(payload)
    payload.origin_channel = payload.origin_channel or payload.channel
    payload.origin_chat_id = payload.origin_chat_id or payload.to
    if not payload.origin_metadata:
        payload.origin_metadata = dict(payload.channel_meta or {})

    payload.deliver = False
    payload.channel = None
    payload.to = None
    payload.channel_meta = {}
    job.updated_at_ms = max(job.updated_at_ms, _now_ms())
    logger.info("Cron: migrated legacy job '{}' ({}) to session-bound payload", job.name, job.id)
    return True


class CronService:
    """Service for managing and executing scheduled jobs."""

    _MAX_RUN_HISTORY = 20

    def __init__(
        self,
        store_path: Path,
        on_job: Callable[[CronJob], Coroutine[Any, Any, str | None]] | None = None,
        max_sleep_ms: int = 300_000,  # 5 minutes
        max_concurrency: int = 4,
    ):
        self.store_path = store_path
        self._action_path = store_path.parent / "action.jsonl"
        self._claims_path = store_path.parent / "claims.json"
        self._owner = uuid.uuid4().hex
        self._claim_lease_ms = 300_000
        self._run_records_dir = store_path.parent / "runs"
        self._lock = FileLock(str(self._action_path.parent) + ".lock")
        self.on_job = on_job
        self._store: CronStore | None = None
        self._timer_task: asyncio.Task | None = None
        self._running = False
        self._timer_active = False
        self.max_sleep_ms = max_sleep_ms
        self.max_concurrency = max(1, max_concurrency)
        self._concurrency = asyncio.Semaphore(self.max_concurrency)

    def _load_jobs(self) -> tuple[list[CronJob], int] | None:
        """Load jobs from disk.

        Returns:
            ``(jobs, version)`` tuple on success or when no store file exists
            (in which case an empty list and version 1 are returned).
            ``None`` when the store file exists but cannot be parsed; the
            corrupt file is preserved with a ``.corrupt-<ts>`` suffix so the
            caller can decide whether to overwrite or bail out.  Returning a
            sentinel here is important: silently treating a parse error as an
            empty job list would cause the next ``_save_store`` to wipe every
            job from disk.
        """
        jobs: list[CronJob] = []
        version = 1
        if self.store_path.exists():
            try:
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                jobs = []
                version = data.get("version", 1)
                for j in data.get("jobs", []):
                    job = CronJob(
                        id=j["id"],
                        name=j["name"],
                        enabled=j.get("enabled", True),
                        schedule=CronSchedule(
                            kind=j["schedule"]["kind"],
                            at_ms=j["schedule"].get("atMs"),
                            every_ms=j["schedule"].get("everyMs"),
                            expr=j["schedule"].get("expr"),
                            tz=j["schedule"].get("tz"),
                        ),
                        payload=CronPayload(
                            kind=j["payload"].get("kind", "agent_turn"),
                            message=j["payload"].get("message", ""),
                            deliver=j["payload"].get("deliver", False),
                            channel=j["payload"].get("channel"),
                            to=j["payload"].get("to"),
                            channel_meta=(
                                j["payload"].get("channelMeta")
                                or j["payload"].get("channel_meta")
                                or {}
                            ),
                            session_key=j["payload"].get("sessionKey") or j["payload"].get("session_key"),
                            origin_channel=(
                                j["payload"].get("originChannel")
                                or j["payload"].get("origin_channel")
                            ),
                            origin_chat_id=(
                                j["payload"].get("originChatId")
                                or j["payload"].get("origin_chat_id")
                            ),
                            origin_metadata=(
                                j["payload"].get("originMetadata")
                                or j["payload"].get("origin_metadata")
                                or {}
                            ),
                            isolated=j["payload"].get("isolated", True),
                            silent=j["payload"].get("silent", False),
                            model_preset=j["payload"].get("model_preset"),
                        ),
                        state=CronJobState(
                            next_run_at_ms=j.get("state", {}).get("nextRunAtMs"),
                            last_run_at_ms=j.get("state", {}).get("lastRunAtMs"),
                            last_status=j.get("state", {}).get("lastStatus"),
                            last_error=j.get("state", {}).get("lastError"),
                            last_delivery_status=j.get("state", {}).get("lastDeliveryStatus"),
                            last_delivery_error=j.get("state", {}).get("lastDeliveryError"),
                            run_history=[
                                CronRunRecord(
                                    run_at_ms=r.get("runAtMs", r.get("startedAtMs", 0)),
                                    scheduled_at_ms=r.get("scheduledAtMs"),
                                    detected_at_ms=r.get("detectedAtMs"),
                                    started_at_ms=r.get("startedAtMs", r.get("runAtMs")),
                                    finished_at_ms=r.get("finishedAtMs"),
                                    status=r["status"],
                                    duration_ms=r.get("durationMs", 0),
                                    error=r.get("error"),
                                    delivery_status=r.get("deliveryStatus"),
                                    delivery_error=r.get("deliveryError"),
                                )
                                for r in j.get("state", {}).get("runHistory", [])
                            ],
                        ),
                        created_at_ms=j.get("createdAtMs", 0),
                        updated_at_ms=j.get("updatedAtMs", 0),
                        delete_after_run=j.get("deleteAfterRun", False),
                        misfire_policy=j.get("misfirePolicy", j.get("misfire_policy", "coalesce")),
                        misfire_grace_ms=j.get("misfireGraceMs", j.get("misfire_grace_ms", 60_000)),
                    )
                    _normalize_agent_turn_job(job)
                    jobs.append(job)
            except Exception:
                # Preserve the corrupt file for forensic recovery instead of
                # letting the next save overwrite it with an empty job list.
                backup = self.store_path.with_suffix(
                    self.store_path.suffix + f".corrupt-{int(time.time())}"
                )
                with suppress(OSError):
                    self.store_path.rename(backup)
                logger.exception(
                    "Failed to load cron store at {}. "
                    "Corrupt file preserved at {}. "
                    "Refusing to overwrite to avoid data loss.",
                    self.store_path,
                    backup,
                )
                return None
        return jobs, version

    def _merge_action(self):
        if not self._action_path.exists():
            return

        jobs_map = {j.id: j for j in self._store.jobs}
        def _update(params: dict):
            j = CronJob.from_dict(params)
            _normalize_agent_turn_job(j)
            jobs_map[j.id] = j

        def _del(params: dict):
            if job_id := params.get("job_id"):
                jobs_map.pop(job_id)

        with self._lock:
            with open(self._action_path, "r", encoding="utf-8") as f:
                changed = False
                for line in f:
                    try:
                        line = line.strip()
                        action = json.loads(line)
                        if "action" not in action:
                            continue
                        if action["action"] == "del":
                            _del(action.get("params", {}))
                        else:
                            _update(action.get("params", {}))
                        changed = True
                    except Exception:
                        logger.exception("load action line error")
                        continue
            self._store.jobs = list(jobs_map.values())
            if self._running and changed:
                self._action_path.write_text("", encoding="utf-8")
                self._save_store()
        return

    def _load_store(self) -> CronStore | None:
        """Load jobs from disk. Reloads automatically if file was modified externally.
        - Reload every time because it needs to merge operations on the jobs object from other instances.
        - During _on_timer execution, return the existing store to prevent concurrent
          _load_store calls (e.g. from list_jobs polling) from replacing it mid-execution.
        - When the on-disk store exists but is unreadable: keep using the
          previous in-memory ``self._store`` if we already have one (so a
          transient corruption does not drop live jobs); only the very first
          load (during ``start``) can return ``None`` to signal an unrecoverable
          state to the caller.
        """
        if self._timer_active and self._store:
            return self._store
        loaded = self._load_jobs()
        if loaded is None:
            # Corrupt store on disk.  Prefer the last good in-memory snapshot
            # over wiping live jobs; ``_load_jobs`` has already moved the
            # corrupt file aside with a ``.corrupt-<ts>`` suffix.
            if self._store is not None:
                return self._store
            return None
        jobs, version = loaded
        self._store = CronStore(version=version, jobs=jobs)
        self._merge_action()

        return self._store

    def _save_store(self) -> None:
        """Save jobs to disk."""
        if not self._store:
            return

        self.store_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "version": self._store.version,
            "jobs": [
                {
                    "id": j.id,
                    "name": j.name,
                    "enabled": j.enabled,
                    "schedule": {
                        "kind": j.schedule.kind,
                        "atMs": j.schedule.at_ms,
                        "everyMs": j.schedule.every_ms,
                        "expr": j.schedule.expr,
                        "tz": j.schedule.tz,
                    },
                    "payload": {
                        "kind": j.payload.kind,
                        "message": j.payload.message,
                        "deliver": j.payload.deliver,
                        "channel": j.payload.channel,
                        "to": j.payload.to,
                        "channelMeta": j.payload.channel_meta,
                        "sessionKey": j.payload.session_key,
                        "originChannel": j.payload.origin_channel,
                        "originChatId": j.payload.origin_chat_id,
                        "originMetadata": j.payload.origin_metadata,
                        "isolated": j.payload.isolated,
                        "silent": j.payload.silent,
                        "model_preset": j.payload.model_preset,
                    },
                    "state": {
                        "nextRunAtMs": j.state.next_run_at_ms,
                        "lastRunAtMs": j.state.last_run_at_ms,
                        "lastStatus": j.state.last_status,
                        "lastError": j.state.last_error,
                        "lastDeliveryStatus": j.state.last_delivery_status,
                        "lastDeliveryError": j.state.last_delivery_error,
                        "runHistory": [
                            {
                                "runAtMs": r.run_at_ms,
                                "scheduledAtMs": r.scheduled_at_ms,
                                "detectedAtMs": r.detected_at_ms,
                                "startedAtMs": r.started_at_ms,
                                "finishedAtMs": r.finished_at_ms,
                                "status": r.status,
                                "durationMs": r.duration_ms,
                                "error": r.error,
                                "deliveryStatus": r.delivery_status,
                                "deliveryError": r.delivery_error,
                            }
                            for r in j.state.run_history
                        ],
                    },
                    "createdAtMs": j.created_at_ms,
                    "updatedAtMs": j.updated_at_ms,
                    "deleteAfterRun": j.delete_after_run,
                    "misfirePolicy": j.misfire_policy,
                    "misfireGraceMs": j.misfire_grace_ms,
                }
                for j in self._store.jobs
            ]
        }

        self._atomic_write(self.store_path, json.dumps(data, indent=2, ensure_ascii=False))

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        """Write *content* to *path* atomically with fsync.

        Uses a temp-file + ``os.replace`` + ``fsync`` pattern so a crash or
        SIGKILL mid-write cannot leave the destination truncated or invalid.
        Mirrors ``nanobot.session.manager.SessionManager.save`` (see
        commit 512bf59, ``fix(session): fsync sessions on graceful shutdown
        to prevent data loss``).  Without this, ``jobs.json`` could be
        corrupted on container shutdown and silently re-created empty on
        next start, wiping every scheduled job.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            # fsync the parent directory so the rename itself is durable.
            # Skip on Windows where opening a directory raises PermissionError;
            # NTFS journals metadata synchronously so this is a no-op there.
            with suppress(PermissionError):
                fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def write_run_record(self, run_id: str, record: dict[str, Any]) -> None:
        """Write an internal audit record for one cron execution.

        Delegates to the shared background-run writer
        (``nanobot.utils.run_records``) so cron and subagent records share one
        schema and one writer. Records land under ``runs/``; subagents land
        under ``subagents/``. The ``kind`` field distinguishes them.
        """
        write_run_record(self._run_records_dir, run_id, record)

    async def start(self) -> None:
        """Start the cron service."""
        self._running = True
        loaded = self._load_store()
        if loaded is None:
            # Store file existed but was corrupt and has been preserved with
            # a ``.corrupt-<ts>`` suffix.  Bail out instead of starting with
            # an empty store; that would call ``_save_store`` and overwrite
            # the now-renamed (but still recoverable) data with [].
            self._running = False
            raise RuntimeError(
                f"cron store at {self.store_path} is corrupt and was preserved; "
                "refusing to start with an empty job list. "
                "Inspect the .corrupt-<ts> backup and restore manually."
            )
        self._recompute_next_runs()
        self._save_store()
        self._arm_timer()
        logger.info("Cron service started with {} jobs", len(self._store.jobs if self._store else []))

    def stop(self) -> None:
        """Stop the cron service."""
        self._running = False
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None

    def _recompute_next_runs(self) -> None:
        """Recompute next run times for all enabled jobs."""
        if not self._store:
            return
        now = _now_ms()
        for job in self._store.jobs:
            if job.enabled and job.state.next_run_at_ms is None:
                # Keep an overdue occurrence so restart handling can apply the
                # persisted misfire policy instead of silently dropping it.
                job.state.next_run_at_ms = _compute_next_run(job.schedule, now)

    def _get_next_wake_ms(self) -> int | None:
        """Get the earliest next run time across all jobs."""
        if not self._store:
            return None
        times = [j.state.next_run_at_ms for j in self._store.jobs
                 if j.enabled and j.state.next_run_at_ms]
        return min(times) if times else None

    def _arm_timer(self) -> None:
        """Schedule the next timer tick."""
        if self._timer_task:
            self._timer_task.cancel()

        if not self._running:
            return

        next_wake = self._get_next_wake_ms()
        if next_wake is None:
            delay_ms = self.max_sleep_ms
        else:
            delay_ms = min(self.max_sleep_ms, max(0, next_wake - _now_ms()))
        delay_s = delay_ms / 1000

        async def tick():
            await asyncio.sleep(delay_s)
            if self._running:
                await self._on_timer()

        self._timer_task = asyncio.create_task(tick())

    async def _on_timer(self) -> None:
        """Handle timer tick - run due jobs."""
        self._load_store()
        # If a hot reload found a corrupt store on disk, ``self._store`` may
        # still hold the previous, known-good in-memory snapshot.  Keep using
        # it rather than crashing the timer or wiping live jobs.
        if not self._store:
            self._arm_timer()
            return

        self._timer_active = True
        try:
            now = _now_ms()
            due_jobs = [
                j for j in self._store.jobs
                if j.enabled and j.state.next_run_at_ms and now >= j.state.next_run_at_ms
            ]

            # Snapshot due jobs: each gets an independent task, bounded by a
            # semaphore, so a slow callback cannot prevent other jobs starting.
            tasks = [asyncio.create_task(self._execute_due_job(j, now)) for j in due_jobs]
            if tasks:
                await asyncio.gather(*tasks)
        finally:
            self._timer_active = False
        self._arm_timer()

    def _advance_schedule(self, job: CronJob, scheduled_ms: int | None) -> None:
        if job.schedule.kind == "every" and scheduled_ms is not None and job.schedule.every_ms:
            # Keep the phase stable. Coalescing intentionally skips missed slots.
            job.state.next_run_at_ms = scheduled_ms + job.schedule.every_ms
            now_ms = _now_ms()
            if job.state.next_run_at_ms <= now_ms:
                missed = (now_ms - job.state.next_run_at_ms) // job.schedule.every_ms + 1
                job.state.next_run_at_ms += missed * job.schedule.every_ms
        elif scheduled_ms is not None and job.schedule.kind == "cron":
            # Coalescing skips every missed calendar occurrence and schedules
            # the first occurrence after the detection time.
            job.state.next_run_at_ms = _compute_next_run(job.schedule, max(scheduled_ms, _now_ms()))
        else:
            job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())

    async def _execute_due_job(self, job: CronJob, detected_ms: int) -> None:
        scheduled_ms = job.state.next_run_at_ms
        if scheduled_ms is None:
            return
        claimed = self._claim_occurrence(job.id, scheduled_ms, require_due=True)
        if claimed is None:
            return
        claimed_job, token = claimed
        late = detected_ms - scheduled_ms
        if late > max(0, claimed_job.misfire_grace_ms) and claimed_job.misfire_policy == "skip":
            claimed_job.state.last_status = "skipped"
            claimed_job.state.last_error = f"misfire: {late}ms late"
            claimed_job.state.last_run_at_ms = detected_ms
            claimed_job.state.run_history.append(CronRunRecord(
                run_at_ms=detected_ms, scheduled_at_ms=scheduled_ms,
                detected_at_ms=detected_ms, started_at_ms=None,
                finished_at_ms=detected_ms, status="skipped", error=claimed_job.state.last_error,
            ))
            claimed_job.state.run_history = claimed_job.state.run_history[-self._MAX_RUN_HISTORY:]
            if claimed_job.schedule.kind == "at":
                if claimed_job.delete_after_run:
                    claimed_job.enabled = False
                else:
                    claimed_job.enabled = False
                    claimed_job.state.next_run_at_ms = None
            else:
                self._advance_schedule(claimed_job, scheduled_ms)
            claimed_job.updated_at_ms = detected_ms
            self._finalize_claim(claimed_job, scheduled_ms, token)
            return
        await self._run_claimed(claimed_job, scheduled_ms, detected_ms, token)
        self._finalize_claim(claimed_job, scheduled_ms, token)

    async def _execute_job(self, job: CronJob, scheduled_ms: int | None = None, detected_ms: int | None = None) -> None:
        """Execute a single job."""
        detected_ms = detected_ms if detected_ms is not None else _now_ms()
        scheduled_ms = scheduled_ms if scheduled_ms is not None else job.state.next_run_at_ms
        start_ms = _now_ms()
        logger.info("Cron: executing job '{}' ({})", job.name, job.id)

        try:
            if self.on_job:
                await self.on_job(job)

            job.state.last_status = "ok"
            job.state.last_error = None
            logger.info("Cron: job '{}' completed", job.name)

        except CronJobSkippedError as e:
            job.state.last_status = "skipped"
            job.state.last_error = str(e) or None
            logger.warning("Cron: job '{}' skipped: {}", job.name, job.state.last_error or "")
        except asyncio.CancelledError as e:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            job.state.last_status = "error"
            job.state.last_error = str(e) or e.__class__.__name__
            logger.exception("Cron: job '{}' was cancelled", job.name)
        except Exception as e:
            job.state.last_status = "error"
            job.state.last_error = str(e)
            logger.exception("Cron: job '{}' failed", job.name)

        end_ms = _now_ms()
        job.state.last_run_at_ms = start_ms
        job.updated_at_ms = end_ms

        job.state.run_history.append(CronRunRecord(
            run_at_ms=start_ms,
            scheduled_at_ms=scheduled_ms,
            detected_at_ms=detected_ms,
            started_at_ms=start_ms,
            finished_at_ms=end_ms,
            status=job.state.last_status,
            duration_ms=end_ms - start_ms,
            error=job.state.last_error,
            delivery_status=job.state.last_delivery_status,
            delivery_error=job.state.last_delivery_error,
        ))
        job.state.run_history = job.state.run_history[-self._MAX_RUN_HISTORY:]

        # Handle one-shot jobs
        if job.schedule.kind == "at":
            if job.delete_after_run:
                self._store.jobs = [j for j in self._store.jobs if j.id != job.id]
            else:
                job.enabled = False
                job.state.next_run_at_ms = None
        else:
            # Advance from the scheduled occurrence, not completion time.
            self._advance_schedule(job, scheduled_ms)

    def _read_claims(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self._claims_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"cron claims store is corrupt: {self._claims_path}") from exc
        if not isinstance(data, dict) or not all(isinstance(value, dict) for value in data.values()):
            raise RuntimeError(f"cron claims store has invalid shape: {self._claims_path}")
        return data

    def _claim_occurrence(
        self, job_id: str, scheduled_ms: int, *, force: bool = False, require_due: bool = True
    ) -> tuple[CronJob, str] | None:
        """Atomically claim one occurrence, without holding the lock while running it."""
        now = _now_ms()
        with self._lock:
            loaded = self._load_jobs()
            if loaded is None:
                return None
            jobs, version = loaded
            job = next((j for j in jobs if j.id == job_id), None)
            if job is None or (not force and not job.enabled):
                return None
            if require_due and job.state.next_run_at_ms != scheduled_ms:
                return None
            # A manual run claims the same pending occurrence as the timer.
            if not require_due and job.state.next_run_at_ms != scheduled_ms:
                return None
            claims = self._read_claims()
            key = f"{job_id}:{scheduled_ms}"
            # Remove abandoned claims while holding the same transaction lock.
            claims = {k: v for k, v in claims.items()
                      if int(v.get("lease_expires_at_ms", 0)) > now}
            old = claims.get(key)
            if old and int(old.get("lease_expires_at_ms", 0)) > now:
                return None
            token = uuid.uuid4().hex
            claims[key] = {
                "job_id": job_id, "scheduled_at_ms": scheduled_ms,
                "owner": self._owner, "token": token,
                "lease_expires_at_ms": now + self._claim_lease_ms,
            }
            self._atomic_write(self._claims_path, json.dumps(claims, indent=2))
            return copy.deepcopy(job), token

    def _renew_claim(self, job_id: str, scheduled_ms: int, token: str) -> bool:
        with self._lock:
            claims = self._read_claims()
            key = f"{job_id}:{scheduled_ms}"
            claim = claims.get(key)
            if not claim or claim.get("token") != token:
                return False
            claim["lease_expires_at_ms"] = _now_ms() + self._claim_lease_ms
            self._atomic_write(self._claims_path, json.dumps(claims, indent=2))
            return True

    async def _claim_heartbeat(self, job_id: str, scheduled_ms: int, token: str) -> None:
        interval = max(1, self._claim_lease_ms // 3) / 1000
        while True:
            await asyncio.sleep(interval)
            if not self._renew_claim(job_id, scheduled_ms, token):
                return

    async def _run_claimed(self, job: CronJob, scheduled_ms: int, detected_ms: int, token: str) -> None:
        heartbeat = asyncio.create_task(self._claim_heartbeat(job.id, scheduled_ms, token))
        try:
            async with self._concurrency:
                await self._execute_job(job, scheduled_ms, detected_ms)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    def _finalize_claim(self, job: CronJob, scheduled_ms: int, token: str) -> bool:
        """Commit execution state only while the claim token is still fenced."""
        with self._lock:
            claims = self._read_claims()
            key = f"{job.id}:{scheduled_ms}"
            claim = claims.get(key)
            if not claim or claim.get("token") != token:
                return False

            loaded = self._load_jobs()
            if loaded is None:
                return False
            jobs, version = loaded

            # Fold legacy queued mutations into the fresh snapshot. Validate
            # the claim first: a stale worker must never consume another
            # process's pending actions.
            actions_changed = False
            if self._action_path.exists():
                jobs_map = {item.id: item for item in jobs}
                with open(self._action_path, "r", encoding="utf-8") as action_file:
                    for line in action_file:
                        try:
                            action = json.loads(line)
                            params = action.get("params", {})
                            if action.get("action") == "del":
                                jobs_map.pop(params.get("job_id"), None)
                                actions_changed = True
                            elif action.get("action") in ("add", "update"):
                                changed_job = CronJob.from_dict(params)
                                _normalize_agent_turn_job(changed_job)
                                jobs_map[changed_job.id] = changed_job
                                actions_changed = True
                        except Exception:
                            logger.exception("load action line error during claim finalization")
                if actions_changed:
                    jobs = list(jobs_map.values())

            current = next((item for item in jobs if item.id == job.id), None)
            if current is None:
                claims.pop(key, None)
                self._atomic_write(self._claims_path, json.dumps(claims, indent=2))
                return False

            # Keep edits made while the agent was running. Execution owns only
            # runtime state, and advances the schedule only if it is unchanged.
            same_occurrence = current.state.next_run_at_ms == scheduled_ms
            current.state.last_run_at_ms = job.state.last_run_at_ms
            current.state.last_status = job.state.last_status
            current.state.last_error = job.state.last_error
            current.state.last_delivery_status = job.state.last_delivery_status
            current.state.last_delivery_error = job.state.last_delivery_error
            current.state.run_history = job.state.run_history
            if same_occurrence:
                current.state.next_run_at_ms = job.state.next_run_at_ms
                current.enabled = job.enabled
                if job.delete_after_run and current.schedule.kind == "at":
                    jobs = [item for item in jobs if item.id != job.id]
            current.updated_at_ms = max(current.updated_at_ms, job.updated_at_ms)
            self._store = CronStore(version=version, jobs=jobs)
            self._save_store_unlocked()
            if actions_changed:
                self._atomic_write(self._action_path, "")
            claims.pop(key, None)
            self._atomic_write(self._claims_path, json.dumps(claims, indent=2))
            return True

    def _save_store_unlocked(self) -> None:
        """Save while the caller owns the process-wide persistence lock."""
        # _save_store is lock-taking for ordinary mutations; this variant is
        # used by the claim finalization transaction to avoid re-entrancy.
        self._save_store()

    def _append_action(self, action: Literal["add", "del", "update"], params: dict):
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with open(self._action_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"action": action, "params": params}, ensure_ascii=False) + "\n")


    # ========== Public API ==========

    def list_jobs(self, include_disabled: bool = False) -> list[CronJob]:
        """List all jobs."""
        store = self._load_store()
        jobs = store.jobs if include_disabled else [j for j in store.jobs if j.enabled]
        return sorted(jobs, key=lambda j: j.state.next_run_at_ms or float('inf'))

    def list_bound_cron_jobs_for_session(
        self,
        session_key: str,
        *,
        include_disabled: bool = True,
    ) -> list[CronJob]:
        """Return user-created bound cron jobs owned by *session_key*."""
        return [
            job
            for job in self.list_jobs(include_disabled=include_disabled)
            if is_bound_cron_job(job)
            and job.payload.session_key == session_key
        ]

    def add_job(
        self,
        name: str,
        schedule: CronSchedule,
        message: str,
        deliver: bool = False,
        channel: str | None = None,
        to: str | None = None,
        delete_after_run: bool = False,
        channel_meta: dict | None = None,
        session_key: str | None = None,
        origin_channel: str | None = None,
        origin_chat_id: str | None = None,
        origin_metadata: dict | None = None,
        silent: bool = False,
        model_preset: str | None = None,
        isolated: bool = True,
        misfire_policy: Literal["skip", "coalesce"] = "coalesce",
        misfire_grace_ms: int = 60_000,
    ) -> CronJob:
        """Add a new job."""
        _validate_schedule_for_add(schedule)
        if misfire_policy not in ("skip", "coalesce"):
            raise ValueError("misfire_policy must be 'skip' or 'coalesce'")
        if misfire_grace_ms < 0:
            raise ValueError("misfire_grace_ms must be non-negative")
        now = _now_ms()

        job = CronJob(
            id=str(uuid.uuid4())[:8],
            name=name,
            enabled=True,
            schedule=schedule,
            payload=CronPayload(
                kind="agent_turn",
                message=message,
                deliver=deliver,
                channel=channel,
                to=to,
                channel_meta=channel_meta or {},
                session_key=session_key,
                origin_channel=origin_channel,
                origin_chat_id=origin_chat_id,
                origin_metadata=origin_metadata or {},
                silent=silent,
                model_preset=model_preset,
                isolated=isolated,
            ),
            state=CronJobState(next_run_at_ms=_compute_next_run(schedule, now)),
            created_at_ms=now,
            updated_at_ms=now,
            delete_after_run=delete_after_run,
            misfire_policy=misfire_policy,
            misfire_grace_ms=misfire_grace_ms,
        )
        _normalize_agent_turn_job(job)
        if self._running:
            store = self._load_store()
            store.jobs.append(job)
            self._save_store()
            self._arm_timer()
        else:
            self._append_action("add", asdict(job))

        logger.info("Cron: added job '{}' ({})", name, job.id)
        return job

    def register_system_job(self, job: CronJob) -> CronJob:
        """Register an internal system job (idempotent on restart)."""
        store = self._load_store()
        now = _now_ms()
        job.state = CronJobState(next_run_at_ms=_compute_next_run(job.schedule, now))
        job.created_at_ms = now
        job.updated_at_ms = now
        store.jobs = [j for j in store.jobs if j.id != job.id]
        store.jobs.append(job)
        self._save_store()
        self._arm_timer()
        logger.info("Cron: registered system job '{}' ({})", job.name, job.id)
        return job

    def remove_job(self, job_id: str) -> Literal["removed", "protected", "not_found"]:
        """Remove a job by ID, unless it is a protected system job."""
        store = self._load_store()
        job = next((j for j in store.jobs if j.id == job_id), None)
        if job is None:
            return "not_found"
        if job.payload.kind == "system_event":
            logger.info("Cron: refused to remove protected system job {}", job_id)
            return "protected"

        before = len(store.jobs)
        store.jobs = [j for j in store.jobs if j.id != job_id]
        removed = len(store.jobs) < before

        if removed:
            if self._running:
                self._save_store()
                self._arm_timer()
            else:
                self._append_action("del", {"job_id": job_id})
            logger.info("Cron: removed job {}", job_id)
            return "removed"

        return "not_found"

    def enable_job(self, job_id: str, enabled: bool = True) -> CronJob | None:
        """Enable or disable a job."""
        store = self._load_store()
        for job in store.jobs:
            if job.id == job_id:
                job.enabled = enabled
                job.updated_at_ms = _now_ms()
                if enabled:
                    job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())
                else:
                    job.state.next_run_at_ms = None
                if self._running:
                    self._save_store()
                    self._arm_timer()
                else:
                    self._append_action("update", asdict(job))
                return job
        return None

    def update_job(
        self,
        job_id: str,
        *,
        name: str | None = None,
        schedule: CronSchedule | None = None,
        message: str | None = None,
        deliver: bool | None = None,
        channel: str | None = ...,
        to: str | None = ...,
        delete_after_run: bool | None = None,
        silent: bool | None = None,
        isolated: bool | None = None,
        misfire_policy: Literal["skip", "coalesce"] | None = None,
        misfire_grace_ms: int | None = None,
    ) -> CronJob | Literal["not_found", "protected"]:
        """Update mutable fields of an existing job. System jobs cannot be updated.

        For ``channel`` and ``to``, pass an explicit value (including ``None``)
        to update; omit (sentinel ``...``) to leave unchanged.
        """
        store = self._load_store()
        job = next((j for j in store.jobs if j.id == job_id), None)
        if job is None:
            return "not_found"
        if job.payload.kind == "system_event":
            return "protected"

        if schedule is not None:
            _validate_schedule_for_add(schedule)
            job.schedule = schedule
        if name is not None:
            job.name = name
        if message is not None:
            job.payload.message = message
        if deliver is not None:
            job.payload.deliver = deliver
        if channel is not ...:
            job.payload.channel = channel
        if to is not ...:
            job.payload.to = to
        if delete_after_run is not None:
            job.delete_after_run = delete_after_run
        if silent is not None:
            job.payload.silent = silent
        if isolated is not None:
            job.payload.isolated = isolated
        if misfire_policy is not None:
            if misfire_policy not in ("skip", "coalesce"):
                raise ValueError("misfire_policy must be 'skip' or 'coalesce'")
            job.misfire_policy = misfire_policy
        if misfire_grace_ms is not None:
            if misfire_grace_ms < 0:
                raise ValueError("misfire_grace_ms must be non-negative")
            job.misfire_grace_ms = misfire_grace_ms
        _normalize_agent_turn_job(job)

        job.updated_at_ms = _now_ms()
        if job.enabled:
            job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())

        if self._running:
            self._save_store()
            self._arm_timer()
        else:
            self._append_action("update", asdict(job))

        logger.info("Cron: updated job '{}' ({})", job.name, job.id)
        return job

    async def run_job(self, job_id: str, force: bool = False) -> bool:
        """Manually run a job; manual and timer runs share occurrence claims."""
        was_running = self._running
        self._running = True
        try:
            store = self._load_store()
            job = next((j for j in store.jobs if j.id == job_id), None)
            if job is None or (not force and not job.enabled) or job.state.next_run_at_ms is None:
                return False
            scheduled_ms = job.state.next_run_at_ms
            claimed = self._claim_occurrence(job_id, scheduled_ms, force=force, require_due=False)
            if claimed is None:
                return False
            claimed_job, token = claimed
            await self._run_claimed(claimed_job, scheduled_ms, _now_ms(), token)
            return self._finalize_claim(claimed_job, scheduled_ms, token)
        finally:
            self._running = was_running
            if was_running:
                self._arm_timer()

    def get_job(self, job_id: str) -> CronJob | None:
        """Get a job by ID."""
        store = self._load_store()
        return next((j for j in store.jobs if j.id == job_id), None)

    def status(self) -> dict:
        """Get service status."""
        store = self._load_store()
        return {
            "enabled": self._running,
            "jobs": len(store.jobs),
            "next_wake_at_ms": self._get_next_wake_ms(),
        }
