"""Shared metadata helpers for scheduled cron session turns."""

from __future__ import annotations

from typing import Any, Mapping

from nanobot.cron.types import CronJob

CRON_TRIGGER_META = "_cron_trigger"
CRON_DEFER_UNTIL_IDLE_META = "_cron_defer_until_session_idle"
CRON_HISTORY_META = "_cron_turn"
# Tagged onto a cron turn's inbound metadata by run_bound_cron_job when the
# originating job was created with silent=True. See cron_suppress_success_delivery().
CRON_SILENT_META = "_cron_silent"
# Exact marker a cron job may return to opt out of delivery for a single run,
# e.g. a periodic check that found nothing worth reporting. Trimmed, exact
# match only — never substring/fuzzy, never honored on non-cron turns.
CRON_SILENT_MARKER = "[SILENT]"


def cron_trigger(metadata: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return structured cron trigger metadata when present."""
    raw = (metadata or {}).get(CRON_TRIGGER_META)
    return raw if isinstance(raw, dict) else None


def is_cron_turn(metadata: Mapping[str, Any] | None) -> bool:
    return cron_trigger(metadata) is not None


def defer_cron_until_session_idle(metadata: Mapping[str, Any] | None) -> bool:
    return bool(
        is_cron_turn(metadata)
        and (metadata or {}).get(CRON_DEFER_UNTIL_IDLE_META) is True
    )


def cron_run_id(metadata: Mapping[str, Any] | None) -> str | None:
    trigger = cron_trigger(metadata)
    if not trigger:
        return None
    value = trigger.get("run_id")
    return value if isinstance(value, str) and value else None


def cron_history_overrides(metadata: Mapping[str, Any] | None) -> tuple[str | None, dict[str, Any]]:
    """Return session-history text/metadata overrides for a cron turn."""
    trigger = cron_trigger(metadata)
    if not trigger:
        return None, {}
    persist_content = trigger.get("persist_content")
    text = (
        persist_content
        if isinstance(persist_content, str) and persist_content.strip()
        else None
    )
    return text, {
        CRON_HISTORY_META: True,
        "cron_job_id": trigger.get("job_id"),
        "cron_job_name": trigger.get("job_name"),
        "cron_run_id": trigger.get("run_id"),
        "cron_prompt_ref": trigger.get("prompt_ref"),
    }


def is_bound_cron_job(job: CronJob) -> bool:
    """True for session-bound cron jobs with complete delivery context."""
    payload = job.payload
    if (
        payload.kind != "agent_turn"
        or not payload.session_key
        or not payload.origin_channel
        or not payload.origin_chat_id
    ):
        return False
    return not (
        payload.deliver
        or payload.channel
        or payload.to
        or payload.channel_meta
    )


def cron_suppress_success_delivery(
    metadata: Mapping[str, Any] | None,
    content: str | None,
) -> bool:
    """True when a cron/background turn's success output should NOT be published.

    Two conditions, cron turns only (normal chat is never suppressed here):
      1. the job was created with ``silent=True`` (CRON_SILENT_META set), or
      2. the response body is exactly ``[SILENT]`` (trimmed).

    Suppression is success-only; error-path messages are published by the
    caller's except branch and are not affected.
    """
    if not is_cron_turn(metadata):
        return False
    if (metadata or {}).get(CRON_SILENT_META):
        return True
    if (content or "").strip() == CRON_SILENT_MARKER:
        return True
    return False
