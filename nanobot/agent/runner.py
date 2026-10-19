"""Shared execution loop for tool-using agents."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from contextlib import suppress
from copy import copy, deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from nanobot.agent.hook import AgentHook, AgentHookContext, AgentRunHookContext
from nanobot.agent.tools.base import ToolResult, adapt_legacy_tool_result
from nanobot.agent.tools.codemode import CODEMODE_EXECUTION, CodemodeExecution
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.providers.errors import is_context_overflow
from nanobot.session.compaction import (
    CompactionEngine,
    CompactionError,
    RequestBudget,
    UsageEstimate,
    ensure_ids,
    project,
)
from nanobot.session.microcompaction import microcompact
from nanobot.utils.file_edit_events import (
    StreamingFileEditTracker,
    build_file_edit_end_event,
    build_file_edit_error_event,
    build_file_edit_start_event,
    prepare_file_edit_trackers,
)
from nanobot.utils.file_edit_events import (
    prepare_file_edit_tracker as _prepare_file_edit_tracker,
)
from nanobot.utils.helpers import (
    IncrementalThinkExtractor,
    build_assistant_message,
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    estimate_request_bytes,
    extract_reasoning,
    maybe_persist_tool_result,
    strip_private_fields,
    strip_reasoning_tags,
    strip_think,
    truncate_text,
)
from nanobot.utils.progress_events import (
    invoke_file_edit_progress,
    on_progress_accepts_file_edit_events,
)
from nanobot.utils.prompt_templates import render_template
from nanobot.utils.runtime import (
    EMPTY_FINAL_RESPONSE_MESSAGE,
    build_budget_exhausted_finalization_message,
    build_finalization_retry_message,
    build_goal_continue_message,
    build_length_recovery_message,
    ensure_nonempty_tool_result,
    is_blank_text,
    repeated_external_lookup_error,
    repeated_workspace_violation_error,
)

_DEFAULT_ERROR_MESSAGE = "Sorry, I encountered an error calling the AI model."
_ARREARAGE_ERROR_MESSAGE = (
    "The AI provider rejected the request because the API key is out of quota or the "
    "account is in arrears. Please top up / check the billing status of your API key and try again."
)
_PERSISTED_MODEL_ERROR_PLACEHOLDER = "[Assistant reply unavailable due to model error.]"
_MAX_EMPTY_RETRIES = 2
_MAX_LENGTH_RECOVERIES = 3
_MAX_INJECTIONS_PER_TURN = 3
_MAX_INJECTION_CYCLES = 5
_MAX_REQUEST_BYTES = 16 * 1024 * 1024
# Upper bound on provider-chain walks; guards duck-typed stand-ins that mint a
# fresh object per attribute access and would otherwise loop forever.
_MAX_PROVIDER_CHAIN_DEPTH = 8
# read_file is the recovery path for persisted results; exempting it prevents persist->read->persist loops.
_TOOL_RESULT_OFFLOAD_EXEMPT_TOOLS = frozenset({"read_file"})
_BACKFILL_CONTENT = "[Tool result unavailable — call was interrupted or lost]"
_PARTIAL_COMPLETION_PREFIX = (
    "Incomplete: one or more tool operations failed or could not be verified."
)
_POLICY_BLOCK_PREFIX = "Incomplete: one or more required operations were blocked by policy."
# Recorded tool-event enrichment: when record_tool_details is set, each tool
# event gains a truncated args map and a truncated rendered-result preview so
# persisted run records stay small even across 100+ iteration runs.
_RECORDED_ARG_MAX_CHARS = 200
_RECORDED_PREVIEW_MAX_CHARS = 500


class ContextBudgetExceededError(Exception):
    """The model prompt cannot fit, even after context governance."""

    def __init__(self, estimate: int, budget: int, *, unit: str = "tokens"):
        self.estimate = estimate
        self.budget = budget
        self.unit = unit
        super().__init__(f"Prompt context is too large ({estimate} {unit}; limit {budget}).")


def _truncate_recorded_text(text: str, max_chars: int) -> str:
    """Collapse to one line and truncate a recorded field with an ellipsis."""
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _truncate_recorded_value(value: Any, max_chars: int) -> Any:
    """Truncate one tool-call argument for a compact persisted event.

    Scalars pass through untouched; strings and nested JSON-like structures
    are collapsed to one line and truncated to ``max_chars``.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _truncate_recorded_text(value, max_chars)
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = str(value)
    return _truncate_recorded_text(text, max_chars)


# Backward-compatible module attribute for tests/extensions that monkeypatch
# the former single-file tracker hook. Runtime uses prepare_file_edit_trackers.
prepare_file_edit_tracker = _prepare_file_edit_tracker


@dataclass(slots=True)
class AgentRunSpec:
    """Configuration for a single agent execution."""

    initial_messages: list[dict[str, Any]]
    tools: ToolRegistry
    model: str
    max_iterations: int
    max_tool_result_chars: int
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    hook: AgentHook | None = None
    error_message: str | None = _DEFAULT_ERROR_MESSAGE
    max_iterations_message: str | None = None
    concurrent_tools: bool = False
    fail_on_tool_error: bool = False
    workspace: Path | None = None
    session_key: str | None = None
    context_window_tokens: int | None = None
    context_block_limit: int | None = None
    provider_retry_mode: str = "standard"
    progress_callback: Any | None = None
    stream_progress_deltas: bool = True
    retry_wait_callback: Any | None = None
    checkpoint_callback: Any | None = None
    injection_callback: Any | None = None
    llm_timeout_s: float | None = None
    goal_active_predicate: Callable[[], bool] | None = None
    goal_continue_message: str | None = None
    finalize_on_max_iterations: bool = True
    # When a reply would otherwise be prefixed as a false success, spend one
    # bounded turn letting the model retry the failed operation or state
    # plainly what did not happen. Set False to keep the old prefix-only
    # behaviour (one fewer LLM call on failing turns).
    verify_tool_failures: bool = True
    vision_handoff: Any = None  # nanobot.agent.vision_handoff.VisionHandoff | None
    # Persisted-run observability: enrich every tool event with a compact
    # args map and a rendered-result preview (see _tool_event).
    record_tool_details: bool = False
    compaction_callback: Any | None = None
    precompaction_callback: Any | None = None
    compaction_engine_factory: Any | None = None
    continuity_checkpoint: dict[str, Any] | None = None
    microcompact_through: str | None = None


@dataclass(slots=True)
class AgentRunResult:
    """Outcome of a shared agent execution."""

    final_content: str | None
    messages: list[dict[str, Any]]
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "completed"
    error: str | None = None
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    had_injections: bool = False


