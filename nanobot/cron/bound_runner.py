"""Execution helpers for session-bound cron jobs."""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from nanobot.agent.tools.cron import CronTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.cron.session_delivery import origin_delivery_context
from nanobot.cron.session_turns import (
    CRON_DEFER_UNTIL_IDLE_META,
    CRON_RUN_SNAPSHOT_META,
    CRON_SILENT_META,
    CRON_TRIGGER_META,
)
from nanobot.cron.types import CronJob, CronRunResult
from nanobot.utils.prompt_templates import render_template
from nanobot.utils.run_records import build_usage_block


def _now_ms() -> int:
    """Wall-clock time in ms. Module-level so tests can pin it."""
    return int(time.time() * 1000)


class BoundCronAgent(Protocol):
    tools: Any
    provider: Any
    model: str

    @property
    def last_usage(self) -> dict[str, int]:
        """Token usage from the most recently completed turn."""
        ...

    def cron_run_snapshot(self) -> dict[str, Any] | None:
        ...

    def cron_run_snapshot_for_preset(self, name: str) -> dict[str, Any] | None:
        ...

    async def process_direct(
        self,
        content: str,
        *,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[..., Awaitable[None]] | None = None,
        ephemeral: bool = False,
        run_provider: Any = None,
        run_model: str | None = None,
        run_context_window_tokens: int | None = None,
        metadata: dict | None = None,
    ) -> OutboundMessage | None:
        ...

    async def submit_cron_turn(self, msg: InboundMessage) -> OutboundMessage | None:
        ...


class CronRunRecorder(Protocol):
    def write_run_record(self, run_id: str, record: dict[str, Any]) -> None:
        ...


