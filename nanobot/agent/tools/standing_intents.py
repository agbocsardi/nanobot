"""Event-conditioned reminders via standing intents (issue #28).

A standing intent is a durable, owner-scoped rule: "when the user's trusted
inbound text matches these trigger terms, surface this reminder." Matching is
deterministic and bounded (no model call, no embeddings); firing is atomic so
duplicate/replayed inbound updates cannot double-fire; cancellation, expiry,
cooldown and a maximum fire budget are explicit durable state.

The matching core (``normalize_text``/``match_trigger_groups``) is pure and
testable in isolation. Storage (``StandingIntentStore``) and the ``intent``
tool live in the same module but are deliberately separate concerns.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ContextAware, RequestContext
from nanobot.agent.tools.schema import (
    ArraySchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)

# Conservative defaults (bounded ranges enforced by the tool).
DEFAULT_INTENT_TTL_S = 30 * 86400  # 30 days
MAX_INTENT_TTL_S = 90 * 86400
MIN_INTENT_TTL_S = 300  # 5 minutes
DEFAULT_COOLDOWN_S = 3600  # 1 hour
MAX_COOLDOWN_S = 7 * 86400
MAX_FIRES = 100
DEFAULT_MAX_FIRES = 3
MAX_TERMS_PER_GROUP = 6
MAX_GROUPS = 8
# Bounded dedup window: keep the last N fired source keys in memory so a
# replayed inbound update cannot double-fire.
_DEDUP_WINDOW = 512

_PUNCT_RE = re.compile(r"[^a-z0-9\s]+")
_WS_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Normalize trusted inbound text for matching.

    Lowercases, replaces punctuation with spaces, collapses whitespace. This
    makes matching predictable across case/punctuation. Phrase boundaries are
    honored at the token level (see ``tokens``).
    """
    lowered = (text or "").lower()
    stripped = _PUNCT_RE.sub(" ", lowered)
    return _WS_RE.sub(" ", stripped).strip()


def tokens(text: str) -> list[str]:
    """Tokenize normalized text into a word sequence (empty-safe)."""
    return [t for t in normalize_text(text).split(" ") if t] if text else []


def match_trigger_groups(trigger_groups: list[list[str]], normalized_text: str) -> bool:
    """OR-of-AND matching over normalized text tokens.

    A group matches when its terms appear as an exact contiguous token
    subsequence (phrase boundaries honored). Firing requires at least one
    whole group to match. Empty triggers never match.
    """
    seq = tokens(normalized_text)
    if not seq or not trigger_groups:
        return False
    seq_len = len(seq)
    for group in trigger_groups:
        group_tokens = tokens(" ".join(group))
        if not group_tokens:
            continue
        n = len(group_tokens)
        if n > seq_len:
            continue
        for start in range(seq_len - n + 1):
            if seq[start : start + n] == group_tokens:
                return True
    return False


def source_digest(*parts: Any) -> str:
    """Stable dedup key for one inbound update."""
    raw = "|".join(str(p) for p in parts if p is not None)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


@dataclass
class StandingIntent:
    intent_id: str
    sender_id: str
    channel: str
    chat_id: str
    session_key: str
    trigger_groups: list[list[str]]
    reminder: str
    created_at_ms: int
    expires_at_ms: int
    cooldown_ms: int
    max_fires: int
    status: str = "active"  # active | cancelled | expired | exhausted
    fire_count: int = 0
    last_fired_at_ms: int | None = None

    def expired(self, now_ms: int | None = None) -> bool:
        return (now_ms if now_ms is not None else int(time.time() * 1000)) > self.expires_at_ms


