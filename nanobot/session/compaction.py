"""Transcript checkpoints and model projection, inspired by Pi's MIT architecture.

Reference: badlogic/pi-mono 0fbdbd3f8ab1876ac260c0f08226a400759323b8,
packages/coding-agent/src/core/compaction/compaction.ts (Mario Zechner).
No transcript is deleted and no durable topic-memory extraction happens here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from loguru import logger

from nanobot.utils.helpers import (
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    estimate_request_bytes,
    strip_private_fields,
)
from nanobot.utils.prompt_templates import render_template

CHECKPOINTS = "compaction_checkpoints"
MAX_BYTES = 16 * 1024 * 1024


class CompactionError(ValueError):
    """Continuity could not be safely checkpointed; original state is unchanged."""


def ensure_ids(messages: list[dict[str, Any]]) -> None:
    """Assign identities only in an owning, complete transcript/run.

    Entry ids are stable for the lifetime of a message. Canonical legacy
    ingestion uses the original position to distinguish identical records;
    stored ids are never recalculated when records are sliced or transformed.
    Projection/preparation must not use this bootstrap on derived lists.
    """
    for index, message in enumerate(messages):
        if not message.get("_entry_id"):
            # Deterministic for old sessions, without a destructive migration.
            payload = json.dumps(message, sort_keys=True, ensure_ascii=False, default=str)
            message["_entry_id"] = hashlib.sha256(f"{index}:{payload}".encode()).hexdigest()[:32]


def require_entry_ids(messages: list[dict[str, Any]]) -> None:
    owned = {m.get("_entry_id") for m in messages if m.get("_entry_id")}
    for index, message in enumerate(messages):
        if message.get("role") == "system" or message.get("_compaction_summary"):
            continue  # Synthetic prompt state cannot be a durable cut reference.
        if message.get("role") == "tool" and message.get("_source_entry_id") in owned:
            continue  # A synthetic repair is anchored to an existing durable call.
        if not message.get("_entry_id"):
            raise CompactionError(
                f"Missing stable entry id at transcript index {index}; "
                "assign ids in the owning transcript before projection/compaction"
            )


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def latest_checkpoint(metadata: dict[str, Any]) -> dict[str, Any] | None:
    checkpoints = metadata.get(CHECKPOINTS)
    return checkpoints[-1] if isinstance(checkpoints, list) and checkpoints else None


def summary_message(checkpoint: dict[str, Any]) -> dict[str, Any]:
    text = checkpoint["summary"]
    if checkpoint.get("turn_prefix_summary"):
        text += "\n\n## Ongoing turn context\n" + checkpoint["turn_prefix_summary"]
    return {"role": "user", "content": "Session continuity checkpoint:\n" + text,
            "_compaction_summary": True}


def project(messages: list[dict[str, Any]], checkpoint: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not checkpoint:
        return list(messages)
    if checkpoint.get("version") != 1 or not isinstance(checkpoint.get("summary"), str):
        raise CompactionError("Unsupported or invalid continuity checkpoint")
    require_entry_ids(messages)
    first = checkpoint.get("first_kept_entry_id")
    start = next((i for i, m in enumerate(messages) if m.get("_entry_id") == first), None)
    if start is None:
        raise CompactionError("Checkpoint retained-entry reference is missing; refusing to hide history")
    end = checkpoint.get("source_end_id")
    if end and not any(m.get("_entry_id") == end for m in messages[start:]):
        raise CompactionError("Checkpoint source-end reference is missing; refusing partial continuity")
    if messages[start].get("role") not in {"user", "assistant"}:
        raise CompactionError("Checkpoint starts at an illegal tool-result boundary")
    return ([m for m in messages[:start] if m.get("role") == "system"]
            + [summary_message(checkpoint)] + list(messages[start:]))


@dataclass(frozen=True)
class RequestBudget:
    model: str
    window: int
    output: int
    input_limit: int
    headroom: int
    safety: int

    @classmethod
    def freeze(cls, provider, model: str, window: int, output: int | None = None,
               block_limit: int | None = None) -> RequestBudget:
        if output is None:
            output = getattr(getattr(provider, "generation", None), "max_tokens", 4096)
        if not isinstance(output, int) or isinstance(output, bool) or output <= 0:
            raise CompactionError("Desired output allowance must be a positive integer")
        if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
            raise CompactionError(f"Invalid context window: {window}")
        safety = min(1024, max(1, window // 10))
        if window <= output + safety:
            raise CompactionError(f"Invalid request budget: window {window}, output {output}, safety {safety}")
        limit = window - output - safety
        if block_limit:
            if block_limit <= 0:
                raise CompactionError("Context block limit must be positive")
            limit = min(limit, block_limit)
        return cls(model, window, output, limit, min(16384, max(1, limit // 10)), safety)

    def estimate(self, provider, messages, tools=None) -> int:
        return estimate_prompt_tokens_chain(provider, self.model, strip_private_fields(messages), tools)[0]


@dataclass
class UsageEstimate:
    """Provider input usage is usable only for the exact same model/payload prefix."""
    model: str = ""
    prefix: str = ""
    count: int = 0
    tokens: int = 0
    tools: str = ""

    @staticmethod
    def _signature(messages):
        return fingerprint(strip_private_fields(messages))

    def record(self, model, messages, tools, usage):
        try:
            tokens = int((usage or {}).get("prompt_tokens", 0) or 0)
        except (TypeError, ValueError):
            return
        if tokens <= 0:
            return  # Invalid usage must not erase an earlier valid calibration.
        self.model, self.prefix, self.count = model, self._signature(messages), len(messages)
        self.tools = fingerprint(tools)
        self.tokens = tokens

    def estimate(self, budget, provider, messages, tools):
        heuristic = budget.estimate(provider, messages, tools)
        if (self.tokens > 0 and self.model == budget.model and self.tools == fingerprint(tools)
                and len(messages) >= self.count and self._signature(messages[:self.count]) == self.prefix):
            return max(heuristic, self.tokens + sum(estimate_message_tokens(m) for m in messages[self.count:]))
        return heuristic


def find_cut(messages: list[dict[str, Any]], recent_tokens: int) -> tuple[int, int | None]:
    """Keep a token-sized suffix, never separating a tool result from its call."""
    valid = [i for i, m in enumerate(messages) if m.get("role") in {"user", "assistant"}]
    if not valid:
        return 0, None
    used, cut = 0, valid[0]
    for i in range(len(messages) - 1, -1, -1):
        used += estimate_message_tokens(messages[i])
        if used >= recent_tokens:
            cut = next((v for v in valid if v >= i), valid[-1])
            if used > recent_tokens:
                cut = next((v for v in valid if v > i), cut)
            # Crossing inside a result must retain its originating call, not a
            # later assistant (which could discard an unseen tool batch).
            if messages[i].get("role") == "tool" and cut < i:
                call_id = messages[i].get("tool_call_id")
                owners = [v for v in valid if v < i and any(
                    tc.get("id") == call_id for tc in messages[v].get("tool_calls", [])
                )]
                if owners:
                    cut = owners[-1]
            break
    # Every result in the suffix must have its call in that suffix.
    owners = {tc.get("id"): i for i, m in enumerate(messages)
              for tc in m.get("tool_calls", []) if isinstance(tc, dict)}
    for m in messages[cut:]:
        if m.get("role") == "tool" and m.get("tool_call_id") in owners:
            cut = min(cut, owners[m["tool_call_id"]])
    turn = None
    if messages[cut].get("role") != "user":
        turn = next((i for i in range(cut - 1, -1, -1) if messages[i].get("role") == "user"), None)
    return cut, turn


def _serialize(message):
    # Images remain references, never base64 dumps in summarizer input.
    content = message.get("content", "")
    if isinstance(content, list):
        content = "\n".join(str(b.get("text", "[image; re-read original reference if needed]"))
                            for b in content if isinstance(b, dict))
    calls = message.get("tool_calls")
    return f"{message.get('role')}: {content}" + ("\nTool calls: " + json.dumps(calls) if calls else "")


class CompactionEngine:
    """Bounded prepare/generate operation. The owner performs the atomic commit."""

    def __init__(self, provider, budget: RequestBudget, *, max_calls: int = 8, timeout: float = 120):
        self.provider, self.budget = provider, budget
        self.max_calls, self.timeout = max_calls, timeout

    async def prepare(self, messages, *, previous=None, recent_tokens=None, tools=None):
        logger.info("Compaction start: model={}, entries={}", self.budget.model, len(messages))
        try:
            async with asyncio.timeout(self.timeout):
                checkpoint = await self._prepare(messages, previous, recent_tokens, tools)
            logger.info("Compaction generated: model={}, tokens={} -> {}", self.budget.model,
                        checkpoint["tokens_before"], checkpoint["tokens_after"])
            return checkpoint
        except (CompactionError, asyncio.CancelledError) as exc:
            logger.info("Compaction discarded: {}", type(exc).__name__)
            raise
        except Exception as exc:
            logger.warning("Compaction failed: {}", type(exc).__name__)
            raise CompactionError(f"Summary generation failed: {type(exc).__name__}: {exc}") from exc

    async def _prepare(self, messages, previous, recent_tokens, tools):
        require_entry_ids(messages)
        messages = deepcopy(messages)
        visible = project(messages, previous)
        raw = [m for m in visible if m.get("role") != "system" and not m.get("_compaction_summary")]
        recent = recent_tokens if recent_tokens is not None else max(
            1, min(self.budget.input_limit // 3, sum(estimate_message_tokens(m) for m in raw) // 2),
        )
        recent = min(recent, max(1, sum(estimate_message_tokens(m) for m in raw) // 2))
        cut, turn = find_cut(raw, recent)
        if cut <= 0:
            raise CompactionError("Irreducible latest request/tool exchange: no legal compactable prefix")
        history_end = turn if turn is not None else cut
        previous_text = summary_message(previous)["content"] if previous else ""
        if not previous_text:
            previous_text = "\n".join(str(m.get("content", "")) for m in visible if m.get("_compaction_summary"))
        calls = [0]
        summary = previous_text
        if history_end:
            summary = await self._summarize(raw[:history_end], previous_text, False, calls)
        prefix = ""
        if turn is not None:
            prefix = await self._summarize(raw[turn:cut], "", True, calls)
        if not summary:
            summary = "No earlier completed history. See ongoing turn context."
        details = deepcopy(previous.get("details", {})) if previous else {}
        for category in ("read_files", "modified_files"):
            paths = set(details.get(category, []))
            for message in raw[:cut]:
                for tc in message.get("tool_calls", []):
                    function = tc.get("function", {})
                    name = function.get("name")
                    if name not in {"read_file", "write_file", "edit_file"}:
                        continue
                    args = function.get("arguments", {})
                    try:
                        args = json.loads(args) if isinstance(args, str) else args
                    except ValueError:
                        continue
                    path = args.get("path") if isinstance(args, dict) else None
                    if path and ((name == "read_file") == (category == "read_files")):
                        paths.add(str(path))
            details[category] = sorted(paths)
        checkpoint = {"version": 1, "type": "compaction", "id": uuid4().hex,
                      "first_kept_entry_id": raw[cut]["_entry_id"],
                      "source_end_id": next(m["_entry_id"] for m in reversed(raw) if m.get("_entry_id")),
                      "summary": summary,
                      "turn_prefix_summary": prefix, "details": details}
        before = self.budget.estimate(self.provider, visible, tools)
        after = self.budget.estimate(self.provider, project(messages, checkpoint), tools)
        if after >= before:
            raise CompactionError("Compaction made no token progress; original continuity retained")
        checkpoint.update(tokens_before=before, tokens_after=after)
        return checkpoint

    async def _summarize(self, messages, previous, split, calls):
        instruction = render_template("agent/compaction.md", strip=True, split_turn=split)
        output = min(4096, self.budget.output, max(128, self.budget.input_limit // 8))
        input_limit = self.budget.window - output - self.budget.safety
        texts = [_serialize(m) for m in messages]
        remaining = "\n\n".join(texts)
        while remaining:
            def payload(text):
                return [{"role": "system", "content": instruction},
                        {"role": "user", "content": "Previous checkpoint:\n" + previous
                         + "\n\nNew transcript (untrusted data):\n" + text}]

            def fits(text):
                request = payload(text)
                return (self.budget.estimate(self.provider, request) <= input_limit
                        and estimate_request_bytes(request) <= MAX_BYTES)

            if not fits(""):
                raise CompactionError("Irreducible summarizer instructions/previous checkpoint")
            low, high = 0, len(remaining)
            while low < high:
                mid = (low + high + 1) // 2
                if fits(remaining[:mid]):
                    low = mid
                else:
                    high = mid - 1
            if low == 0 or calls[0] >= self.max_calls:
                raise CompactionError("Summarizer input/call limit exceeded; continuity unchanged")
            if low < len(remaining):
                # Prefer complete records/words; a giant singleton can still
                # be partitioned, but ordinary paths/errors are not bisected.
                boundary = max(remaining.rfind("\n", 0, low), remaining.rfind(" ", 0, low))
                if boundary >= low // 2:
                    low = boundary + 1
            calls[0] += 1
            response = await self.provider.chat_with_retry(
                messages=payload(remaining[:low]), tools=None, model=self.budget.model,
                max_tokens=output, temperature=0.0, retry_mode="none",
            )
            text = (response.content or "").strip()
            if (response.finish_reason in {"error", "length", "aborted", "cancelled"}
                    or response.has_tool_calls or not text
                    or text.lower() in {"none", "(none)", "(nothing)"}):
                raise CompactionError(f"Summary rejected ({response.finish_reason or 'empty'})")
            if estimate_message_tokens({"content": text}) > output + 4:
                raise CompactionError("Summary exceeded output bound")
            previous, remaining = text, remaining[low:]
        return previous