def _cron_prompt_ref(prompt: str) -> dict[str, Any]:
    return {
        "id": "cron.agent_turn.reminder",
        "version": 1,
        "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }


def _agent_provider_name(agent: BoundCronAgent) -> str:
    provider = getattr(agent, "provider", None)
    spec = getattr(provider, "_spec", None)
    name = getattr(spec, "name", None)
    return str(name or getattr(provider, "__class__", type("x", (), {"__name__": "unknown"})).__name__)


def _bound_session_delivery_context(
    job: CronJob,
    *,
    turn_seed: str,
    source_label: str | None,
) -> tuple[str, str, dict[str, Any]]:
    channel, chat_id, metadata = origin_delivery_context(job)

    return channel, chat_id, metadata


async def run_bound_cron_job(
    job: CronJob,
    *,
    agent: BoundCronAgent,
    cron: CronRunRecorder,
) -> str | None:
    """Execute a session-bound cron job as a normal agent session turn."""
    session_key = job.payload.session_key
    if not session_key:
        raise ValueError(f"cron job {job.id} is missing payload.session_key")

    prompt = render_template(
        "agent/cron_reminder.md",
        strip=True,
        message=job.payload.message,
    )
    prompt_ref = _cron_prompt_ref(prompt)
    run_id = f"{job.id}:{_now_ms()}:{uuid.uuid4().hex[:8]}"
    channel, chat_id, metadata = _bound_session_delivery_context(
        job,
        turn_seed=f"cron:{job.id}",
        source_label=job.name,
    )
    metadata[CRON_TRIGGER_META] = {
        "job_id": job.id,
        "job_name": job.name,
        "run_id": run_id,
        "prompt_ref": prompt_ref,
        "persist_content": (
            f"Scheduled cron job triggered: {job.name}\n\n{job.payload.message}"
        ),
    }
    # Mode for policy rules: scheduled cron turns are mode=cron.
    metadata["_interaction_mode"] = "cron"
    metadata[CRON_DEFER_UNTIL_IDLE_META] = True
    # Per-job model preset wins over the global cron snapshot; fall back to
    # the global snapshot (then the main model) if resolution fails.
    snapshot = None
    if job.payload.model_preset:
        resolver = getattr(agent, "cron_run_snapshot_for_preset", None)
        snapshot = resolver(job.payload.model_preset) if callable(resolver) else None
    if snapshot is None:
        snapshot_getter = getattr(agent, "cron_run_snapshot", None)
        snapshot = snapshot_getter() if callable(snapshot_getter) else None
    if snapshot:
        metadata[CRON_RUN_SNAPSHOT_META] = snapshot
    # Tag success-output suppression policy: a silent job runs but its reply
    # is never published to chat. Honored by AgentLoop._dispatch.
    if job.payload.silent:
        metadata[CRON_SILENT_META] = True
    run_record_base: dict[str, Any] = {
        "kind": "cron",
        "job_id": job.id,
        "job_name": job.name,
        "session_key": session_key,
        "prompt_ref": prompt_ref,
        "prompt_vars": {"message": job.payload.message},
        "rendered_prompt": prompt,
        "silent": bool(job.payload.silent),
    }

    cron.write_run_record(
        run_id,
        {
            **run_record_base,
            "status": "queued",
        },
    )

    cron_tool = agent.tools.get("cron")
    cron_token = None
    if isinstance(cron_tool, CronTool):
        cron_token = cron_tool.set_cron_context(True)
    try:
        resp = await agent.submit_cron_turn(
            InboundMessage(
                channel=channel,
                sender_id="cron",
                chat_id=chat_id,
                content=prompt,
                metadata=metadata,
                session_key_override=session_key,
            )
        )
        agent_finished_at_ms = _now_ms()
    except (Exception, asyncio.CancelledError) as exc:
        error_text = str(exc) or exc.__class__.__name__
        agent_finished_at_ms = _now_ms()
        job.state.last_delivery_status = "not_attempted"
        job.state.last_delivery_error = error_text
        cron.write_run_record(
            run_id,
            {
                **run_record_base,
                "status": "error",
                "error": error_text,
                "agent_finished_at_ms": agent_finished_at_ms,
                "delivery": {"status": "not_attempted", "error": error_text},
            },
        )
        raise
    finally:
        if isinstance(cron_tool, CronTool) and cron_token is not None:
            cron_tool.reset_cron_context(cron_token)

    response = resp.content if resp else ""
    delivery_status = "empty" if not response else "suppressed" if job.payload.silent else "delivered"
    job.state.last_delivery_status = delivery_status
    job.state.last_delivery_error = None
    # In-band cron delivery is complete once the turn's reply has been routed
    # back through the loop; record the wall time so delivery duration is
    # separately measurable from turn duration.
    delivery_finished_at_ms = _now_ms()
    job.state.last_delivery_at_ms = delivery_finished_at_ms
    # What actually ran + what it cost. provider/model come from the loop's
    # active runtime for this turn (cron turns do not currently override the
    # provider). usage is the delta captured by _last_usage for this turn.
    provider_name = _agent_provider_name(agent)
    usage_block = build_usage_block(
        getattr(agent, "last_usage", None),
        provider=provider_name,
        model=getattr(agent, "model", None),
    )
    cron.write_run_record(
        run_id,
        {
            **run_record_base,
            "status": "ok",
            "response": response,
            "usage": usage_block,
            "agent_finished_at_ms": agent_finished_at_ms,
            "delivery": {
                "status": delivery_status,
                "error": None,
                "at_ms": delivery_finished_at_ms,
            },
        },
    )
    return CronRunResult(
        response,
        agent_finished_at_ms=agent_finished_at_ms,
        delivery_finished_at_ms=delivery_finished_at_ms,
    )


async def _async_noop(*_args: Any, **_kwargs: Any) -> None:
    """No-op async callback for progress/stream hooks in isolated runs."""
    return None


async def run_isolated_cron_job(
    job: CronJob,
    *,
    agent: BoundCronAgent,
    cron: CronRunRecorder,
    deliver: Callable[..., Awaitable[None]],
) -> str | None:
    """Execute a session-bound cron job in an isolated background session.

    Unlike ``run_bound_cron_job`` (in-band in the chat session), this runs the
    turn via ``process_direct`` in a per-run ephemeral session: no shared chat
    context, no progress chatter, and foreground replies cannot redirect it.
    The ``message()`` tool remains available for targeted pings during the
    turn. Only the final auto-reply is delivered to the origin chat — and only
    when the job is not ``silent``.
    """
    if not job.payload.session_key:
        raise ValueError(f"cron job {job.id} is missing payload.session_key")

    prompt = render_template(
        "agent/cron_reminder.md",
        strip=True,
        message=job.payload.message,
    )
    prompt_ref = _cron_prompt_ref(prompt)
    run_id = f"{job.id}:{_now_ms()}:{uuid.uuid4().hex[:8]}"
    channel, chat_id, _ = origin_delivery_context(job)

    # Per-job model preset wins over the global cron snapshot; fall back to
    # the global snapshot (then the main model) if resolution fails.
    snapshot = None
    if job.payload.model_preset:
        resolver = getattr(agent, "cron_run_snapshot_for_preset", None)
        snapshot = resolver(job.payload.model_preset) if callable(resolver) else None
    if snapshot is None:
        snapshot_getter = getattr(agent, "cron_run_snapshot", None)
        snapshot = snapshot_getter() if callable(snapshot_getter) else None

    run_record_base: dict[str, Any] = {
        "kind": "cron",
        "job_id": job.id,
        "job_name": job.name,
        "session_key": job.payload.session_key,
        "isolated": True,
        "prompt_ref": prompt_ref,
        "prompt_vars": {"message": job.payload.message},
        "rendered_prompt": prompt,
        "silent": bool(job.payload.silent),
    }
    cron.write_run_record(run_id, {**run_record_base, "status": "queued"})

    # Fresh per-run session: no carryover between runs, nothing to persist or
    # retain. ephemeral=True means the working history is never saved.
    session_key = f"cron:{job.id}:{run_id}"

    cron_tool = agent.tools.get("cron")
    cron_token = None
    if isinstance(cron_tool, CronTool):
        cron_token = cron_tool.set_cron_context(True)
    try:
        resp = await agent.process_direct(
            prompt,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            on_progress=_async_noop,
            ephemeral=True,
            metadata={"_interaction_mode": "cron"},
            run_provider=(snapshot.get("provider") if snapshot else None),
            run_model=(snapshot.get("model") if snapshot else None),
            run_context_window_tokens=(
                snapshot.get("context_window_tokens") if snapshot else None
            ),
        )
        agent_finished_at_ms = _now_ms()
    except (Exception, asyncio.CancelledError) as exc:
        error_text = str(exc) or exc.__class__.__name__
        agent_finished_at_ms = _now_ms()
        job.state.last_delivery_status = "not_attempted"
        job.state.last_delivery_error = error_text
        cron.write_run_record(
            run_id,
            {
                **run_record_base,
                "status": "error",
                "error": error_text,
                "agent_finished_at_ms": agent_finished_at_ms,
                "delivery": {"status": "not_attempted", "error": error_text},
            },
        )
        raise
    finally:
        if isinstance(cron_tool, CronTool) and cron_token is not None:
            cron_tool.reset_cron_context(cron_token)

    response = resp.content if resp else ""
    delivery_status = "empty" if not response else "suppressed" if job.payload.silent else "pending"
    delivery_error = None
    if delivery_status == "pending":
        try:
            await deliver(
                OutboundMessage(channel=channel, chat_id=chat_id, content=response),
                record=True,
            )
            delivery_status = "delivered"
        except (Exception, asyncio.CancelledError) as exc:
            delivery_status = "failed"
            delivery_error = str(exc) or exc.__class__.__name__
            delivery_finished_at_ms = _now_ms()
            job.state.last_delivery_status = delivery_status
            job.state.last_delivery_error = delivery_error
            job.state.last_delivery_at_ms = delivery_finished_at_ms
            cron.write_run_record(
                run_id,
                {
                    **run_record_base,
                    "status": "error",
                    "error": f"delivery failed: {delivery_error}",
                    "response": response,
                    "agent_finished_at_ms": agent_finished_at_ms,
                    "delivery": {
                        "status": delivery_status,
                        "error": delivery_error,
                        "at_ms": delivery_finished_at_ms,
                    },
                },
            )
            raise

    delivery_finished_at_ms = _now_ms()
    job.state.last_delivery_status = delivery_status
    job.state.last_delivery_error = delivery_error
    job.state.last_delivery_at_ms = delivery_finished_at_ms

    provider_name = _agent_provider_name(agent)
    usage_block = build_usage_block(
        getattr(agent, "last_usage", None),
        provider=provider_name,
        model=getattr(agent, "model", None),
    )
    cron.write_run_record(
        run_id,
        {
            **run_record_base,
            "status": "ok",
            "response": response,
            "usage": usage_block,
            "agent_finished_at_ms": agent_finished_at_ms,
            "delivery": {
                "status": delivery_status,
                "error": delivery_error,
                "at_ms": delivery_finished_at_ms,
            },
        },
    )
    return CronRunResult(
        response,
        agent_finished_at_ms=agent_finished_at_ms,
        delivery_finished_at_ms=delivery_finished_at_ms,
    )