class StandingIntentStore:
    """Durable intent log under ``workspace/standing_intents.json``.

    Fires are atomic read-modify-write under a process lock; terminal intents
    stay on disk (evidence retained) and are filtered out of match/fire.
    """

    def __init__(self, workspace: Path | None = None):
        self._path = (workspace or Path(".")) / "standing_intents.json"
        self._lock = threading.Lock()
        self._dedup: set[str] = set()
        self._dedup_order: list[str] = []

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> list[StandingIntent]:
        if not self._path.exists():
            return []
        data = json.loads(self._path.read_text(encoding="utf-8"))
        items = data.get("intents", []) if isinstance(data, dict) else data
        result: list[StandingIntent] = []
        for item in items:
            if isinstance(item, dict):
                item["trigger_groups"] = [
                    [str(t) for t in g] for g in (item.get("trigger_groups") or [])
                ]
                result.append(StandingIntent(**item))
        return result

    def _load_safe(self) -> list[StandingIntent]:
        try:
            return self._load()
        except (OSError, json.JSONDecodeError, TypeError):
            backup = self._path.with_suffix(self._path.suffix + f".corrupt-{int(time.time())}")
            with suppress(OSError):
                self._path.rename(backup)
            return []

    def _write(self, intents: list[StandingIntent]) -> None:
        _atomic_write(
            self._path,
            json.dumps({"version": 1, "intents": [asdict(i) for i in intents]},
                       indent=2, ensure_ascii=False),
        )

    def add(
        self,
        *,
        sender_id: str,
        channel: str,
        chat_id: str,
        session_key: str,
        trigger_groups: list[list[str]],
        reminder: str,
        ttl_ms: int,
        cooldown_ms: int,
        max_fires: int,
    ) -> StandingIntent:
        now = int(time.time() * 1000)
        intent = StandingIntent(
            intent_id=uuid.uuid4().hex[:10],
            sender_id=sender_id,
            channel=channel,
            chat_id=str(chat_id),
            session_key=session_key,
            trigger_groups=[[t.strip() for t in g if t.strip()] for g in trigger_groups],
            reminder=reminder,
            created_at_ms=now,
            expires_at_ms=now + ttl_ms,
            cooldown_ms=cooldown_ms,
            max_fires=max_fires,
        )
        with self._lock:
            intents = self._load_safe()
            intents.append(intent)
            self._write(intents)
        return intent

    def list_for_owner(self, session_key: str, *, sender_id: str | None = None) -> list[StandingIntent]:
        with self._lock:
            intents = self._load_safe()
        return [
            i for i in intents
            if i.session_key == session_key
            and (sender_id is None or i.sender_id == sender_id)
        ]

    def cancel(self, intent_id: str, *, session_key: str, sender_id: str | None = None) -> bool:
        with self._lock:
            intents = self._load_safe()
            intent = next((i for i in intents if i.intent_id == intent_id), None)
            if intent is None:
                return False
            if intent.session_key != session_key:
                return False
            if sender_id is not None and intent.sender_id != sender_id:
                return False
            if intent.status != "active":
                return False
            intent.status = "cancelled"
            self._write(intents)
            return True

    def match_and_fire(
        self,
        *,
        session_key: str,
        sender_id: str,
        source_key: str,
        text: str,
        now_ms: int | None = None,
    ) -> list[StandingIntent]:
        """Fire every eligible owned intent once, atomically.

        Returns the list of fired intents (with ``fire_count`` already
        incremented). Terminal states (expired/exhausted/cancelled) are
        resolved lazily here; duplicate ``source_key`` within the bounded
        dedup window is ignored, so replayed inbound updates cannot
        double-fire.
        """
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        normalized = normalize_text(text)
        with self._lock:
            if source_key in self._dedup:
                return []
            self._dedup.add(source_key)
            self._dedup_order.append(source_key)
            while len(self._dedup_order) > _DEDUP_WINDOW:
                self._dedup.discard(self._dedup_order.pop(0))

            intents = self._load_safe()
            fired: list[StandingIntent] = []
            changed = False
            for intent in intents:
                if intent.session_key != session_key or intent.sender_id != sender_id:
                    continue
                if intent.status != "active":
                    continue
                if intent.expired(now):
                    intent.status = "expired"
                    changed = True
                    continue
                if (
                    intent.last_fired_at_ms is not None
                    and now - intent.last_fired_at_ms < intent.cooldown_ms
                ):
                    continue
                if intent.fire_count >= intent.max_fires:
                    if intent.fire_count > 0:
                        intent.status = "exhausted"
                        changed = True
                    continue
                if not match_trigger_groups(intent.trigger_groups, normalized):
                    continue
                intent.fire_count += 1
                intent.last_fired_at_ms = now
                if intent.fire_count >= intent.max_fires:
                    intent.status = "exhausted"
                changed = True
                fired.append(intent)
            if changed:
                self._write(intents)
            return list(fired)


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