class AgentRunner:
    """Run a tool-capable LLM loop without product-layer concerns."""

    def __init__(self, provider: LLMProvider):
        self.provider = provider

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [
                    item if isinstance(item, dict) else {"type": "text", "text": str(item)}
                    for item in value
                ]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    @classmethod
    def _append_injected_messages(
        cls,
        messages: list[dict[str, Any]],
        injections: list[dict[str, Any]],
    ) -> None:
        """Append injected user messages while preserving role alternation."""
        for injection in injections:
            if (
                messages
                and injection.get("role") == "user"
                and messages[-1].get("role") == "user"
            ):
                merged = dict(messages[-1])
                merged["content"] = cls._merge_message_content(
                    merged.get("content"),
                    injection.get("content"),
                )
                messages[-1] = merged
                continue
            messages.append(injection)

    async def _try_drain_injections(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        assistant_message: dict[str, Any] | None,
        injection_cycles: int,
        *,
        phase: str = "after error",
        iteration: int | None = None,
        allow_goal_continue: bool = False,
    ) -> tuple[bool, int]:
        """Drain pending injections. Returns (should_continue, updated_cycles).

        If injections are found and we haven't exceeded _MAX_INJECTION_CYCLES,
        append them to *messages* (and emit a checkpoint if *assistant_message*
        and *iteration* are both provided) and return (True, cycles+1) so the
        caller continues the iteration loop.  Otherwise return (False, cycles).
        """
        injections: list[dict[str, Any]] = []
        real_injection = False
        if injection_cycles < _MAX_INJECTION_CYCLES:
            injections = await self._drain_injections(spec)
            real_injection = bool(injections)
        if not injections and allow_goal_continue and assistant_message is not None:
            predicate = spec.goal_active_predicate
            if predicate is not None and predicate():
                injections = [build_goal_continue_message(spec.goal_continue_message)]
        if not injections:
            return False, injection_cycles
        if real_injection:
            injection_cycles += 1
        if assistant_message is not None:
            messages.append(assistant_message)
            if iteration is not None:
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "final_response",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [],
                    },
                )
        self._append_injected_messages(messages, injections)
        if real_injection:
            logger.info(
                "Injected {} follow-up message(s) {} ({}/{})",
                len(injections), phase, injection_cycles, _MAX_INJECTION_CYCLES,
            )
        else:
            logger.info("Injected sustained-goal continuation {}", phase)
        return True, injection_cycles

    async def _drain_injections(self, spec: AgentRunSpec) -> list[dict[str, Any]]:
        """Drain pending user messages via the injection callback.

        Returns normalized user messages (capped by
        ``_MAX_INJECTIONS_PER_TURN``), or an empty list when there is
        nothing to inject. Messages beyond the cap are logged so they
        are not silently lost.
        """
        if spec.injection_callback is None:
            return []
        try:
            signature = inspect.signature(spec.injection_callback)
            accepts_limit = (
                "limit" in signature.parameters
                or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                )
            )
            if accepts_limit:
                items = await spec.injection_callback(limit=_MAX_INJECTIONS_PER_TURN)
            else:
                items = await spec.injection_callback()
        except Exception:
            logger.exception("injection_callback failed")
            return []
        if not items:
            return []
        injected_messages: list[dict[str, Any]] = []
        for item in items:
            if item is None:
                continue
            if isinstance(item, dict) and item.get("role") == "user" and "content" in item:
                if self._has_injection_content(item.get("content")):
                    injected_messages.append(item)
                continue
            if isinstance(item, dict):
                continue
            content = getattr(item, "content") if hasattr(item, "content") else str(item)
            if self._has_injection_content(content):
                injected_messages.append({"role": "user", "content": content})
        if len(injected_messages) > _MAX_INJECTIONS_PER_TURN:
            dropped = len(injected_messages) - _MAX_INJECTIONS_PER_TURN
            logger.warning(
                "Injection callback returned {} messages, capping to {} ({} dropped)",
                len(injected_messages), _MAX_INJECTIONS_PER_TURN, dropped,
            )
            injected_messages = injected_messages[:_MAX_INJECTIONS_PER_TURN]
        return injected_messages

    @staticmethod
    def _has_injection_content(content: Any) -> bool:
        if content is None:
            return False
        if isinstance(content, str):
            return bool(content.strip())
        if isinstance(content, list):
            return bool(content)
        return True

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        # Freeze the output allowance with model/window before any await. Never
        # combine a hot-reloaded provider default with an older run's window.
        if spec.context_window_tokens:
            budget = RequestBudget.freeze(self.provider, spec.model, spec.context_window_tokens,
                                          spec.max_tokens, spec.context_block_limit)
            spec = replace(spec, max_tokens=budget.output)
        hook = spec.hook or AgentHook()
        messages = deepcopy(spec.initial_messages)
        context = AgentRunHookContext(messages=deepcopy(messages))
        frozen_runner = copy(self)  # Provider swaps affect future runs only.

        try:
            await hook.before_run(context)
            result = await frozen_runner._run_core(spec, hook, messages)
        except asyncio.CancelledError as exc:
            context.messages = deepcopy(messages)
            context.stop_reason = "cancelled"
            context.error = None
            context.exception = exc
            raise
        except Exception as exc:
            context.messages = deepcopy(messages)
            context.stop_reason = "error"
            context.error = f"Error: {type(exc).__name__}: {exc}"
            context.exception = exc
            await hook.on_error(context)
            raise
        else:
            context.messages = deepcopy(result.messages)
            context.final_content = result.final_content
            context.tools_used = list(result.tools_used)
            context.usage = dict(result.usage)
            context.stop_reason = result.stop_reason
            context.error = result.error
            context.tool_events = deepcopy(result.tool_events)
            context.had_injections = result.had_injections
            context.exception = None
            if context.error is not None:
                await hook.on_error(context)
            await hook.after_run(context)
            return result
        finally:
            context.messages = deepcopy(messages)
            if context.exception is None:
                await hook.on_finally(context)
            else:
                try:
                    await hook.on_finally(context)
                except Exception:
                    logger.exception(
                        "AgentHook.on_finally error after {}",
                        context.stop_reason or "run exception",
                    )

    async def _run_core(
        self,
        spec: AgentRunSpec,
        hook: AgentHook,
        messages: list[dict[str, Any]],
    ) -> AgentRunResult:
        final_content: str | None = None
        tools_used: list[str] = []
        usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        error: str | None = None
        stop_reason = "completed"
        tool_events: list[dict[str, Any]] = []
        # Index-aligned with tool_events: what each call touched, used only to
        # decide whether a failure was later resolved (never exposed).
        tool_event_targets: list[list[str]] = []
        # At most one verification turn per run: it must not become a loop.
        verification_attempted = False
        external_lookup_counts: dict[str, int] = {}
        # Per-turn throttle for repeated attempts against the same outside target.
        workspace_violation_counts: dict[str, int] = {}
        empty_content_retries = 0
        length_recovery_count = 0
        had_injections = False
        injection_cycles = 0
        checkpoint = spec.continuity_checkpoint
        budget = (RequestBudget.freeze(self.provider, spec.model, spec.context_window_tokens,
                                       spec.max_tokens, spec.context_block_limit)
                  if spec.context_window_tokens else None)
        engine = None
        usage_estimate = UsageEstimate()
        self._request_usage_basis = None

        async def compact(recent_tokens):
            nonlocal checkpoint, engine
            if engine is None:
                if spec.compaction_engine_factory is not None:
                    engine = spec.compaction_engine_factory()
                elif budget is not None:
                    engine = CompactionEngine(self.provider, budget)
                else:
                    raise CompactionError("No context window configured for compaction")
            ensure_ids(messages)
            before_payload = await self._govern_payload(spec, messages, checkpoint)
            try:
                self._enforce_context_budget(spec, before_payload)
            except ContextBudgetExceededError as exc:
                if exc.unit == "images" or getattr(exc, "reason", "").startswith("Irreducible"):
                    raise CompactionError(getattr(exc, "reason", str(exc))) from exc
            if spec.precompaction_callback is not None:
                await spec.precompaction_callback()
            # Summaries consume stored tool results, never idle placeholders.
            # The engine bounds/chunks this larger input independently; progress
            # is still measured against the actual (microcompacted) chat payload.
            summary_payload = await self._govern_payload(
                spec, messages, checkpoint, for_compaction=True,
            ) if spec.microcompact_through else before_payload
            candidate = await engine.prepare(
                summary_payload, previous=checkpoint, recent_tokens=recent_tokens,
                tools=spec.tools.get_definitions(),
            )
            after_payload = await self._govern_payload(spec, messages, candidate)
            try:
                self._enforce_context_budget(spec, after_payload)
            except ContextBudgetExceededError as exc:
                if exc.unit == "images" or getattr(exc, "reason", "").startswith("Irreducible"):
                    raise CompactionError(getattr(exc, "reason", str(exc))) from exc
            definitions = spec.tools.get_definitions()
            if budget and (
                budget.estimate(self.provider, after_payload, definitions)
                >= budget.estimate(self.provider, before_payload, definitions)
                and estimate_request_bytes(after_payload, definitions)
                >= estimate_request_bytes(before_payload, definitions)
            ):
                raise CompactionError("Compaction made no progress in the transformed request payload")
            if spec.compaction_callback is not None:
                await spec.compaction_callback(messages, candidate)
            checkpoint = candidate

        for iteration in range(spec.max_iterations):
            if checkpoint is not None:
                ensure_ids(messages)  # Own newly completed batches before projection.
            context = AgentHookContext(
                iteration=iteration,
                messages=messages,
                session_key=spec.session_key,
            )
            await hook.before_iteration(context)
            try:
                # Recover only this interrupted continuation. Completed tool
                # batches stay in `messages`; neither tools nor prior streams run again.
                for recovery in range(4):
                    try:
                        messages_for_model = await self._govern_payload(spec, messages, checkpoint)
                        if recovery == 0 and budget and usage_estimate.estimate(
                            budget, self.provider, messages_for_model, spec.tools.get_definitions(),
                        ) >= budget.input_limit - budget.headroom:
                            previous_checkpoint = checkpoint
                            try:
                                await compact(max(1, budget.input_limit // 3))
                            except CompactionError as exc:
                                logger.info("Proactive compaction deferred: {}", exc)
                            if checkpoint is not previous_checkpoint:
                                messages_for_model = await self._govern_payload(spec, messages, checkpoint)
                        response = await self._request_model(spec, messages_for_model, hook, context)
                    except ContextBudgetExceededError as exc:
                        if exc.unit == "images" or getattr(exc, "reason", "").startswith("Irreducible"):
                            raise
                        overflow = exc
                    else:
                        if not is_context_overflow(response) or context.streamed_content:
                            break
                        overflow = ContextBudgetExceededError(
                            budget.estimate(self.provider, messages_for_model, spec.tools.get_definitions())
                            if budget else 0, budget.input_limit if budget else 0,
                        )
                    if recovery == 3 or budget is None:
                        raise overflow
                    before = budget.estimate(self.provider, messages_for_model, spec.tools.get_definitions())
                    try:
                        await compact(max(1, budget.input_limit // (3 * (2 ** recovery))))
                    except CompactionError as exc:
                        overflow.reason = str(exc)
                        raise overflow from exc
                    messages_for_model = await self._govern_payload(spec, messages, checkpoint)
                    after = budget.estimate(self.provider, messages_for_model, spec.tools.get_definitions())
                    if after >= before:
                        overflow.reason = "Checkpoint did not reduce the actual request payload"
                        raise overflow
            except ContextBudgetExceededError as exc:
                if exc.unit == "images":
                    final_content = (
                        "I could not send this request because Umans allows at most "
                        f"{exc.budget} images per request, but {exc.estimate} images "
                        "are in the current unseen batch."
                    )
                else:
                    reason = getattr(exc, "reason", "fixed instructions or latest indispensable exchange cannot fit")
                    final_content = (
                        f"Context recovery stopped: {reason} "
                        f"({exc.estimate} {exc.unit}; limit {exc.budget})."
                    )
                error = final_content
                stop_reason = "context_budget_exceeded"
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                self._append_final_message(messages, final_content)
                await hook.after_iteration(context)
                break
            # Fallback wrappers do not identify the physical responder in
            # LLMResponse, so their usage cannot safely calibrate this model.
            measured_usage = response.usage if (
                response.finish_reason != "error" and getattr(self.provider, "_primary", None) is None
            ) else {}
            sent_messages, sent_tools = self._request_usage_basis or (
                messages_for_model, spec.tools.get_definitions(),
            )
            usage_estimate.record(spec.model, sent_messages, sent_tools, measured_usage)
            self._mark_images_seen(messages)
            context.response = response
            context.tool_calls = list(response.tool_calls)

            reasoning_text, cleaned_content = extract_reasoning(
                response.reasoning_content,
                response.thinking_blocks,
                response.content,
            )
            response.content = cleaned_content
            raw_usage = self._usage_or_estimate(spec, messages_for_model, response)
            context.usage = dict(raw_usage)
            self._accumulate_usage(usage, raw_usage)
            if reasoning_text and not context.streamed_reasoning:
                await hook.emit_reasoning(reasoning_text)
                await hook.emit_reasoning_end()
                context.streamed_reasoning = True

            if response.should_execute_tools:
                context.tool_calls = list(response.tool_calls)
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=True)

                assistant_message = build_assistant_message(
                    response.content or "",
                    tool_calls=[tc.to_openai_tool_call() for tc in response.tool_calls],
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                messages.append(assistant_message)
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "awaiting_tools",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [tc.to_openai_tool_call() for tc in response.tool_calls],
                    },
                )

                await hook.before_execute_tools(context)

                results, new_events, new_event_targets, fatal_error = await self._execute_tools(
                    spec,
                    response.tool_calls,
                    external_lookup_counts,
                    workspace_violation_counts,
                )
                tool_events.extend(new_events)
                tool_event_targets.extend(new_event_targets)
                tools_used.extend(
                    event["name"] for event in new_events if event.get("status") == "success"
                )
                context.tool_results = list(results)
                # Iteration hooks pair outer calls/results by index; nested
                # operational events live in the run-level record, not that pairing.
                context.tool_events = [e for e in new_events if "parent_call_id" not in e]
                completed_tool_results: list[dict[str, Any]] = []
                for tool_call, result in zip(response.tool_calls, results):
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": self._normalize_tool_result(
                            spec,
                            tool_call.id,
                            tool_call.name,
                            result,
                        ),
                    }
                    messages.append(tool_message)
                    completed_tool_results.append(tool_message)
                if fatal_error is not None:
                    error = f"Error: {type(fatal_error).__name__}: {fatal_error}"
                    final_content = error
                    stop_reason = "tool_error"
                    self._append_final_message(messages, final_content)
                    context.final_content = final_content
                    context.error = error
                    context.stop_reason = stop_reason
                    await hook.after_iteration(context)
                    should_continue, injection_cycles = await self._try_drain_injections(
                        spec, messages, None, injection_cycles,
                        phase="after tool error",
                    )
                    if should_continue:
                        had_injections = True
                        continue
                    break
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "tools_completed",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": completed_tool_results,
                        "pending_tool_calls": [],
                    },
                )
                empty_content_retries = 0
                length_recovery_count = 0
                # Checkpoint 1: drain injections after tools, before next LLM call
                _drained, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after tool execution",
                )
                if _drained:
                    had_injections = True
                await hook.after_iteration(context)
                continue

            if response.has_tool_calls:
                logger.warning(
                    "Ignoring tool calls under finish_reason='{}' for {}",
                    response.finish_reason,
                    spec.session_key or "default",
                )

            clean = hook.finalize_content(context, response.content)
            message_tool = spec.tools.get("message")
            already_delivered = isinstance(message_tool, MessageTool) and message_tool._sent_in_turn
            if response.finish_reason != "error" and is_blank_text(clean) and not already_delivered:
                empty_content_retries += 1
                if empty_content_retries < _MAX_EMPTY_RETRIES:
                    logger.warning(
                        "Empty response on turn {} for {} ({}/{}); retrying",
                        iteration,
                        spec.session_key or "default",
                        empty_content_retries,
                        _MAX_EMPTY_RETRIES,
                    )
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=False)
                    await hook.after_iteration(context)
                    continue
                logger.warning(
                    "Empty response on turn {} for {} after {} retries; attempting finalization",
                    iteration,
                    spec.session_key or "default",
                    empty_content_retries,
                )
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=False)
                retry_messages = self._finalization_retry_messages(messages_for_model)
                response = await self._request_finalization_retry(spec, messages_for_model)
                retry_usage = self._usage_or_estimate(spec, retry_messages, response)
                self._accumulate_usage(usage, retry_usage)
                raw_usage = self._merge_usage(raw_usage, retry_usage)
                context.response = response
                context.usage = dict(raw_usage)
                context.tool_calls = list(response.tool_calls)
                clean = hook.finalize_content(context, response.content)

            if response.finish_reason == "length" and not is_blank_text(clean):
                length_recovery_count += 1
                if length_recovery_count <= _MAX_LENGTH_RECOVERIES:
                    logger.info(
                        "Output truncated on turn {} for {} ({}/{}); continuing",
                        iteration,
                        spec.session_key or "default",
                        length_recovery_count,
                        _MAX_LENGTH_RECOVERIES,
                    )
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=True)
                    messages.append(build_assistant_message(
                        clean,
                        reasoning_content=response.reasoning_content,
                        thinking_blocks=response.thinking_blocks,
                    ))
                    messages.append(build_length_recovery_message())
                    await hook.after_iteration(context)
                    continue

            assistant_message: dict[str, Any] | None = None
            if response.finish_reason != "error" and not is_blank_text(clean):
                assistant_message = build_assistant_message(
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )

            # Check for mid-turn injections BEFORE signaling stream end.
            # If injections are found we keep the stream alive (resuming=True)
            # so streaming channels don't prematurely finalize the card.
            should_continue, injection_cycles = await self._try_drain_injections(
                spec, messages, assistant_message, injection_cycles,
                phase="after final response",
                iteration=iteration,
                allow_goal_continue=True,
            )
            if should_continue:
                had_injections = True

            if hook.wants_streaming():
                await hook.on_stream_end(context, resuming=should_continue)

            if should_continue:
                await hook.after_iteration(context)
                continue

            if response.finish_reason == "error":
                if LLMProvider.is_arrearage_response(response):
                    final_content = _ARREARAGE_ERROR_MESSAGE
                else:
                    final_content = clean or spec.error_message or _DEFAULT_ERROR_MESSAGE
                stop_reason = "provider_error"
                error = final_content
                self._append_model_error_placeholder(messages)
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                await hook.after_iteration(context)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after LLM error",
                )
                if should_continue:
                    had_injections = True
                    continue
                break
            if is_blank_text(clean):
                if already_delivered:
                    # The tool reply is the final answer. Tool results may end
                    # the history; no synthetic assistant message is needed.
                    final_content = None
                    stop_reason = "delivered_via_tool"
                    error = None
                else:
                    final_content = EMPTY_FINAL_RESPONSE_MESSAGE
                    stop_reason = "empty_final_response"
                    error = final_content
                    self._append_final_message(messages, final_content)
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                await hook.after_iteration(context)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after empty response",
                )
                if should_continue:
                    had_injections = True
                    continue
                break

            terminal_tool_reason = self._terminal_tool_stop_reason(
                tool_events, tool_event_targets,
            )
            if (
                terminal_tool_reason == "partial_completion"
                and spec.verify_tool_failures
                and not verification_attempted
                # Only when a retry or a plainer statement could change the
                # outcome; an unverifiable side effect is not fixable here.
                and self._actionable_failures(
                    self._unresolved_events(tool_events, tool_event_targets)
                )
                # Leave a slot for the verification call, otherwise the loop
                # would fall through to max_iterations handling instead.
                and iteration + 1 < spec.max_iterations
            ):
                verification_attempted = True
                unresolved = self._unresolved_events(tool_events, tool_event_targets)
                actionable = self._actionable_failures(unresolved)
                # Record what the model actually said, unprefixed: the reply is
                # on hold, not rewritten, and the transcript stays honest.
                messages.append(assistant_message or build_assistant_message(
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                ))
                self._append_injected_messages(
                    messages,
                    [self._tool_failure_verification_message(actionable)],
                )
                logger.info(
                    "Requesting one verification turn after {} unresolved tool failure(s) for {}",
                    len(actionable),
                    spec.session_key or "default",
                )
                await hook.after_iteration(context)
                continue
            if terminal_tool_reason is not None:
                stop_reason = terminal_tool_reason
                clean = self._suppress_false_success(
                    clean, stop_reason, tool_events, tool_event_targets,
                )
                assistant_message = build_assistant_message(
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
            messages.append(assistant_message or build_assistant_message(
                clean,
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
            ))
            await self._emit_checkpoint(
                spec,
                {
                    "phase": "final_response",
                    "iteration": iteration,
                    "model": spec.model,
                    "assistant_message": messages[-1],
                    "completed_tool_results": [],
                    "pending_tool_calls": [],
                },
            )
            final_content = clean
            context.final_content = final_content
            context.stop_reason = stop_reason
            await hook.after_iteration(context)
            break
        else:
            stop_reason = "max_iterations"
            # Drain any remaining injections so they are appended to the
            # conversation history instead of being re-published as
            # independent inbound messages by _dispatch's finally block.
            # We include them before the no-tools finalization pass so the
            # final response can account for every known follow-up.
            drained_after_max_iterations, injection_cycles = await self._try_drain_injections(
                spec, messages, None, injection_cycles,
                phase="after max_iterations",
            )
            if drained_after_max_iterations:
                had_injections = True
            final_content = None
            if spec.finalize_on_max_iterations:
                if checkpoint is not None:
                    ensure_ids(messages)
                final_content = await self._try_finalize_after_max_iterations(
                    spec,
                    hook,
                    project(messages, checkpoint),
                    usage,
                )
            if final_content is None:
                # The forced no-tools finalization produced no final answer,
                # so the run stays incomplete with the explicit fallback.
                final_content = self._max_iterations_fallback(spec)
            else:
                # The budget-exhausted finalization produced a genuine final
                # response, so the run settles as completed instead of
                # retaining stop_reason="max_iterations" (which downstream
                # consumers announce/record as incomplete).
                stop_reason = "completed"
            self._append_final_message(messages, final_content)

        return AgentRunResult(
            final_content=final_content,
            messages=messages,
            tools_used=tools_used,
            usage=usage,
            stop_reason=stop_reason,
            error=error,
            tool_events=tool_events,
            had_injections=had_injections,
        )

    # Statuses that mean a material operation may not have landed.
    # ``nonzero_exit`` is deliberately absent: the command ran and returned, so
    # its exit code is data rather than an unresolved failure.
    # ``invalid_arguments`` is absent for the same reason at the other end: the
    # call was rejected before it touched anything, so no effect is unknown.
    _FAILURE_STATUSES = frozenset({"partial", "retryable_error"})
    # Failures a later success can supersede. Policy blocks participate so a
    # blocked call that is then satisfied by another route stops being terminal.
    _RESOLVABLE_STATUSES = frozenset({"partial", "retryable_error", "policy_block"})
    # How many unresolved failures the notice names before it elides the rest.
    _MAX_NOTICE_DETAILS = 3

    @classmethod
    def _unresolved_events(
        cls,
        tool_events: list[dict[str, Any]],
        event_targets: list[list[str]] | None = None,
    ) -> list[dict[str, Any]]:
        """Failures that no later success resolved, in call order."""
        unresolved: list[dict[str, Any]] = []
        for index, event in enumerate(tool_events):
            status = str(event.get("status") or "")
            if status not in cls._RESOLVABLE_STATUSES:
                continue
            if cls._superseded(index, event, tool_events, event_targets):
                continue
            unresolved.append(event)
        return unresolved

    @staticmethod
    def _targets_at(
        event_targets: list[list[str]] | None,
        index: int,
    ) -> set[str]:
        if not event_targets or index >= len(event_targets):
            return set()
        return {t for t in event_targets[index] if isinstance(t, str)}

    @classmethod
    def _superseded(
        cls,
        index: int,
        event: dict[str, Any],
        tool_events: list[dict[str, Any]],
        event_targets: list[list[str]] | None = None,
    ) -> bool:
        """True when a later success cleared this failure.

        Two ways to clear: the same tool succeeded again (a retry that worked),
        or another tool succeeded on the same target -- apply_patch failing and
        edit_file then writing that same file is a recovery, not a failure. An
        unrelated success never launders a failure.
        """
        name = str(event.get("name") or "")
        targets = cls._targets_at(event_targets, index)
        for later_index in range(index + 1, len(tool_events)):
            later = tool_events[later_index]
            if str(later.get("status") or "") != "success":
                continue
            if str(later.get("name") or "") == name:
                return True
            if targets & cls._targets_at(event_targets, later_index):
                return True
        return False

    @classmethod
    def _terminal_tool_stop_reason(
        cls,
        tool_events: list[dict[str, Any]],
        event_targets: list[list[str]] | None = None,
    ) -> str | None:
        statuses = {
            str(event.get("status") or "")
            for event in cls._unresolved_events(tool_events, event_targets)
        }
        if "policy_block" in statuses:
            return "policy_block"
        if statuses & cls._FAILURE_STATUSES:
            return "partial_completion"
        if any(str(event.get("status") or "") == "ask_user" for event in tool_events):
            return "ask_user"
        return None

    @classmethod
    def _suppress_false_success(
        cls,
        content: str,
        stop_reason: str,
        tool_events: list[dict[str, Any]] | None = None,
        event_targets: list[list[str]] | None = None,
    ) -> str:
        # Only false-success outcomes get a prefix; ask_user (a deliberate
        # wait-for-user pause) and other terminal reasons pass content through.
        # The first line stays byte-identical so log fingerprinting and the
        # error ledger keep tracking this class; the specifics follow on their
        # own lines, which is what makes the notice actionable.
        if stop_reason == "policy_block":
            prefix = _POLICY_BLOCK_PREFIX
        elif stop_reason == "partial_completion":
            prefix = _PARTIAL_COMPLETION_PREFIX
        else:
            return content
        details = cls._failure_details(tool_events or [], event_targets)
        if details:
            prefix = "\n".join([prefix, *details])
        return f"{prefix}\n\n{content}" if content else prefix

    @classmethod
    def _actionable_failures(
        cls,
        unresolved: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Unresolved failures a retry or a clearer statement could resolve.

        An unchecked postcondition is excluded: the side effect may well have
        happened and no tool can confirm it, so an extra turn is churn and the
        notice alone guards the claim. A failure the model can actually act on
        -- the operation could not run, or it ran and the intended effect did
        not happen -- earns the turn.
        """
        return [
            event
            for event in unresolved
            if str(event.get("status") or "") == "retryable_error"
            or str(event.get("postcondition") or "") == "failed"
            or (
                str(event.get("status") or "") == "partial"
                and not event.get("postcondition")
            )
        ]

    @classmethod
    def _tool_failure_verification_message(
        cls,
        unresolved: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """The single follow-up asking the model to verify before replying."""
        failures = cls._format_failure_lines(unresolved)
        content = render_template(
            "agent/tool_failure_verification.md",
            strip=True,
            failures="\n".join(failures),
        )
        return {"role": "user", "content": content}

    @classmethod
    def _format_failure_lines(cls, unresolved: list[dict[str, Any]]) -> list[str]:
        lines: list[str] = []
        for event in unresolved[: cls._MAX_NOTICE_DETAILS]:
            name = str(event.get("name") or "tool")
            detail = str(event.get("detail") or "").strip()
            lines.append(f"- {name}: {detail}" if detail else f"- {name}")
        remaining = len(unresolved) - len(lines)
        if remaining > 0:
            lines.append(f"- and {remaining} more")
        return lines

    @classmethod
    def _failure_details(
        cls,
        tool_events: list[dict[str, Any]],
        event_targets: list[list[str]] | None = None,
    ) -> list[str]:
        """Name the unresolved failures, bounded to keep the notice scannable."""
        return cls._format_failure_lines(
            cls._unresolved_events(tool_events, event_targets)
        )

    def _build_request_kwargs(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "messages": strip_private_fields(messages),
            "tools": tools,
            "model": spec.model,
            "retry_mode": spec.provider_retry_mode,
            "on_retry_wait": spec.retry_wait_callback,
        }
        if spec.temperature is not None:
            kwargs["temperature"] = spec.temperature
        if spec.max_tokens is not None:
            kwargs["max_tokens"] = spec.max_tokens
        if spec.reasoning_effort is not None:
            kwargs["reasoning_effort"] = spec.reasoning_effort
        return kwargs

    def _enforce_context_budget(
        self, spec: AgentRunSpec, messages: list[dict[str, Any]], *, include_tools: bool = True,
    ) -> None:
        """Reject oversized prompts immediately before any provider call."""
        tools = spec.tools.get_definitions() if include_tools else None
        messages = strip_private_fields(messages)
        request_bytes = estimate_request_bytes(messages, tools)
        if request_bytes > _MAX_REQUEST_BYTES:
            exc = ContextBudgetExceededError(request_bytes, _MAX_REQUEST_BYTES, unit="bytes")
            fixed = [m for m in messages if m.get("role") == "system"]
            if estimate_request_bytes(fixed, tools) > _MAX_REQUEST_BYTES:
                exc.reason = "Irreducible fixed instructions/tool schemas exceed request-byte limit"
            else:
                latest = next((i for i in range(len(messages) - 1, -1, -1)
                               if messages[i].get("role") in {"user", "assistant"}), len(messages))
                minimal = fixed + messages[latest:]
                if estimate_request_bytes(minimal, tools) > _MAX_REQUEST_BYTES:
                    exc.reason = "Irreducible latest request/tool exchange exceeds request-byte limit"
            raise exc
        if not spec.context_window_tokens and not spec.context_block_limit:
            return
        provider_max_tokens = getattr(getattr(self.provider, "generation", None), "max_tokens", 4096)
        max_output = spec.max_tokens if isinstance(spec.max_tokens, int) else (
            provider_max_tokens if isinstance(provider_max_tokens, int) else 4096
        )
        window_budget = (
            RequestBudget.freeze(self.provider, spec.model, spec.context_window_tokens, max_output).input_limit
            if spec.context_window_tokens else 0
        )
        budget = min(spec.context_block_limit, window_budget) if (
            spec.context_block_limit and spec.context_window_tokens
        ) else (spec.context_block_limit or window_budget)
        budget = max(0, budget)
        estimate, _ = estimate_prompt_tokens_chain(
            self.provider, spec.model, messages, tools
        )
        if estimate > budget:
            exc = ContextBudgetExceededError(estimate, budget)
            fixed, _ = estimate_prompt_tokens_chain(
                self.provider, spec.model, [m for m in messages if m.get("role") == "system"], tools,
            )
            if fixed > budget:
                exc.reason = "Irreducible fixed instructions/tool schemas exceed input-token budget"
            else:
                latest = next((i for i in range(len(messages) - 1, -1, -1)
                               if messages[i].get("role") in {"user", "assistant"}), len(messages))
                minimal = [m for m in messages[:latest] if m.get("role") == "system"] + messages[latest:]
                minimal_tokens, _ = estimate_prompt_tokens_chain(self.provider, spec.model, minimal, tools)
                if minimal_tokens > budget:
                    exc.reason = "Irreducible latest request/tool exchange (including attached images) is too large"
            raise exc

    async def _request_model(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        hook: AgentHook,
        context: AgentHookContext,
    ):
        timeout_s: float | None = spec.llm_timeout_s
        if timeout_s is None:
            # Default to a finite timeout to avoid per-session lock starvation when an LLM
            # request hangs indefinitely (e.g. gateway/network stall).
            # Set NANOBOT_LLM_TIMEOUT_S=0 to disable.
            raw = os.environ.get("NANOBOT_LLM_TIMEOUT_S", "300").strip()
            try:
                timeout_s = float(raw)
            except (TypeError, ValueError):
                timeout_s = 300.0
        if timeout_s is not None and timeout_s <= 0:
            timeout_s = None

        # Input is the governed payload built by the owning request seam.
        self._enforce_context_budget(spec, messages)
        kwargs = self._build_request_kwargs(
            spec,
            messages,
            tools=spec.tools.get_definitions(),
        )
        # Calibrate against exactly the transformed messages and schemas sent,
        # not a later registry/config snapshot or pre-handoff image context.
        self._request_usage_basis = (deepcopy(kwargs["messages"]), deepcopy(kwargs["tools"]))
        wants_streaming = hook.wants_streaming()
        wants_progress_streaming = (
            not wants_streaming
            and spec.stream_progress_deltas
            and spec.progress_callback is not None
            and getattr(self.provider, "supports_progress_deltas", False) is True
        )

        progress_state: dict[str, bool] | None = None
        live_file_edits: StreamingFileEditTracker | None = None

        if (
            spec.progress_callback is not None
            and on_progress_accepts_file_edit_events(spec.progress_callback)
        ):
            async def _emit_live_file_edits(events: list[dict[str, Any]]) -> None:
                await invoke_file_edit_progress(spec.progress_callback, events)

            live_file_edits = StreamingFileEditTracker(
                workspace=spec.workspace,
                tools=spec.tools,
                emit=_emit_live_file_edits,
            )

        async def _tool_call_delta(delta: dict[str, Any]) -> None:
            if live_file_edits is not None:
                await live_file_edits.update(delta)

        if wants_streaming:
            thinking_buf = ""

            async def _stream(delta: str) -> None:
                if delta:
                    context.streamed_content = True
                await hook.on_stream(context, delta)

            async def _thinking(delta: str) -> None:
                nonlocal thinking_buf
                if not delta:
                    return
                prev_clean = strip_reasoning_tags(thinking_buf)
                thinking_buf += delta
                new_clean = strip_reasoning_tags(thinking_buf)
                incremental = new_clean[len(prev_clean):]
                if incremental:
                    context.streamed_reasoning = True
                    await hook.emit_reasoning(incremental)

            async def _stream_recover() -> None:
                await hook.on_stream_end(context, resuming=True)

            coro = self.provider.chat_stream_with_retry(
                **kwargs,
                on_content_delta=_stream,
                on_thinking_delta=_thinking,
                on_tool_call_delta=_tool_call_delta if live_file_edits is not None else None,
                on_stream_recover=_stream_recover,
            )
        elif wants_progress_streaming:
            stream_buf = ""
            think_extractor = IncrementalThinkExtractor()
            progress_state = {"reasoning_open": False}

            async def _stream_progress(delta: str) -> None:
                nonlocal stream_buf
                if not delta:
                    return
                prev_clean = strip_think(stream_buf)
                stream_buf += delta
                new_clean = strip_think(stream_buf)
                incremental = new_clean[len(prev_clean):]

                if await think_extractor.feed(stream_buf, hook.emit_reasoning):
                    context.streamed_reasoning = True
                    progress_state["reasoning_open"] = True

                if incremental:
                    if progress_state["reasoning_open"]:
                        await hook.emit_reasoning_end()
                        progress_state["reasoning_open"] = False
                    context.streamed_content = True
                    await spec.progress_callback(incremental)

            coro = self.provider.chat_stream_with_retry(
                **kwargs,
                on_content_delta=_stream_progress,
                on_tool_call_delta=_tool_call_delta if live_file_edits is not None else None,
            )
        else:
            coro = self.provider.chat_with_retry(**kwargs)

        # Streaming requests already have provider-level idle timeouts
        # (NANOBOT_STREAM_IDLE_TIMEOUT_S). Do not also apply the outer wall-clock
        # LLM timeout here, or healthy long reasoning streams can be killed just
        # because total elapsed time exceeded NANOBOT_LLM_TIMEOUT_S.
        outer_timeout_s = None if (wants_streaming or wants_progress_streaming) else timeout_s
        try:
            response = (
                await coro if outer_timeout_s is None
                else await asyncio.wait_for(coro, timeout=outer_timeout_s)
            )
            if live_file_edits is not None:
                await live_file_edits.flush()
                if response.should_execute_tools:
                    live_file_edits.apply_final_call_ids(response.tool_calls)
                await live_file_edits.error_unmatched(
                    response.tool_calls if response.should_execute_tools else [],
                    "Tool call did not complete.",
                )
        except asyncio.TimeoutError:
            if outer_timeout_s is None:
                return LLMResponse(
                    content="Error calling LLM: stream stalled",
                    finish_reason="error",
                    error_kind="timeout",
                )
            return LLMResponse(
                content=f"Error calling LLM: timed out after {outer_timeout_s:g}s",
                finish_reason="error",
                error_kind="timeout",
            )
        if progress_state and progress_state.get("reasoning_open"):
            await hook.emit_reasoning_end()
        return response

    async def _request_finalization_retry(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ):
        retry_messages = self._finalization_retry_messages(messages)
        return await self._request_no_tools(spec, retry_messages)

    @staticmethod
    def _finalization_retry_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        retry_messages = list(messages)
        retry_messages.append(build_finalization_retry_message())
        return retry_messages

    async def _try_finalize_after_max_iterations(
        self,
        spec: AgentRunSpec,
        hook: AgentHook,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
    ) -> str | None:
        retry_messages = self._budget_exhausted_finalization_messages(messages)
        try:
            response = await self._request_no_tools(spec, retry_messages)
        except Exception:
            logger.exception(
                "Budget-exhausted finalization failed for {}; using fallback",
                spec.session_key or "default",
            )
            return None

        raw_usage = self._usage_or_estimate(spec, retry_messages, response)
        self._accumulate_usage(usage, raw_usage)
        if response.finish_reason == "error" or response.has_tool_calls:
            logger.warning(
                "Budget-exhausted finalization returned finish_reason='{}' "
                "with {} tool call(s) for {}; using fallback",
                response.finish_reason,
                len(response.tool_calls),
                spec.session_key or "default",
            )
            return None

        context = AgentHookContext(
            iteration=spec.max_iterations,
            messages=messages,
            response=response,
            usage=dict(raw_usage),
            session_key=spec.session_key,
        )
        clean = hook.finalize_content(context, response.content)
        if is_blank_text(clean):
            return None
        return clean

    async def _request_no_tools(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> LLMResponse:
        messages = await self._govern_payload(spec, messages)
        self._enforce_context_budget(spec, messages, include_tools=False)
        kwargs = self._build_request_kwargs(spec, messages, tools=None)
        return await self.provider.chat_with_retry(**kwargs)

    async def _govern_payload(self, spec, messages, checkpoint=None, *, for_compaction=False):
        """One projection: checkpoint → idle microcompact → repair → vision → images.

        Full summaries bypass only idle microcompaction, retaining stored results.

        Synthetic repair never changes the owning transcript/append boundary.
        Retry a transient repair failure once; never send an ungoverned fallback.
        """
        projected = project(messages, checkpoint)
        if not for_compaction:
            projected = microcompact(projected, spec.microcompact_through)
        for attempt in range(2):
            try:
                repaired = self._backfill_missing_tool_results(self._drop_orphan_tool_results(projected))
                break
            except Exception:
                if attempt:
                    raise
                logger.warning("Context repair failed for {}; retrying", spec.session_key or "default")
        transformed = await self._maybe_vision_handoff(spec, repaired)
        return self._prepare_umans_image_budget(transformed)

    def _is_umans_provider(self) -> bool:
        """Return whether the active provider (including fallback primary) is Umans.

        The walk is depth-bounded.  A plain attribute chain can be traversed by
        duck-typed stand-ins (test doubles, proxies) that mint a fresh object per
        attribute access, so an ``id()``-only cycle guard can loop forever and
        exhaust memory.  The bound terminates such chains.
        """
        provider: Any = self.provider
        for _ in range(_MAX_PROVIDER_CHAIN_DEPTH):
            if provider is None:
                return False
            spec = getattr(provider, "_spec", None)
            if getattr(spec, "name", None) == "umans":
                return True
            nxt = getattr(provider, "_primary", None)
            if nxt is None or nxt is provider:
                return False
            provider = nxt
        return False

    def _prepare_umans_image_budget(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self._is_umans_provider():
            return messages
        locations: list[tuple[int, int, dict[str, Any], bool]] = []
        latest_tool_batch = max(
            (idx for idx, msg in enumerate(messages)
             if msg.get("role") == "assistant" and msg.get("tool_calls")),
            default=-1,
        )
        for mi, message in enumerate(messages):
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for bi, block in enumerate(content):
                if not isinstance(block, dict) or block.get("type") != "image_url":
                    continue
                seen = bool(message.get("_images_seen")) or (
                    message.get("role") == "tool" and mi < latest_tool_batch
                )
                locations.append((mi, bi, block, seen))
        unseen = [item for item in locations if not item[3]]
        if len(unseen) > 20:
            raise ContextBudgetExceededError(len(unseen), 20, unit="images")
        excess = len(locations) - 20
        if excess <= 0:
            return messages
        # Remove oldest seen images only. Unseen images are never silently dropped.
        removable = [item for item in locations if item[3]]
        if len(removable) < excess:
            raise ContextBudgetExceededError(len(locations), 20, unit="images")
        remove = {(mi, bi) for mi, bi, _block, _seen in removable[:excess]}
        updated = deepcopy(messages)
        for mi, bi, block, _seen in locations:
            if (mi, bi) not in remove:
                continue
            meta = block.get("_meta")
            path = meta.get("path") if isinstance(meta, dict) else None
            if not path:
                path = "unknown path"
            content = updated[mi].get("content")
            if isinstance(content, list):
                content[bi] = {"type": "text", "text": f"[image: {path}; re-read via read_file({path})]"}
        return updated

    async def _maybe_vision_handoff(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Swap image blocks for text descriptions when the target model can't see."""
        vh = spec.vision_handoff
        if vh is None:
            return messages
        return await vh.transform(messages, spec.model)

    @staticmethod
    def _budget_exhausted_finalization_messages(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        retry_messages = list(messages)
        retry_messages.append(build_budget_exhausted_finalization_message())
        return retry_messages

    @staticmethod
    def _max_iterations_fallback(spec: AgentRunSpec) -> str:
        if spec.max_iterations_message:
            return spec.max_iterations_message.format(
                max_iterations=spec.max_iterations,
            )
        return render_template(
            "agent/max_iterations_message.md",
            strip=True,
            max_iterations=spec.max_iterations,
        )

    def _usage_or_estimate(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        response: LLMResponse,
    ) -> dict[str, int]:
        usage = self._usage_dict(response.usage)
        total = self._usage_total(usage)
        if total > 0:
            usage["total_tokens"] = total
            usage.setdefault("provider_tokens", total)
            return usage
        if response.finish_reason == "error":
            return {}
        return self._estimate_response_usage(spec, messages, response)

    def _estimate_response_usage(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        response: LLMResponse,
    ) -> dict[str, int]:
        try:
            tools = spec.tools.get_definitions()
        except Exception:
            tools = None
        prompt_tokens, _ = estimate_prompt_tokens_chain(self.provider, spec.model, messages, tools)
        assistant_message = build_assistant_message(
            response.content or "",
            tool_calls=[tc.to_openai_tool_call() for tc in response.tool_calls],
            reasoning_content=response.reasoning_content,
            thinking_blocks=response.thinking_blocks,
        )
        completion_tokens = estimate_message_tokens(assistant_message)
        total_tokens = max(0, prompt_tokens) + max(0, completion_tokens)
        if total_tokens <= 0:
            return {}
        return {
            "prompt_tokens": max(0, prompt_tokens),
            "completion_tokens": max(0, completion_tokens),
            "total_tokens": total_tokens,
            "estimated_tokens": total_tokens,
        }

    @staticmethod
    def _usage_dict(usage: dict[str, Any] | None) -> dict[str, int]:
        if not usage:
            return {}
        result: dict[str, int] = {}
        for key, value in usage.items():
            try:
                result[key] = int(value or 0)
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _usage_total(usage: dict[str, int]) -> int:
        return max(0, usage.get("total_tokens", 0) or (
            usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        ))

    @staticmethod
    def _accumulate_usage(target: dict[str, int], addition: dict[str, int]) -> None:
        for key, value in addition.items():
            target[key] = target.get(key, 0) + value

    @staticmethod
    def _merge_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
        merged = dict(left)
        for key, value in right.items():
            merged[key] = merged.get(key, 0) + value
        return merged

    async def _execute_tools(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
    ) -> tuple[list[Any], list[dict[str, Any]], list[list[str]], BaseException | None]:
        async def run_call(tool_call: ToolCallRequest):
            if tool_call.name != "codemode":
                return await self._run_tool(
                    spec, tool_call, external_lookup_counts, workspace_violation_counts,
                )
            children: list[tuple[int, dict[str, Any], list[str]]] = []
            child_errors: list[BaseException] = []
            serial = asyncio.Lock()

            async def dispatch(name: str, arguments: dict[str, Any], request_id: int):
                if not execution.active:
                    raise RuntimeError("The parent codemode call has ended")
                child = ToolCallRequest(
                    id=f"{tool_call.id}/codemode/{request_id}", name=name, arguments=arguments,
                )
                async def execute():
                    return await self._run_tool(
                        spec, child, external_lookup_counts, workspace_violation_counts,
                    )
                try:
                    if spec.concurrent_tools:
                        payload, event, error = await execute()
                    else:
                        async with serial:
                            payload, event, error = await execute()
                except asyncio.CancelledError:
                    cancelled = self._tool_event(
                        name, ToolResult.partial("Nested call cancelled; outcome may be unknown"),
                        execution_succeeded=False,
                    )
                    cancelled.update({"parent_call_id": tool_call.id, "call_id": child.id})
                    children.append((request_id, cancelled, self._event_targets(arguments)))
                    logger.debug("Codemode child {} {}: cancelled", child.id, name)
                    raise
                envelope = {
                    "content": str(payload) if payload is not None else "",
                    "status": event["status"],
                    **{key: event.get(key) for key in (
                        "data", "evidence", "side_effects", "postcondition", "retryable",
                        "exit_code", "stdout", "stderr",
                    )},
                }
                # Keep compact operational evidence, not unfiltered tool payloads,
                # in run records. The full structured result goes only to the script.
                compact = {key: value for key, value in event.items() if key not in (
                    "data", "stdout", "stderr",
                )}
                compact.update({"parent_call_id": tool_call.id, "call_id": child.id})
                children.append((request_id, compact, self._event_targets(arguments)))
                logger.debug("Codemode child {} {}: {}", child.id, name, event["status"])
                if error is not None:
                    child_errors.append(error)
                return envelope

            execution = CodemodeExecution(spec.tools, dispatch)
            token = CODEMODE_EXECUTION.set(execution)
            try:
                result, event, error = await self._run_tool(
                    spec, tool_call, external_lookup_counts, workspace_violation_counts,
                )
            finally:
                execution.active = False
                CODEMODE_EXECUTION.reset(token)
            if children:
                # Preserve call order and target alignment for the runner's
                # recovery/verification logic. Caught errors must not create a
                # second, synthetic failure on the orchestration envelope.
                children.sort(key=lambda item: item[0])
                event["_nested_events"] = [child for _, child, _ in children]
                event["_nested_targets"] = [targets for _, _, targets in children]
            return result, event, error or (child_errors[0] if child_errors else None)

        batches = self._partition_tool_batches(spec, tool_calls)
        tool_results: list[tuple[Any, dict[str, Any], BaseException | None]] = []
        for batch in batches:
            if spec.concurrent_tools and len(batch) > 1:
                batch_results = await asyncio.gather(*(
                    run_call(tool_call) for tool_call in batch
                ))
                tool_results.extend(batch_results)
            else:
                batch_results = []
                for tool_call in batch:
                    result = await run_call(tool_call)
                    tool_results.append(result)
                    batch_results.append(result)

        results: list[Any] = []
        events: list[dict[str, Any]] = []
        # Targets stay out of persisted events but remain index-aligned,
        # including every nested call, for cross-tool recovery matching.
        event_targets: list[list[str]] = []
        fatal_error: BaseException | None = None
        for call, (result, event, error) in zip(tool_calls, tool_results):
            results.append(result)
            children = event.pop("_nested_events", [])
            child_targets = event.pop("_nested_targets", [])
            events.append(event)
            event_targets.append(self._event_targets(call.arguments))
            events.extend(children)
            event_targets.extend(child_targets)
            if error is not None and fatal_error is None:
                fatal_error = error
        return results, events, event_targets, fatal_error

    async def _run_tool(
        self,
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
    ) -> tuple[Any, dict[str, Any], BaseException | None]:
        hint = "\n\n[Analyze the error above and try a different approach.]"
        # Enriched persistence for run records: capture compact args whenever
        # the run opts in (subagents), leaving default runs byte-identical.
        recorded_args = tool_call.arguments if spec.record_tool_details else None
        lookup_error = repeated_external_lookup_error(
            tool_call.name,
            tool_call.arguments,
            external_lookup_counts,
        )
        if lookup_error:
            outcome = ToolResult.policy_block(
                lookup_error,
                evidence=[{"kind": "policy", "rule": "repeated_external_lookup"}],
            )
            event = self._tool_event(
                tool_call.name,
                outcome,
                detail="repeated external lookup blocked",
                arguments=recorded_args,
            )
            if spec.fail_on_tool_error:
                return lookup_error + hint, event, RuntimeError(lookup_error)
            return lookup_error + hint, event, None
        prepare_call = getattr(spec.tools, "prepare_call", None)
        tool, params, prep_error = None, tool_call.arguments, None
        if callable(prepare_call):
            with suppress(Exception):
                prepared = prepare_call(tool_call.name, tool_call.arguments)
                if isinstance(prepared, tuple) and len(prepared) == 3:
                    tool, params, prep_error = prepared
        if prep_error:
            # Schema/parameter rejection: the call never reached the tool, so it
            # is a malformed call rather than a failed operation.
            outcome = ToolResult.invalid_arguments(prep_error)
            event = self._tool_event(
                tool_call.name,
                outcome,
                detail=prep_error.split(": ", 1)[-1][:120],
                execution_succeeded=False,
                arguments=recorded_args,
            )
            handled = self._classify_violation(
                raw_text=prep_error,
                soft_payload=prep_error + hint,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            return prep_error + hint, event, (
                RuntimeError(prep_error) if spec.fail_on_tool_error else None
            )
        evaluate_policy = getattr(spec.tools, "evaluate_policy", None)
        if tool is not None and callable(evaluate_policy):
            decision = evaluate_policy(tool, params)
            if decision.outcome != "allow":
                action = "requires explicit approval" if decision.outcome == "ask" else "was denied"
                reason = f": {decision.reason}" if decision.reason else ""
                approval_hint = ""
                if decision.outcome == "ask" and decision.approval_token:
                    approval_hint = (
                        f" Approve with `/policy approve {decision.approval_token}` "
                        f"or deny with `/policy deny {decision.approval_token}`."
                    )
                message = (
                    f"Policy {action} for tool '{tool_call.name}'"
                    f" (rule: {decision.rule_id or 'unnamed'}){reason}.{approval_hint}"
                )
                data: dict[str, Any] = {
                    "decision": decision.outcome,
                    "rule_id": decision.rule_id,
                    "resource": decision.resource,
                }
                evidence: dict[str, Any] = {
                    "kind": "tool_policy",
                    "decision": decision.outcome,
                    "rule_id": decision.rule_id,
                }
                if decision.approval_token:
                    data["approval_token"] = decision.approval_token
                    evidence["approval_token"] = decision.approval_token
                outcome = ToolResult.policy_block(
                    message,
                    data=data,
                    evidence=[evidence],
                )
                return str(outcome), self._tool_event(
                    tool_call.name,
                    outcome,
                    execution_succeeded=False,
                    arguments=recorded_args,
                ), None
        emit_file_edit_events = (
            spec.progress_callback is not None
            and on_progress_accepts_file_edit_events(spec.progress_callback)
        )
        progress_callback = spec.progress_callback if emit_file_edit_events else None
        file_edit_trackers = (
            prepare_file_edit_trackers(
                call_id=tool_call.id,
                tool_name=tool_call.name,
                tool=tool,
                workspace=spec.workspace,
                params=params if isinstance(params, dict) else None,
            )
            if progress_callback is not None
            else None
        )
        if file_edit_trackers and progress_callback is not None:
            await invoke_file_edit_progress(
                progress_callback,
                [build_file_edit_start_event(
                    file_edit_tracker,
                    params if isinstance(params, dict) else None,
                ) for file_edit_tracker in file_edit_trackers],
            )
        try:
            exec_call_kwargs: dict[str, Any] = {}
            if getattr(spec.tools, "receipt_store", None) is not None:
                exec_call_kwargs["exec_id"] = tool_call.id
            if tool is not None and exec_call_kwargs:
                result = await spec.tools.execute(tool_call.name, params, **exec_call_kwargs)
            elif tool is not None:
                result = await tool.execute(**params)
            else:
                result = await spec.tools.execute(tool_call.name, params, **exec_call_kwargs)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if file_edit_trackers and progress_callback is not None:
                await invoke_file_edit_progress(
                    progress_callback,
                    [
                        build_file_edit_error_event(file_edit_tracker, str(exc))
                        for file_edit_tracker in file_edit_trackers
                    ],
                )
            payload = f"Error: {type(exc).__name__}: {exc}"
            outcome = ToolResult(
                payload,
                status="fatal_error" if spec.fail_on_tool_error else "retryable_error",
            )
            event = self._tool_event(
                tool_call.name,
                outcome,
                detail=str(exc),
                execution_succeeded=False,
                arguments=recorded_args,
            )
            handled = self._classify_violation(
                raw_text=str(exc),
                # Preserve legacy exception payloads without the retry hint.
                soft_payload=payload,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            if spec.fail_on_tool_error:
                return payload, event, exc
            return payload, event, None

        outcome = adapt_legacy_tool_result(result)
        if outcome.status != "success":
            if file_edit_trackers and progress_callback is not None:
                await invoke_file_edit_progress(
                    progress_callback,
                    [
                        build_file_edit_error_event(file_edit_tracker, str(outcome))
                        for file_edit_tracker in file_edit_trackers
                    ],
                )
            event = self._tool_event(
                tool_call.name, outcome, arguments=recorded_args,
            )
            handled = self._classify_violation(
                raw_text=str(outcome),
                soft_payload=str(outcome) + hint,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            payload = str(outcome)
            if outcome.retryable:
                payload += hint
            fatal = outcome.status == "fatal_error" or spec.fail_on_tool_error
            return payload, event, RuntimeError(str(outcome)) if fatal else None

        if file_edit_trackers and progress_callback is not None:
            await invoke_file_edit_progress(
                progress_callback,
                [build_file_edit_end_event(
                    file_edit_tracker,
                    params if isinstance(params, dict) else None,
                ) for file_edit_tracker in file_edit_trackers],
            )

        payload = str(outcome) if isinstance(result, ToolResult) else result
        return payload, self._tool_event(
            tool_call.name, outcome, arguments=recorded_args,
        ), None

    # Argument keys that identify what a call touched. Matched across tools so
    # that a failure recovered through a different tool on the same target
    # (apply_patch -> edit_file on one file) resolves instead of lingering.
    _EVENT_TARGET_KEYS: tuple[str, ...] = (
        "path",
        "file_path",
        "file",
        "session_id",
        "job_id",
        "intent_id",
    )
    _EVENT_TARGET_MAX_ITEMS = 4

    @classmethod
    def _event_targets(cls, arguments: Any) -> list[str]:
        """Identifying values this call touched, in a bounded, JSON-safe form."""
        if not isinstance(arguments, dict):
            return []
        targets: list[str] = []

        def _add(value: Any) -> None:
            if isinstance(value, str) and value.strip():
                cleaned = value.strip()[:_RECORDED_ARG_MAX_CHARS]
                if cleaned not in targets:
                    targets.append(cleaned)

        for key in cls._EVENT_TARGET_KEYS:
            _add(arguments.get(key))
        edits = arguments.get("edits")
        if isinstance(edits, list):
            for item in edits:
                if isinstance(item, dict):
                    _add(item.get("path"))
        channel = arguments.get("channel")
        chat_id = arguments.get("chat_id")
        if isinstance(channel, str) and isinstance(chat_id, (str, int)):
            _add(f"{channel}:{chat_id}")
        return targets[: cls._EVENT_TARGET_MAX_ITEMS]

    @staticmethod
    def _tool_event(
        name: str,
        outcome: ToolResult,
        *,
        detail: str | None = None,
        execution_succeeded: bool = True,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        rendered = detail if detail is not None else str(outcome)
        rendered = rendered.replace("\n", " ").strip()
        if not rendered:
            rendered = "(empty)"
        elif len(rendered) > 120:
            rendered = rendered[:120] + "..."
        event: dict[str, Any] = {
            "name": name,
            "status": outcome.status,
            "detail": rendered,
            "execution_succeeded": execution_succeeded,
            "operational_success": outcome.operational_success,
            "verified": outcome.verified,
            "retryable": outcome.retryable,
            "postcondition": outcome.postcondition,
        }
        if arguments is not None:
            # Enriched persistence (subagent run records): compact args + a
            # truncated preview of the rendered result. Additive keys; older
            # consumers that only read name/status/detail are unaffected.
            args: dict[str, Any] = {}
            if isinstance(arguments, dict):
                args = {
                    str(key): _truncate_recorded_value(value, _RECORDED_ARG_MAX_CHARS)
                    for key, value in arguments.items()
                }
            else:
                args["raw"] = _truncate_recorded_text(
                    str(arguments), _RECORDED_ARG_MAX_CHARS,
                )
            event["args"] = args
            event["preview"] = _truncate_recorded_text(
                str(outcome), _RECORDED_PREVIEW_MAX_CHARS,
            )
        if outcome.data is not None:
            event["data"] = outcome.data
        if outcome.evidence:
            event["evidence"] = outcome.evidence
        if outcome.side_effects:
            event["side_effects"] = outcome.side_effects
        if outcome.exit_code is not None:
            event["exit_code"] = outcome.exit_code
        if outcome.stdout is not None:
            event["stdout"] = outcome.stdout
        if outcome.stderr is not None:
            event["stderr"] = outcome.stderr
        return event

    # SSRF is a hard security block at the tool boundary, but the agent turn
    # should recover conversationally instead of aborting the runtime.
    _SSRF_MARKERS: tuple[str, ...] = (
        "internal/private url detected",
        "private/internal address",
        "private address",
    )
    _SSRF_BOUNDARY_NOTE: str = (
        "This is a non-bypassable security boundary. Stop trying to access "
        "private/internal URLs. Do not retry with curl, wget, encoded IPs, "
        "alternate DNS, redirects, proxies, or another tool. Ask the user for "
        "local files, logs, screenshots, or an explicit safe public URL instead. "
        "If the user explicitly trusts this private URL, ask them to whitelist "
        "the exact IP/CIDR via tools.ssrfWhitelist."
    )

    # Non-SSRF boundary markers returned to the LLM as recoverable tool errors.
    _WORKSPACE_VIOLATION_MARKERS: tuple[str, ...] = (
        "outside the configured workspace",
        "outside allowed directory",
        "working_dir is outside",
        "working_dir could not be resolved",
        "path outside working dir",
        "path traversal detected",
    )

    @classmethod
    def _is_ssrf_violation(cls, text: str) -> bool:
        if not text:
            return False
        lowered = text.lower()
        return any(marker in lowered for marker in cls._SSRF_MARKERS)

    @classmethod
    def _is_workspace_violation(cls, text: str) -> bool:
        """True when *text* looks like any policy boundary rejection."""
        if not text:
            return False
        lowered = text.lower()
        if cls._is_ssrf_violation(lowered):
            return True
        return any(marker in lowered for marker in cls._WORKSPACE_VIOLATION_MARKERS)

    def _classify_violation(
        self,
        *,
        raw_text: str,
        soft_payload: str,
        event: dict[str, Any],
        tool_call: ToolCallRequest,
        workspace_violation_counts: dict[str, int],
    ) -> tuple[Any, dict[str, Any], BaseException | None] | None:
        """Classify safety-boundary failures, or return ``None`` to pass through."""
        if self._is_ssrf_violation(raw_text):
            logger.warning(
                "Tool {} blocked by SSRF guard; returning non-retryable tool error: {}",
                tool_call.name,
                raw_text.replace("\n", " ").strip()[:200],
            )
            event["detail"] = self._event_detail("ssrf_violation: ", raw_text)
            event.update({"status": "policy_block", "retryable": False})
            return self._ssrf_soft_payload(raw_text), event, None

        if self._is_workspace_violation(raw_text):
            escalation = repeated_workspace_violation_error(
                tool_call.name,
                tool_call.arguments,
                workspace_violation_counts,
            )
            event["detail"] = self._event_detail("workspace_violation: ", raw_text)
            event.update({"status": "policy_block", "retryable": False})
            if escalation is not None:
                logger.warning(
                    "Tool {} hit workspace boundary repeatedly; escalating hint",
                    tool_call.name,
                )
                event["detail"] = self._event_detail(
                    "workspace_violation_escalated: ",
                    raw_text,
                )
                return escalation, event, None
            return soft_payload, event, None

        return None

    @classmethod
    def _ssrf_soft_payload(cls, raw_text: str) -> str:
        text = raw_text.strip() or "Error: request blocked by SSRF guard"
        return f"{text}\n\n{cls._SSRF_BOUNDARY_NOTE}"

    @staticmethod
    def _event_detail(prefix: str, text: str, limit: int = 160) -> str:
        return (prefix + text.replace("\n", " ").strip())[:limit]

    async def _emit_checkpoint(
        self,
        spec: AgentRunSpec,
        payload: dict[str, Any],
    ) -> None:
        callback = spec.checkpoint_callback
        if callback is not None:
            await callback(payload)

    @staticmethod
    def _append_final_message(messages: list[dict[str, Any]], content: str | None) -> None:
        if not content:
            return
        if (
            messages
            and messages[-1].get("role") == "assistant"
            and not messages[-1].get("tool_calls")
        ):
            if messages[-1].get("content") == content:
                return
            messages[-1] = build_assistant_message(content)
            return
        messages.append(build_assistant_message(content))

    @staticmethod
    def _append_model_error_placeholder(messages: list[dict[str, Any]]) -> None:
        if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
            return
        messages.append(build_assistant_message(_PERSISTED_MODEL_ERROR_PLACEHOLDER))

    def _normalize_tool_result(
        self,
        spec: AgentRunSpec,
        tool_call_id: str,
        tool_name: str,
        result: Any,
    ) -> Any:
        result = ensure_nonempty_tool_result(tool_name, result)
        if tool_name in _TOOL_RESULT_OFFLOAD_EXEMPT_TOOLS:
            # Exempt tools bound their own output; skip generic offload and truncation.
            return result
        try:
            content = maybe_persist_tool_result(
                spec.workspace,
                spec.session_key,
                tool_call_id,
                result,
                max_chars=spec.max_tool_result_chars,
            )
        except Exception:
            logger.exception(
                "Tool result persist failed for {} in {}; using raw result",
                tool_call_id,
                spec.session_key or "default",
            )
            content = result
        if isinstance(content, str) and len(content) > spec.max_tool_result_chars:
            return truncate_text(content, spec.max_tool_result_chars)
        return content

    @staticmethod
    def _drop_orphan_tool_results(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Drop tool results that have no matching assistant tool_call earlier in the history."""
        declared: set[str] = set()
        updated: list[dict[str, Any]] | None = None
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        declared.add(str(tc["id"]))
            if role == "tool":
                tid = msg.get("tool_call_id")
                if tid and str(tid) not in declared:
                    if updated is None:
                        updated = [dict(m) for m in messages[:idx]]
                    continue
            if updated is not None:
                updated.append(dict(msg))

        if updated is None:
            return messages
        return updated

    @staticmethod
    def _backfill_missing_tool_results(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Insert synthetic error results for orphaned tool_use blocks."""
        declared: list[tuple[int, str, str]] = []  # (assistant_idx, call_id, name)
        fulfilled: set[str] = set()
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        name = ""
                        func = tc.get("function")
                        if isinstance(func, dict):
                            name = func.get("name", "")
                        declared.append((idx, str(tc["id"]), name))
            elif role == "tool":
                tid = msg.get("tool_call_id")
                if tid:
                    fulfilled.add(str(tid))

        missing = [(ai, cid, name) for ai, cid, name in declared if cid not in fulfilled]
        if not missing:
            return messages

        updated = list(messages)
        offset = 0
        for assistant_idx, call_id, name in missing:
            insert_at = assistant_idx + 1 + offset
            while insert_at < len(updated) and updated[insert_at].get("role") == "tool":
                insert_at += 1
            result = {
                "role": "tool", "tool_call_id": call_id, "name": name,
                "content": _BACKFILL_CONTENT,
            }
            source_id = messages[assistant_idx].get("_entry_id")
            if source_id:
                result["_source_entry_id"] = source_id
            updated.insert(insert_at, result)
            offset += 1
        return updated

    @staticmethod
    def _mark_images_seen(messages: list[dict[str, Any]]) -> None:
        """Mark image tool results as observed after the first model response."""
        latest = max((idx for idx, msg in enumerate(messages)
                      if msg.get("role") == "assistant" and msg.get("tool_calls")), default=-1)
        for idx in range(latest + 1, len(messages)):
            msg = messages[idx]
            if msg.get("role") == "tool" and isinstance(msg.get("content"), list) and any(
                isinstance(block, dict) and block.get("type") == "image_url"
                for block in msg["content"]
            ):
                msg["_images_seen"] = True

    def _partition_tool_batches(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
    ) -> list[list[ToolCallRequest]]:
        if not spec.concurrent_tools:
            return [[tool_call] for tool_call in tool_calls]

        batches: list[list[ToolCallRequest]] = []
        current: list[ToolCallRequest] = []
        for tool_call in tool_calls:
            get_tool = getattr(spec.tools, "get", None)
            tool = get_tool(tool_call.name) if callable(get_tool) else None
            can_batch = bool(tool and tool.concurrency_safe)
            if can_batch:
                current.append(tool_call)
                continue
            if current:
                batches.append(current)
                current = []
            batches.append([tool_call])
        if current:
            batches.append(current)
        return batches
