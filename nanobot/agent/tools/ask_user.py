"""ask_user: durable, channel-agnostic user decisions with Telegram buttons.

The agent calls ``ask_user`` for a small genuine user decision. The tool:

- persists a PendingQuestion (opaque id, channel/chat/session/sender scope,
  stable option ids, expiry) before rendering anything, so a gateway restart
  never makes an already-rendered button unsafe or ambiguous;
- sends the question through the normal outbound path with an extended button
  representation ``{label, callback_value}`` (opaque ids, never the visible
  label — Telegram caps callback_data at 64 bytes);
- returns a terminal tool result (status ``ask_user``) so the runner ends the
  turn without parking a coroutine. The answer arrives as a new inbound turn.

Rich adapters (Telegram) claim the question atomically on button click and
enqueue a structured inbound answer. Channels without rich controls see
numbered options in the message text and handle the reply as an ordinary
text turn.

This is intentionally NOT a durable workflow engine: asking ends the turn;
suspension/resumption of the model coroutine is out of scope.
"""

from __future__ import annotations

import inspect
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from nanobot.agent.tools._durable_store import DurableJsonStore
from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ContextAware, RequestContext
from nanobot.agent.tools.schema import (
    ArraySchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.bus.events import OutboundMessage

# Deep-linking prefix for Telegram callback_data (opaque question+option ids).
QUESTION_CALLBACK_PREFIX = "pq:"
# Maximum answer lifetime when the caller does not pass expires_in_s (seconds).
DEFAULT_QUESTION_TTL_S = 900
# Terminal rows (answered/expired) are kept briefly so duplicate callback taps
# still get "Already answered."; anything older is pruned on every write.
_RETAINED_TERMINAL_MS = 30 * 60 * 1000  # 30 minutes
MIN_QUESTION_TTL_S = 30
MAX_QUESTION_TTL_S = 86_400
# Terminal tool status that stops the turn after a question is rendered.
ASK_USER_STATUS = "ask_user"
# Opaque option ids are single ASCII letters (Telegram payload stays tiny).
_OPTION_IDS = ("a", "b", "c", "d")
# Simple heuristic: this tool must never be used to collect credentials.
_SECRET_HINTS = ("password", "secret", "token", "api key", "api_key", "apikey",
                 "credential", "private key", "private_key", "passphrase")


def _now_ms() -> int:
    return int(time.time() * 1000)


def question_callback_value(question_id: str, option_id: str) -> str:
    """Opaque callback payload; guaranteed to fit Telegram's 64-byte limit."""
    return f"{QUESTION_CALLBACK_PREFIX}{question_id}:{option_id}"


def parse_question_callback(payload: str | None) -> tuple[str, str] | None:
    """Parse a callback payload into ``(question_id, option_id)`` or None."""
    if not payload or not payload.startswith(QUESTION_CALLBACK_PREFIX):
        return None
    rest = payload[len(QUESTION_CALLBACK_PREFIX):]
    question_id, sep, option_id = rest.partition(":")
    if not sep or not question_id or not option_id:
        return None
    return question_id, option_id


@dataclass
class PendingQuestion:
    """One durable pending user question."""

    question_id: str
    channel: str
    chat_id: str
    session_key: str
    sender_id: str
    prompt: str
    options: list[dict[str, str]]  # [{"id": "a", "label": "..."}]
    created_at_ms: int
    expires_at_ms: int
    status: str = "pending"  # pending | answered | expired
    selected_option_id: str | None = None
    answered_at_ms: int | None = None
    answered_by: str | None = None

    def option_label(self, option_id: str) -> str | None:
        for option in self.options:
            if option.get("id") == option_id:
                return option.get("label")
        return None

    def expired(self, now_ms: int | None = None) -> bool:
        """True when the answer deadline has passed."""
        return (now_ms if now_ms is not None else _now_ms()) > self.expires_at_ms


class PendingQuestionStore:
    """Durable, atomic question log under ``workspace/pending_questions.json``.

    Every transition is a fresh read-modify-write under the shared store lock
    so two callbacks (or a callback racing a tool call) cannot double-answer.
    The file is the source of truth; corrupt input degrades to an empty store
    and terminal rows are pruned on every write so the log stays bounded.
    """

    def __init__(self, workspace: Path | None = None):
        self._store = DurableJsonStore(
            (workspace or Path(".")) / "pending_questions.json", "questions"
        )

    @property
    def path(self) -> Path:
        return self._store.path

    def _load(self) -> list[PendingQuestion]:
        questions: list[PendingQuestion] = []
        for item in self._store.load():
            options = [dict(o) for o in (item.get("options") or []) if isinstance(o, dict)]
            item["options"] = options
            try:
                questions.append(PendingQuestion(**item))
            except (TypeError, ValueError):
                continue
        return questions

    def _write(self, questions: list[PendingQuestion]) -> None:
        # Keep terminal rows only inside a short retention window (duplicate
        # taps keep answering "Already answered."), prune the rest on every
        # write so the log stays bounded.
        now = _now_ms()
        keep = [
            q for q in questions
            if q.status == "pending"
            or now - (q.answered_at_ms or q.expires_at_ms or q.created_at_ms) < _RETAINED_TERMINAL_MS
        ]
        self._store.write([asdict(q) for q in keep])

    def create(
        self,
        *,
        prompt: str,
        option_labels: list[str],
        channel: str,
        chat_id: str,
        session_key: str,
        sender_id: str,
        expires_at_ms: int | None = None,
    ) -> PendingQuestion:
        now = _now_ms()
        question = PendingQuestion(
            question_id=uuid.uuid4().hex[:10],
            channel=channel,
            chat_id=str(chat_id),
            session_key=session_key,
            sender_id=str(sender_id),
            prompt=prompt,
            options=[
                {"id": _OPTION_IDS[idx], "label": label}
                for idx, label in enumerate(option_labels[: len(_OPTION_IDS)])
            ],
            created_at_ms=now,
            expires_at_ms=expires_at_ms or (now + DEFAULT_QUESTION_TTL_S * 1000),
        )
        with self._store.lock:
            questions = self._load()
            questions.append(question)
            self._write(questions)
        return question

    def claim(
        self,
        question_id: str,
        option_id: str,
        *,
        channel: str,
        chat_id: str,
        sender_id: str,
        now_ms: int | None = None,
    ) -> tuple[bool, PendingQuestion | None]:
        """Atomically claim one answer. Returns ``(claimed, question)``.

        One status-then-scope gate: pending -> expiry -> reach -> option.
        ``claimed`` is False when the question is missing, expired, answered,
        unreachable from this channel/chat/sender, or the option is unknown.
        """
        now = now_ms if now_ms is not None else _now_ms()
        with self._store.lock:
            questions = self._load()
            question = next((q for q in questions if q.question_id == question_id), None)
            if question is None:
                return False, None
            if question.status != "pending":
                return False, question
            if question.expired(now):
                question.status = "expired"
                self._write(questions)
                return False, question
            if question.channel != channel or str(question.chat_id) != str(chat_id):
                return False, question
            if str(question.sender_id or "") != str(sender_id or ""):
                return False, question
            if option_id not in {o["id"] for o in question.options}:
                return False, question
            question.status = "answered"
            question.selected_option_id = option_id
            question.answered_at_ms = now
            question.answered_by = str(sender_id)
            self._write(questions)
            return True, question


def _build_question_text(question: PendingQuestion) -> str:
    lines = [question.prompt, ""]
    for idx, option in enumerate(question.options, start=1):
        lines.append(f"{idx}. {option['label']}")
    return "\n".join(lines).strip()


@tool_parameters(
    tool_parameters_schema(
        question=StringSchema(
            "The exact question to ask the user. One question only.",
            min_length=1,
            max_length=1000,
        ),
        options=ArraySchema(
            items=StringSchema("One answer option label"),
            description="2-4 short answer options. Labels are for display only; "
            "the answer returns a stable option id.",
            min_items=2,
            max_items=4,
        ),
        expires_in_s=IntegerSchema(
            DEFAULT_QUESTION_TTL_S,
            description="Seconds the question stays answerable (30-86400; "
            "default 900). No default answer is chosen on expiry.",
            minimum=MIN_QUESTION_TTL_S,
            maximum=MAX_QUESTION_TTL_S,
            nullable=True,
        ),
        required=["question", "options"],
        description=(
            "Ask the user ONE genuine decision and stop the turn until they "
            "answer. Renders buttons on rich channels and numbered options "
            "everywhere. Use only for real user choices; never for credentials, "
            "secrets, or routine confirmation spam."
        ),
    )
)
class AskUserTool(Tool, ContextAware):
    """Ask one durable, user-scoped question; answers arrive as a new turn."""

    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "ask_user"

    @property
    def description(self) -> str:
        return (
            "Ask the user ONE genuine decision and end the current turn until "
            "they answer. The question is rendered with buttons on rich "
            "channels (Telegram) and with numbered options everywhere else; "
            "answers arrive as a new structured turn. Use sparingly for real "
            "user decisions — never for credentials, secrets, or routine "
            "confirmation spam."
        )

    def __init__(
        self,
        *,
        workspace: Path | None = None,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
    ):
        self._workspace = workspace or Path(".")
        self._send_callback = send_callback
        self._channel: str = ""
        self._chat_id: str = ""
        self._session_key: str = ""
        self._sender_id: str = ""

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(
            workspace=Path(ctx.workspace),
            send_callback=ctx.bus.publish_outbound if ctx.bus else None,
        )

    def set_context(self, ctx: RequestContext) -> None:
        self._channel = ctx.channel
        self._chat_id = str(ctx.chat_id)
        self._session_key = ctx.session_key or f"{ctx.channel}:{ctx.chat_id}"
        self._sender_id = str(ctx.sender_id or "") if ctx.sender_id is not None else ""

    def _store(self) -> PendingQuestionStore:
        return PendingQuestionStore(self._workspace)

    async def execute(
        self,
        question: str,
        options: list[str],
        expires_in_s: int | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        if not self._channel or not self._chat_id:
            return ToolResult.retryable_error(
                "Error: ask_user requires an interactive chat turn."
            )
        if not self._send_callback:
            return ToolResult.retryable_error(
                "Error: outbound message bus unavailable."
            )
        if not self._sender_id:
            return ToolResult.retryable_error(
                "Error: ask_user requires a known user (no sender identity)."
            )
        lowered = question.lower()
        if any(hint in lowered for hint in _SECRET_HINTS):
            return ToolResult.policy_block(
                "Refused: ask_user must never collect credentials or secrets. "
                "Handle these outside the chat."
            )

        option_labels = [str(label).strip() for label in (options or [])]
        option_labels = [label for label in option_labels if label]
        if len(option_labels) < 2 or len(option_labels) > 4:
            return ToolResult.retryable_error(
                "Error: ask_user requires between 2 and 4 non-empty options."
            )
        if len(set(option_labels)) != len(option_labels):
            return ToolResult.retryable_error(
                "Error: option labels must be unique."
            )

        ttl_s = expires_in_s if expires_in_s is not None else DEFAULT_QUESTION_TTL_S
        store = self._store()
        pending = store.create(
            prompt=question,
            option_labels=option_labels,
            channel=self._channel,
            chat_id=self._chat_id,
            session_key=self._session_key,
            sender_id=self._sender_id,
            expires_at_ms=_now_ms() + ttl_s * 1000,
        )

        # Durable wait-and-resume (issue #29): persist the envelope closure
        # for this question BEFORE rendering anything, so a crash in either
        # order stays recoverable and an answer can resume exactly once.
        from nanobot.agent.tools.waiting_runs import WaitingRunStore

        WaitingRunStore(self._workspace).create(
            question_id=pending.question_id,
            sender_id=self._sender_id,
            channel=self._channel,
            chat_id=self._chat_id,
            session_key=self._session_key,
            expires_at_ms=pending.expires_at_ms,
        )

        buttons = [
            [
                {
                    "label": option["label"],
                    "callback_value": question_callback_value(
                        pending.question_id, option["id"]
                    ),
                }
            ]
            for option in pending.options
        ]
        outbound = OutboundMessage(
            channel=self._channel,
            chat_id=self._chat_id,
            content=_build_question_text(pending),
            buttons=buttons,
            metadata={"question_id": pending.question_id},
        )
        sent = self._send_callback(outbound)
        if inspect.isawaitable(sent):
            await sent

        return ToolResult(
            f"Question {pending.question_id} sent — awaiting your answer.",
            status=ASK_USER_STATUS,
            data={
                "question_id": pending.question_id,
                "options": [dict(o) for o in pending.options],
                "expires_at_ms": pending.expires_at_ms,
            },
            evidence=[{"kind": "pending_question", "question_id": pending.question_id}],
            side_effects=[{"kind": "question_asked", "question_id": pending.question_id}],
            postcondition="checked",
        )