@tool_parameters(
    tool_parameters_schema(
        action=StringSchema("Action: add, list, or cancel.", enum=["add", "list", "cancel"]),
        reminder=StringSchema(
            "REQUIRED for add: the reminder text to surface when the trigger matches.",
            min_length=1,
            max_length=500,
        ),
        trigger_terms=ArraySchema(
            items=StringSchema("One trigger phrase/keyword term"),
            description=(
                "REQUIRED for add. Groups of terms with OR-of-AND semantics: "
                "pass multiple groups (each as its own list) via trigger_groups "
                "or a single list here (one group)."
            ),
            min_items=1,
            max_items=MAX_TERMS_PER_GROUP,
        ),
        trigger_groups=ArraySchema(
            items=ArraySchema(items=StringSchema("term")),
            description="Explicit groups: a group matches when ALL its terms "
            "appear as a contiguous phrase; any group fires the intent.",
            max_items=MAX_GROUPS,
        ),
        expires_in_s=IntegerSchema(
            DEFAULT_INTENT_TTL_S,
            description=f"Lifetime in seconds ({MIN_INTENT_TTL_S}-{MAX_INTENT_TTL_S}; "
            f"default {DEFAULT_INTENT_TTL_S}).",
            minimum=MIN_INTENT_TTL_S,
            maximum=MAX_INTENT_TTL_S,
            nullable=True,
        ),
        cooldown_s=IntegerSchema(
            DEFAULT_COOLDOWN_S,
            description="Minimum seconds between fires (default 3600).",
            minimum=0,
            maximum=MAX_COOLDOWN_S,
            nullable=True,
        ),
        max_fires=IntegerSchema(
            DEFAULT_MAX_FIRES,
            description="Maximum number of fires before the intent exhausts (1-100).",
            minimum=1,
            maximum=MAX_FIRES,
            nullable=True,
        ),
        intent_id=StringSchema("intent id (from add/list) for cancel."),
        required=["action"],
        description=(
            "Create, list, or cancel event-conditioned reminders. The model "
            "translates natural language into explicit trigger terms; matching "
            "itself is deterministic. Never ask for secrets. Triggers only fire "
            "on trusted user text in the owner's session."
        ),
    )
)
class IntentTool(Tool, ContextAware):
    """Durable standing intents for event-conditioned reminders."""

    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "intent"

    @property
    def description(self) -> str:
        return (
            "Manage standing intents: 'when the user mentions X, remind them Y'. "
            "add/list/cancel. Matching is deterministic term matching on the "
            "user's own message; intents are scoped to the session/owner and "
            "fire at most max_fires times with a cooldown. Never use for secrets."
        )

    def __init__(self, *, workspace: Path | None = None):
        self._workspace = workspace or Path(".")
        self._channel = ""
        self._chat_id = ""
        self._session_key = ""
        self._sender_id = ""

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(workspace=Path(ctx.workspace))

    def set_context(self, ctx: RequestContext) -> None:
        self._channel = ctx.channel
        self._chat_id = str(ctx.chat_id)
        self._session_key = ctx.session_key or f"{ctx.channel}:{ctx.chat_id}"
        self._sender_id = str(ctx.sender_id or "") if ctx.sender_id is not None else ""

    def _store(self) -> StandingIntentStore:
        return StandingIntentStore(self._workspace)

    @staticmethod
    def _intent_line(intent: StandingIntent) -> str:
        groups = " OR ".join(" AND ".join(g) for g in intent.trigger_groups)
        remaining = intent.max_fires - intent.fire_count
        return (
            f"- {intent.intent_id} | when: {groups} | remind: {intent.reminder} "
            f"| fires left: {remaining} | status: {intent.status}"
        )

    async def execute(
        self,
        action: str,
        reminder: str | None = None,
        trigger_terms: list[str] | None = None,
        trigger_groups: list[list[str]] | None = None,
        expires_in_s: int | None = None,
        cooldown_s: int | None = None,
        max_fires: int | None = None,
        intent_id: str | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        store = self._store()
        if action == "add":
            if not self._session_key or not self._sender_id:
                return ToolResult.retryable_error(
                    "Error: intents require an interactive session with a known user."
                )
            if not reminder or not reminder.strip():
                return ToolResult.retryable_error("Error: reminder text is required.")
            groups = list(trigger_groups) if trigger_groups else ([list(trigger_terms)] if trigger_terms else None)
            if not groups or not groups[0]:
                return ToolResult.retryable_error(
                    "Error: trigger terms are required (pass trigger_terms or trigger_groups)."
                )
            cleaned = [[str(term).strip() for term in group if str(term).strip()] for group in groups]
            cleaned = [g for g in cleaned if g]
            if not cleaned:
                return ToolResult.retryable_error("Error: trigger groups are empty.")
            if len(cleaned) > MAX_GROUPS or any(len(g) > MAX_TERMS_PER_GROUP for g in cleaned):
                return ToolResult.retryable_error("Error: trigger group size out of bounds.")
            ttl_s = expires_in_s if expires_in_s is not None else DEFAULT_INTENT_TTL_S
            cooldown = cooldown_s if cooldown_s is not None else DEFAULT_COOLDOWN_S
            fires = max_fires if max_fires is not None else DEFAULT_MAX_FIRES
            if not (MIN_INTENT_TTL_S <= ttl_s <= MAX_INTENT_TTL_S):
                return ToolResult.retryable_error("Error: expiry out of bounds.")
            if not (0 <= cooldown <= MAX_COOLDOWN_S):
                return ToolResult.retryable_error("Error: cooldown out of bounds.")
            if not (1 <= fires <= MAX_FIRES):
                return ToolResult.retryable_error("Error: max_fires out of bounds.")

            intent = store.add(
                sender_id=self._sender_id,
                channel=self._channel,
                chat_id=self._chat_id,
                session_key=self._session_key,
                trigger_groups=cleaned,
                reminder=reminder.strip(),
                ttl_ms=ttl_s * 1000,
                cooldown_ms=cooldown * 1000,
                max_fires=fires,
            )
            return ToolResult(
                f"Standing intent added (id: {intent.intent_id}) — "
                f"'{intent.reminder}' fires when: "
                + " OR ".join(" AND ".join(g) for g in cleaned),
                data={"intent_id": intent.intent_id, "trigger_groups": cleaned},
                side_effects=[{"kind": "standing_intent_added", "intent_id": intent.intent_id, "status": "active"}],
                postcondition="checked",
            )
        if action == "list":
            intents = store.list_for_owner(self._session_key, sender_id=self._sender_id or None)
            if not intents:
                return ToolResult("No standing intents for this session.")
            return ToolResult(
                "Standing intents:\n" + "\n".join(self._intent_line(i) for i in intents),
                data={"intents": [asdict(i) for i in intents]},
            )
        if action == "cancel":
            if not intent_id:
                return ToolResult.retryable_error("Error: intent_id is required for cancel.")
            if store.cancel(intent_id, session_key=self._session_key, sender_id=self._sender_id or None):
                return ToolResult(
                    f"Cancelled standing intent {intent_id}.",
                    side_effects=[{"kind": "standing_intent_cancelled", "intent_id": intent_id}],
                    postcondition="checked",
                )
            return ToolResult.retryable_error(f"Error: intent {intent_id} not found or not cancellable.")
        return ToolResult.retryable_error("Error: unknown intent action.")
