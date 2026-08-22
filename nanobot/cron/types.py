"""Cron types."""

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class CronSchedule:
    """Schedule definition for a cron job."""
    kind: Literal["at", "every", "cron"]
    # For "at": timestamp in ms
    at_ms: int | None = None
    # For "every": interval in ms
    every_ms: int | None = None
    # For "cron": cron expression (e.g. "0 9 * * *")
    expr: str | None = None
    # Timezone for cron expressions
    tz: str | None = None


@dataclass
class CronPayload:
    """What to do when the job runs."""
    kind: Literal["system_event", "agent_turn"] = "agent_turn"
    message: str = ""
    # ── DEAD / LEGACY — pre-session-bound cron delivery fields. ─────────────
    # These controlled the old "push result to an external channel" path.
    # Session-bound cron (run_bound_cron_job) ignores them entirely, and
    # _normalize_agent_turn_job force-clears them on every bound job.
    #
    # DO NOT reuse `deliver` to mean "silence the chat reply" — its history
    # is the opposite (deliver=True once meant "push to WhatsApp"), and
    # every existing bound job already has deliver=False, so repurposing it
    # would silently mute every reminder. Use `silent` instead.
    #
    # ponytail: kept only to deserialize old job stores + match upstream,
    # which still ships these. Safe to delete once legacy migration is done.
    deliver: bool = False
    channel: str | None = None  # e.g. "whatsapp"
    to: str | None = None  # e.g. phone number
    channel_meta: dict[str, Any] = field(default_factory=dict)
    # ── end dead/legacy fields. ─────────────────────────────────────────────
    session_key: str | None = None  # original session key for correct session recording
    origin_channel: str | None = None
    origin_chat_id: str | None = None
    origin_metadata: dict[str, Any] = field(default_factory=dict)
    # Session-bound delivery control.
    # silent=True: the job runs normally (tools, file writes, logging, run
    # record) but its success reply is never published to chat. Defaults
    # False so existing reminders keep notifying. Honored only for cron/
    # background turns; see cron_suppress_success_delivery().
    silent: bool = False
    # Optional model_preset name to run this job's turn on (e.g. a cheap model
    # for a recurring web check). Validated at add time; resolved to a provider
    # snapshot at trigger time. None = use the global cron run preset/main model.
    model_preset: str | None = None
    # Run in a dedicated background session instead of the user's live chat
    # session. Isolated jobs share no chat context, emit no progress chatter,
    # can't be redirected by foreground replies, and deliver only their final
    # reply (when not silent) to the origin chat. Default True; legacy jobs
    # loaded without the field become isolated too.
    isolated: bool = True


@dataclass
class CronRunRecord:
    """A single execution record for a cron job."""
    run_at_ms: int
    status: Literal["ok", "error", "skipped"]
    duration_ms: int = 0
    error: str | None = None
    delivery_status: str | None = None
    delivery_error: str | None = None


@dataclass
class CronJobState:
    """Runtime state of a job."""
    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: Literal["ok", "error", "skipped"] | None = None
    last_error: str | None = None
    last_delivery_status: str | None = None
    last_delivery_error: str | None = None
    run_history: list[CronRunRecord] = field(default_factory=list)


@dataclass
class CronJob:
    """A scheduled job."""
    id: str
    name: str
    enabled: bool = True
    schedule: CronSchedule = field(default_factory=lambda: CronSchedule(kind="every"))
    payload: CronPayload = field(default_factory=CronPayload)
    state: CronJobState = field(default_factory=CronJobState)
    created_at_ms: int = 0
    updated_at_ms: int = 0
    delete_after_run: bool = False

    @classmethod
    def from_dict(cls, kwargs: dict):
        state_kwargs = dict(kwargs.get("state", {}))
        state_kwargs["run_history"] = [
            record if isinstance(record, CronRunRecord) else CronRunRecord(**record)
            for record in state_kwargs.get("run_history", [])
        ]
        kwargs["schedule"] = CronSchedule(**kwargs.get("schedule", {"kind": "every"}))
        kwargs["payload"] = CronPayload(**kwargs.get("payload", {}))
        kwargs["state"] = CronJobState(**state_kwargs)
        return cls(**kwargs)


@dataclass
class CronStore:
    """Persistent store for cron jobs."""
    version: int = 1
    jobs: list[CronJob] = field(default_factory=list)
