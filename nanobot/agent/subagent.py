"""Subagent manager for background task execution."""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from nanobot.agent import model_presets as preset_helpers
from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.context import (
    RequestContext,
    ToolContext,
    bind_request_context,
    reset_request_context,
)
from nanobot.agent.tools.file_state import FileStates
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.policy import ToolPolicy, effective_policy_rules
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import AgentDefaults, ToolsConfig
from nanobot.providers.base import LLMProvider
from nanobot.security.workspace_access import (
    WorkspaceScope,
    bind_workspace_scope,
    reset_workspace_scope,
    workspace_sandbox_status,
)
from nanobot.utils.helpers import truncate_text
from nanobot.utils.prompt_templates import render_template
from nanobot.utils.run_records import (
    build_usage_block,
    safe_record_name,
    write_run_record,
)

# Bounded tool-event persistence: a 121-iteration run must not balloon the
# record file, so only the most recent events are persisted (schema stays
# additive — old records without enriched fields remain readable).
MAX_PERSISTED_TOOL_EVENTS = 50


@dataclass(slots=True)
class SubagentStatus:
    """Real-time status of a running subagent."""

    task_id: str
    label: str
    task_description: str
    started_at: float          # time.monotonic()
    phase: str = "queued"
    activity: str = "waiting_for_capacity"
    iteration: int = 0
    tool_events: list = field(default_factory=list)   # [{name, status, detail}, ...]
    usage: dict = field(default_factory=dict)          # token usage
    stop_reason: str | None = None
    error: str | None = None
    effective_budgets: dict[str, Any] = field(default_factory=dict)
    context_report: dict[str, Any] = field(default_factory=dict)
    holds_capacity: bool = False
    retry_of: str | None = None


class QueueFullError(RuntimeError):
    """Structured rejection when the bounded delegated queue is saturated.

    Carries the queue length at rejection time, the 1-based position the new
    task would occupy if admitted, and the queue capacity, so the spawn tool
    can report the wait state precisely (every slot is occupied, so the task
    would have waited at ``position``).
    """

    def __init__(
        self,
        *,
        queue_length: int,
        position: int,
        capacity: int,
        would_wait: bool = True,
    ) -> None:
        self.queue_length = queue_length
        self.position = position
        self.capacity = capacity
        self.would_wait = would_wait
        super().__init__(
            f"delegated queue is full ({queue_length}/{capacity} queued; "
            f"capacity {capacity}; this task would wait at position {position})"
        )


class _SubagentHook(AgentHook):
    """Hook for subagent execution — logs tool calls and updates status."""

    def __init__(self, task_id: str, status: SubagentStatus | None = None) -> None:
        super().__init__()
        self._task_id = task_id
        self._status = status

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        if self._status is not None:
            self._status.phase = "running"
            self._status.activity = "executing_tools"
        for tool_call in context.tool_calls:
            args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
            logger.debug(
                "Subagent [{}] executing: {} with arguments: {}",
                self._task_id, tool_call.name, args_str,
            )

    async def after_iteration(self, context: AgentHookContext) -> None:
        if self._status is None:
            return
        self._status.iteration = context.iteration
        self._status.tool_events = list(context.tool_events)
        self._status.usage = dict(context.usage)
        if context.error:
            self._status.error = str(context.error)


class SubagentManager:
    """Manages background subagent execution."""

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        max_tool_result_chars: int,
        model: str | None = None,
        tools_config: ToolsConfig | None = None,
        restrict_to_workspace: bool = False,
        disabled_skills: list[str] | None = None,
        max_iterations: int | None = None,
        max_concurrent_subagents: int | None = None,
        max_queued_subagents: int | None = None,
        llm_wall_timeout_for_session: Callable[[str | None], float | None] | None = None,
        run_provider: LLMProvider | None = None,
        run_model: str | None = None,
        preset_snapshot_loader: Callable[[str], Any] | None = None,
        presets: dict[str, Any] | None = None,
        context_retrieval: Any | None = None,
    ):
        defaults = AgentDefaults()
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.tools_config = tools_config or ToolsConfig()
        self.max_tool_result_chars = max_tool_result_chars
        self.restrict_to_workspace = restrict_to_workspace
        self.disabled_skills = set(disabled_skills or [])
        self.max_iterations = (
            max_iterations
            if max_iterations is not None
            else defaults.max_tool_iterations
        )
        self.max_concurrent_subagents = (
            max_concurrent_subagents
            if max_concurrent_subagents is not None
            else defaults.max_concurrent_subagents
        )
        self.max_queued_subagents = (
            max_queued_subagents
            if max_queued_subagents is not None
            else max(4, self.max_concurrent_subagents * 4)
        )
        self._execution_slots = asyncio.Semaphore(self.max_concurrent_subagents)
        self.runner = AgentRunner(provider)
        self.run_provider = run_provider
        self.run_model = run_model
        self._preset_snapshot_loader = preset_snapshot_loader
        self._presets = presets
        self._llm_wall_timeout_for_session = llm_wall_timeout_for_session
        self.context_retrieval = context_retrieval
        # Per-run observability records: prompt, params, model/provider, usage,
        # status. Source of truth for "which model ran this subagent + what did
        # it cost". Defaults to <workspace>/subagents/.
        self.records_dir = workspace / "subagents"
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._finalizer_tasks: set[asyncio.Task[None]] = set()
        self._task_statuses: dict[str, SubagentStatus] = {}
        self._terminal_statuses: dict[str, SubagentStatus] = {}
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {task_id, ...}

    def _subagent_tools_config(self) -> ToolsConfig:
        """Build a ToolsConfig scoped for subagent use."""
        return ToolsConfig(
            exec=self.tools_config.exec,
            web=self.tools_config.web,
            file=self.tools_config.file,
            policies=list(self.tools_config.policies),
            approval=self.tools_config.approval,
            audit_mode_read_only=self.tools_config.audit_mode_read_only,
            exploration_mode_deny_mutations=self.tools_config.exploration_mode_deny_mutations,
            restrict_to_workspace=self.restrict_to_workspace,
        )

    def _build_tools(
        self,
        workspace: Path | None = None,
        tools_config: ToolsConfig | None = None,
        policy_model: str | None = None,
        policy_preset: str | None = None,
    ) -> ToolRegistry:
        """Build an isolated subagent tool registry via ToolLoader."""
        root = self.workspace if workspace is None else workspace
        cfg = tools_config if tools_config is not None else self._subagent_tools_config()
        registry = ToolRegistry(policy=ToolPolicy(
            effective_policy_rules(
                cfg.policies,
                audit_mode_read_only=cfg.audit_mode_read_only,
                exploration_mode_deny_mutations=cfg.exploration_mode_deny_mutations,
            ),
            default_context=lambda: {
                "mode": "delegated",
                "model": policy_model or self.run_model or self.model,
                "preset": policy_preset,
            },
        ))
        ctx = ToolContext(
            config=cfg,
            workspace=str(root.resolve()),
            file_state_store=FileStates(),
            workspace_sandbox=workspace_sandbox_status(
                restrict_to_workspace=cfg.restrict_to_workspace,
                workspace=root,
            ),
        )
        ToolLoader().load(ctx, registry, scope="subagent")
        return registry

    def set_provider(self, provider: LLMProvider, model: str) -> None:
        self.provider = provider
        self.model = model
        self.runner.provider = provider

    async def spawn(self, *args: Any, **kwargs: Any) -> str:
        """Spawn a subagent; returns the human confirmation line.

        The task id lives in the durable run record (``read_run_record`` /
        ``owned_record``), never parsed from prose. See ``_spawn_impl`` for
        the full signature and behaviour.
        """
        _, message = await self._spawn_impl(*args, **kwargs)
        return message

    async def _spawn_impl(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        origin_message_id: str | None = None,
        temperature: float | None = None,
        model_preset: str | None = None,
        max_iterations: int | None = None,
        context_window_tokens: int | None = None,
        max_tokens: int | None = None,
        workspace_scope: WorkspaceScope | None = None,
        origin_sender_id: str | None = None,
        retry_of: str | None = None,
    ) -> tuple[str, str]:
        """Execute one spawn; returns ``(task_id, human confirmation line)``."""
        queued_count = self.get_queued_count()
        if queued_count >= self.max_queued_subagents:
            raise QueueFullError(
                queue_length=queued_count,
                position=queued_count + 1,
                capacity=self.max_queued_subagents,
            )
        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")
        origin = {"channel": origin_channel, "chat_id": origin_chat_id, "session_key": session_key}
        if origin_sender_id is not None:
            origin["sender_id"] = str(origin_sender_id)

        provider_override: LLMProvider | None = None
        model_override: str | None = None
        preset_ctx: int | None = None
        if model_preset:
            provider_override, model_override, preset_ctx = self._resolve_preset_override(model_preset)
        effective_provider = provider_override or self.run_provider or self.provider
        effective_model = model_override or self.run_model or self.model
        effective_ctx = context_window_tokens if context_window_tokens is not None else preset_ctx
        effective_max_iterations = (
            max_iterations if max_iterations is not None else self.max_iterations
        )
        effective_max_tool_result_chars = self.max_tool_result_chars

        queued = self.get_running_count() >= self.max_concurrent_subagents
        status = SubagentStatus(
            task_id=task_id,
            label=display_label,
            task_description=task,
            started_at=time.monotonic(),
            effective_budgets={
                "max_iterations": effective_max_iterations,
                "context_window_tokens": effective_ctx,
                "max_tool_result_chars": effective_max_tool_result_chars,
                "max_tokens": max_tokens,
                "model": effective_model,
                "model_preset": model_preset,
            },
            phase="queued" if queued else "running",
            retry_of=retry_of,
        )
        self._task_statuses[task_id] = status

        # Durable from the start so /tasks and /task can see this run while
        # it is queued/running; the terminal write overwrites this record.
        self._write_run_record(
            task_id,
            task,
            display_label,
            origin,
            temperature,
            workspace_scope,
            "",
            status,
            provider=effective_provider,
            model=effective_model,
        )

        bg_task = asyncio.create_task(
            self._run_scheduled_subagent(
                task_id,
                task,
                display_label,
                origin,
                status,
                origin_message_id,
                temperature,
                workspace_scope,
                provider_override=effective_provider,
                model_override=effective_model,
                model_preset=model_preset,
                context_window_tokens=effective_ctx,
                max_iterations=effective_max_iterations,
                max_tool_result_chars=effective_max_tool_result_chars,
                max_tokens=max_tokens,
            )
        )
        self._running_tasks[task_id] = bg_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)

        def _cleanup(finished: asyncio.Task) -> None:
            if finished.cancelled() and status.phase != "cancelled":
                status.phase = "cancelled"
                status.activity = "terminal"
                status.stop_reason = "cancelled"
                cancelled_result = "Task was cancelled before execution started."
                self._write_run_record(
                    task_id,
                    task,
                    display_label,
                    origin,
                    temperature,
                    workspace_scope,
                    cancelled_result,
                    status,
                    provider=effective_provider,
                    model=effective_model,
                )
                finalizer = asyncio.create_task(self._announce_result(
                    task_id,
                    display_label,
                    task,
                    cancelled_result,
                    origin,
                    "error",
                    origin_message_id,
                    stop_reason="cancelled",
                ))
                self._finalizer_tasks.add(finalizer)
                finalizer.add_done_callback(self._finalizer_tasks.discard)
            self._running_tasks.pop(task_id, None)
            terminal_status = self._task_statuses.pop(task_id, None)
            if terminal_status is not None:
                self._terminal_statuses[task_id] = terminal_status
                while len(self._terminal_statuses) > 100:
                    self._terminal_statuses.pop(next(iter(self._terminal_statuses)))
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_key]

        bg_task.add_done_callback(_cleanup)

        logger.info("Spawned subagent [{}]: {}", task_id, display_label)
        state = "queued" if queued else "started"
        return (
            task_id,
            f"Subagent [{display_label}] {state} (id: {task_id}). "
            "I'll notify you when it completes.",
        )

    async def _run_scheduled_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        status: SubagentStatus,
        origin_message_id: str | None = None,
        temperature: float | None = None,
        workspace_scope: WorkspaceScope | None = None,
        max_tokens: int | None = None,
        **run_kwargs: Any,
    ) -> None:
        try:
            async with self._execution_slots:
                status.holds_capacity = True
                try:
                    await self._run_subagent(
                        task_id,
                        task,
                        label,
                        origin,
                        status,
                        origin_message_id,
                        temperature,
                        workspace_scope,
                        max_tokens=max_tokens,
                        **run_kwargs,
                    )
                finally:
                    status.holds_capacity = False
        except asyncio.CancelledError:
            if status.phase != "cancelled":
                status.phase = "cancelled"
                status.activity = "terminal"
                status.stop_reason = "cancelled"
                result = "Task was cancelled while queued."
                await self._announce_result(
                    task_id,
                    label,
                    task,
                    result,
                    origin,
                    "error",
                    origin_message_id,
                    stop_reason="cancelled",
                )
                self._write_run_record(
                    task_id,
                    task,
                    label,
                    origin,
                    temperature,
                    workspace_scope,
                    result,
                    status,
                    provider=run_kwargs.get("provider_override"),
                    model=run_kwargs.get("model_override"),
                )
            raise

    def _resolve_preset_override(
        self, model_preset: str
    ) -> tuple[LLMProvider, str, int | None]:
        """Resolve a named model_preset to (provider, model, context_window).

        Raises ValueError/KeyError for an unknown preset (clear message listing
        available presets) and RuntimeError if no preset loader is configured.
        """
        if self._preset_snapshot_loader is None or self._presets is None:
            raise RuntimeError(
                "model_preset override is unavailable: SubagentManager has no preset loader"
            )
        name = preset_helpers.normalize_preset_name(model_preset, self._presets)
        snap = self._preset_snapshot_loader(name)
        return snap.provider, snap.model, snap.context_window_tokens

    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        status: SubagentStatus,
        origin_message_id: str | None = None,
        temperature: float | None = None,
        workspace_scope: WorkspaceScope | None = None,
        provider_override: LLMProvider | None = None,
        model_override: str | None = None,
        model_preset: str | None = None,
        context_window_tokens: int | None = None,
        max_iterations: int | None = None,
        max_tool_result_chars: int | None = None,
        max_tokens: int | None = None,
    ) -> None:
        """Execute the subagent task and announce the result."""
        status.phase = "running"
        status.activity = "requesting_model"
        logger.info("Subagent [{}] starting task: {}", task_id, label)

        async def _on_checkpoint(payload: dict) -> None:
            status.activity = payload.get("phase", status.activity)
            status.iteration = payload.get("iteration", status.iteration)

        async def _on_retry_wait(_message: str) -> None:
            status.phase = "waiting"
            status.activity = "provider_retry"

        record_result = ""  # set in every terminal branch; pre-bound for cancel safety
        # Pre-bound so the outer finally's _write_run_record can't UnboundLocalError
        # if setup (build_tools / build_subagent_prompt / bind_workspace_scope)
        # raises before the inner try resolves them.
        run_provider = provider_override or self.run_provider or self.provider
        run_model = model_override or self.run_model or self.model
        eff_max_iterations = max_iterations if max_iterations is not None else self.max_iterations
        eff_max_tool_result_chars = (
            max_tool_result_chars
            if max_tool_result_chars is not None
            else self.max_tool_result_chars
        )
        if not status.effective_budgets:
            status.effective_budgets = {
                "max_iterations": eff_max_iterations,
                "context_window_tokens": context_window_tokens,
                "max_tool_result_chars": eff_max_tool_result_chars,
                "max_tokens": max_tokens,
                "model": run_model,
                "model_preset": model_preset,
            }
        try:
            root = workspace_scope.project_path if workspace_scope is not None else self.workspace
            cfg = None
            if workspace_scope is not None:
                cfg = self._subagent_tools_config()
                cfg.restrict_to_workspace = workspace_scope.restrict_to_workspace
            tools = self._build_tools(
                workspace=root,
                tools_config=cfg,
                policy_model=run_model,
                policy_preset=model_preset,
            )
            system_prompt = self._build_subagent_prompt(
                workspace=root,
                task=task,
                status=status,
            )
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            sess_key = origin.get("session_key")
            llm_timeout = (
                self._llm_wall_timeout_for_session(sess_key)
                if self._llm_wall_timeout_for_session
                else None
            )
            workspace_token = bind_workspace_scope(workspace_scope) if workspace_scope is not None else None
            # Delegated runs carry a deterministic interaction mode so policy
            # rules can match them (mode=delegated). Subagents are autonomous:
            # no approval store is attached, so ask outcomes keep blocking.
            request_token = bind_request_context(RequestContext(
                channel=origin.get("channel") or "system",
                chat_id=origin.get("chat_id") or "direct",
                session_key=sess_key,
                metadata={"interaction_mode": "delegated"},
            ))
            try:
                dedicated = (
                    provider_override is not None
                    and provider_override is not self.runner.provider
                )
                runner = AgentRunner(run_provider) if dedicated else self.runner
                result = await runner.run(AgentRunSpec(
                    initial_messages=messages,
                    tools=tools,
                    model=run_model,
                    temperature=temperature,
                    max_iterations=eff_max_iterations,
                    max_tool_result_chars=eff_max_tool_result_chars,
                    context_window_tokens=context_window_tokens,
                    max_tokens=max_tokens,
                    hook=_SubagentHook(task_id, status),
                    max_iterations_message="Task ended without a verified final synthesis.",
                    finalize_on_max_iterations=True,
                    record_tool_details=True,
                    error_message=None,
                    fail_on_tool_error=False,
                    checkpoint_callback=_on_checkpoint,
                    retry_wait_callback=_on_retry_wait,
                    session_key=sess_key,
                    workspace=root,
                    llm_timeout_s=llm_timeout,
                ))
            finally:
                if workspace_token is not None:
                    reset_workspace_scope(workspace_token)
                reset_request_context(request_token)
            status.stop_reason = result.stop_reason
            status.tool_events = list(result.tool_events)
            status.activity = "terminal"
            # result.usage is the authoritative token count for the run; mirror
            # it onto status so _write_run_record captures it even when the
            # after_iteration hook never fired (e.g. single-pass completion).
            if result.usage:
                status.usage = dict(result.usage)

            if result.stop_reason == "tool_error":
                status.phase = "failed"
                record_result = self._format_partial_progress(result)
                await self._announce_result(
                    task_id, label, task,
                    record_result,
                    origin, "error", origin_message_id,
                    stop_reason=result.stop_reason,
                )
            elif result.stop_reason in {"error", "provider_error"}:
                status.phase = "failed"
                record_result = result.error or "Error: subagent execution failed."
                await self._announce_result(
                    task_id, label, task,
                    record_result,
                    origin, "error", origin_message_id,
                    stop_reason=result.stop_reason,
                )
            elif result.stop_reason in {
                "partial_completion",
                "policy_block",
                "max_iterations",
                "empty_final_response",
            }:
                status.phase = "incomplete"
                record_result = result.final_content or "Task ended without verified completion."
                logger.info(
                    "Subagent [{}] ended with {}",
                    task_id,
                    result.stop_reason,
                )
                await self._announce_result(
                    task_id, label, task,
                    record_result,
                    origin, "error", origin_message_id,
                    stop_reason=result.stop_reason,
                )
            else:
                status.phase = "completed"
                record_result = result.final_content or "Task completed but no final response was generated."
                logger.info("Subagent [{}] completed successfully", task_id)
                await self._announce_result(
                    task_id, label, task, record_result, origin, "ok", origin_message_id,
                    stop_reason=result.stop_reason,
                )

        except asyncio.CancelledError:
            status.phase = "cancelled"
            status.activity = "terminal"
            status.stop_reason = "cancelled"
            record_result = "Task was cancelled before completion."
            await self._announce_result(
                task_id, label, task, record_result, origin, "error", origin_message_id,
                stop_reason="cancelled",
            )
            raise
        except Exception as e:
            status.phase = "failed"
            status.activity = "terminal"
            status.stop_reason = "error"
            status.error = str(e)
            record_result = f"Error: {e}"
            logger.exception("Subagent [{}] failed", task_id)
            await self._announce_result(
                task_id, label, task, record_result, origin, "error", origin_message_id,
                stop_reason="error",
            )
        finally:
            # One record per run, all outcomes (success/tool_error/error/exception).
            # Same writer + schema as cron run records; the kind field distinguishes.
            self._write_run_record(
                task_id, task, label, origin, temperature,
                workspace_scope, record_result, status,
                provider=run_provider,
                model=run_model,
            )

    def _record_path(self, task_id: str) -> Path:
        """Deterministic path where this run's record will be written."""
        return self.records_dir / f"{safe_record_name(task_id)}.json"

    # ------------------------------------------------------------------
    # Task-control surface (issue #27): durable, session-scoped, bounded.
    # ------------------------------------------------------------------

    _TASK_LIST_LIMIT = 20
    _TASK_CHAT_TEXT_LIMIT = 800

    @staticmethod
    def _truncate_chat(text: str | None, limit: int = _TASK_CHAT_TEXT_LIMIT) -> str:
        """Single-line-safe chat text, reusing helpers.truncate_text."""
        line = (text or "").strip().replace("\n", " ")
        return truncate_text(line, limit).replace("\n", " ")

    @classmethod
    def task_status_vocabulary(cls, record: dict[str, Any]) -> str:
        """Map a durable record to the chat status vocabulary.

        queued/running/waiting are live states; terminal states map by
        stop_reason. ``waiting`` is deliberately only derived from the
        existing waiting_for_capacity activity (never fabricated).
        """
        phase = str(record.get("phase") or "")
        if phase == "queued" or phase == "running":
            if phase == "queued" and str(record.get("activity") or "") == "waiting_for_capacity":
                return "waiting"
            return phase
        stop_reason = str(record.get("stop_reason") or "")
        if phase == "cancelled" or stop_reason == "cancelled":
            return "cancelled"
        if stop_reason == "completed" or phase == "completed":
            return "completed"
        if stop_reason in ("partial_completion", "empty_final_response", "max_iterations"):
            return "incomplete"
        if stop_reason in ("error", "provider_error", "tool_error", "policy_block", "exception"):
            return "failed"
        return "failed"

    def read_run_record(self, task_id: str) -> dict[str, Any] | None:
        """Return one durable subagent run record (disk is the source of truth)."""
        path = self._record_path(task_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or str(data.get("task_id", "")) != str(task_id):
            return None
        return data

    def owned_record(
        self,
        task_id: str,
        *,
        session_key: str,
        sender_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Ownership-gated record lookup: ``(record, None)`` or ``(None, problem)``.

        ``problem`` is ``not_found`` when there is no durable record and
        ``not_owned`` when the session or recorded sender does not match.
        One gate for cancel/retry inspection and the /task command.
        """
        record = self.read_run_record(task_id)
        if record is None:
            return None, "not_found"
        origin = record.get("origin") or {}
        if origin.get("session_key") != session_key:
            return None, "not_owned"
        rec_sender = origin.get("sender_id")
        if sender_id is not None and rec_sender and str(rec_sender) != str(sender_id):
            return None, "not_owned"
        return record, None

    def list_session_task_records(
        self,
        session_key: str,
        *,
        sender_id: str | None = None,
        limit: int = _TASK_LIST_LIMIT,
    ) -> list[dict[str, Any]]:
        """Bounded, newest-first durable records owned by *session_key*."""
        if self.records_dir is None or not self.records_dir.exists():
            return []
        records: list[dict[str, Any]] = []
        for path in self.records_dir.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            origin = record.get("origin")
            if not isinstance(origin, dict) or origin.get("session_key") != session_key:
                continue
            rec_sender = origin.get("sender_id")
            if sender_id is not None and rec_sender and str(rec_sender) != str(sender_id):
                continue
            records.append(record)
        records.sort(
            key=lambda r: int(r.get("updated_at_ms") or r.get("created_at_ms") or 0),
            reverse=True,
        )
        return records[: max(1, limit)]

    async def cancel_task(self, task_id: str, *, session_key: str) -> str:
        """Cooperatively cancel one owned task.

        Returns ``cancelled``, ``not_owned``, ``done`` (already terminal) or
        ``not_found``. Never kills external processes; cancellation is the
        same cooperative task cancellation used by ``/stop``.
        """
        task = self._running_tasks.get(task_id)
        if task is not None and not task.done():
            owned_ids = self._session_tasks.get(session_key, set())
            if task_id not in owned_ids:
                return "not_owned"
            task.cancel()
            await asyncio.gather(*[task], return_exceptions=True)
            await asyncio.sleep(0)
            if self._finalizer_tasks:
                await asyncio.gather(*list(self._finalizer_tasks), return_exceptions=True)
            return "cancelled"
        record, problem = self.owned_record(task_id, session_key=session_key)
        if problem is not None:
            return problem
        phase = str(record.get("phase") or "")
        if phase in ("queued", "running"):
            return "not_owned"  # live elsewhere in this process; defensive
        return "done"

    async def retry_task(
        self,
        task_id: str,
        *,
        session_key: str,
        sender_id: str | None = None,
    ) -> tuple[str, str]:
        """Retry a terminal task as a NEW run with explicit lineage.

        Returns ``(status, task_id)`` where status is ``created`` (new id),
        ``not_found``, ``not_owned``, ``still_active`` or ``queue_full``.
        The old record gains ``retried_by``; the new record carries
        ``retry_of``. Never re-marks the failed record as running.
        """
        # The in-process registry is authoritative for live runs; consult it
        # first so active tasks are never retried.
        task = self._running_tasks.get(task_id)
        if task is not None and not task.done():
            owned_ids = self._session_tasks.get(session_key, set())
            if task_id not in owned_ids:
                return "not_owned", task_id
            return "still_active", task_id

        record, problem = self.owned_record(
            task_id, session_key=session_key, sender_id=sender_id
        )
        if problem is not None:
            return problem, task_id

        params = record.get("params") or {}
        origin = record.get("origin") or {}
        rec_sender = origin.get("sender_id")
        try:
            new_id, _ = await self._spawn_impl(
                task=record.get("task") or "",
                label=record.get("label"),
                origin_channel=origin.get("channel", "cli"),
                origin_chat_id=origin.get("chat_id", "direct"),
                session_key=session_key,
                origin_sender_id=str(rec_sender) if rec_sender else None,
                temperature=params.get("temperature"),
                model_preset=params.get("model_preset"),
                max_iterations=params.get("max_iterations"),
                context_window_tokens=params.get("context_window_tokens"),
                max_tokens=params.get("max_tokens"),
                retry_of=task_id,
            )
        except QueueFullError:
            return "queue_full", task_id

        # Append-only lineage on the old record (never overwrite history).
        try:
            record["retried_by"] = new_id
            write_run_record(self.records_dir, task_id, record)
        except OSError:
            logger.warning("Subagent [{}] could not record retry lineage", task_id, exc_info=True)
        return "created", new_id

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
        origin_message_id: str | None = None,
        stop_reason: str | None = None,
    ) -> None:
        """Announce the subagent result to the main agent via the message bus."""
        status_text = "completed successfully" if status == "ok" else "failed"
        record_path = self._record_path(task_id)

        announce_content = render_template(
            "agent/subagent_announce.md",
            label=label,
            status_text=status_text,
            task=task,
            result=result,
            record_path=str(record_path),
        )

        # Inject as system message to trigger main agent.
        # Use session_key_override to align with the main agent's effective
        # session key (which accounts for unified sessions) so the result is
        # routed to the correct pending queue (mid-turn injection) instead of
        # being dispatched as a competing independent task.
        override = origin.get("session_key") or f"{origin['channel']}:{origin['chat_id']}"
        metadata: dict[str, Any] = {
            "injected_event": "subagent_result",
            "subagent_task_id": task_id,
            "delivery_policy": "parent",
            "subagent_result": {
                "task_id": task_id,
                "status": status,
                "stop_reason": stop_reason,
                "result": result,
                "record_path": str(record_path),
            },
        }
        if origin_message_id:
            metadata["origin_message_id"] = origin_message_id
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
            session_key_override=override,
            metadata=metadata,
        )

        await self.bus.publish_inbound(msg)
        logger.debug("Subagent [{}] announced result to {}:{}", task_id, origin['channel'], origin['chat_id'])

    @staticmethod
    def _format_partial_progress(result) -> str:
        completed = [e for e in result.tool_events if e["status"] in {"success", "ok"}]
        failure = next(
            (
                e
                for e in reversed(result.tool_events)
                if e["status"] not in {"success", "ok"}
            ),
            None,
        )
        lines: list[str] = []
        if completed:
            lines.append("Completed steps:")
            for event in completed[-3:]:
                lines.append(f"- {event['name']}: {event['detail']}")
        if failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {failure['name']}: {failure['detail']}")
        if result.error and not failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {result.error}")
        return "\n".join(lines) or (result.error or "Error: subagent execution failed.")

    def _build_subagent_prompt(
        self,
        workspace: Path | None = None,
        *,
        task: str = "",
        status: SubagentStatus | None = None,
    ) -> str:
        """Build a focused system prompt for the subagent."""
        from nanobot.agent.context import ContextBuilder
        from nanobot.agent.skills import SkillsLoader

        time_ctx = ContextBuilder._build_runtime_context(None, None)
        root = workspace or self.workspace
        skills_summary = SkillsLoader(
            root,
            disabled_skills=self.disabled_skills,
        ).build_skills_summary()
        prompt = render_template(
            "agent/subagent_system.md",
            time_ctx=time_ctx,
            workspace=str(root),
            skills_summary=skills_summary or "",
        )
        if (
            self.context_retrieval is not None
            and getattr(self.context_retrieval, "mode", "all_pinned") == "manifest"
        ):
            builder = ContextBuilder(
                root,
                disabled_skills=list(self.disabled_skills),
                context_retrieval=self.context_retrieval,
            )
            retrieved = builder.build_system_prompt(
                include_memory_recent_history=False,
                retrieval_query=task,
            )
            if status is not None:
                status.context_report = dict(builder.last_context_report)
            prompt = f"{prompt}\n\n---\n\n{retrieved}"
        elif status is not None:
            status.context_report = {
                "mode": "all_pinned",
                "sources": [],
                "totals": {},
            }
        return prompt

    async def cancel_by_session(self, session_key: str) -> int:
        """Cancel all subagents for the given session. Returns count cancelled."""
        tasks = [self._running_tasks[tid] for tid in self._session_tasks.get(session_key, [])
                 if tid in self._running_tasks and not self._running_tasks[tid].done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)
            if self._finalizer_tasks:
                await asyncio.gather(*list(self._finalizer_tasks), return_exceptions=True)
        return len(tasks)

    def get_running_count(self) -> int:
        """Return active delegated tasks, including queued work."""
        return sum(not task.done() for task in self._running_tasks.values())

    def get_executing_count(self) -> int:
        """Return tasks that currently hold execution capacity."""
        return sum(status.holds_capacity for status in self._task_statuses.values())

    def get_queued_count(self) -> int:
        """Return delegated tasks waiting for execution capacity."""
        return sum(status.phase == "queued" for status in self._task_statuses.values())

    def get_running_count_by_session(self, session_key: str) -> int:
        """Return the number of currently running subagents for a session."""
        tids = self._session_tasks.get(session_key, set())
        return sum(
            1 for tid in tids
            if tid in self._running_tasks and not self._running_tasks[tid].done()
        )

    def runtime_statuses(self) -> dict[str, SubagentStatus]:
        """Return a detached task-status mapping for read-only inspection."""
        return {**self._terminal_statuses, **self._task_statuses}

    def _provider_name(self, provider: LLMProvider | None = None) -> str:
        provider = provider or self.provider
        spec = getattr(provider, "_spec", None)
        name = getattr(spec, "name", None)
        return str(name or provider.__class__.__name__)

    def _write_run_record(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        temperature: float | None,
        workspace_scope: WorkspaceScope | None,
        result_text: str,
        status: SubagentStatus,
        provider: LLMProvider | None = None,
        model: str | None = None,
    ) -> None:
        """Write one observability record for this subagent run.

        Same writer + schema as cron run records; ``kind`` distinguishes them.
        Captures prompt, params, the model/provider that ran, token usage,
        iteration count, tool events, and outcome — everything needed to debug
        a looping/errored subagent after it finishes (previously lost).
        """
        record = {
            "kind": "subagent",
            "task_id": task_id,
            "label": label,
            "task": task,
            "origin": dict(origin),
            "workspace": str(workspace_scope.project_path) if workspace_scope else str(self.workspace),
            "params": {
                "temperature": temperature,
                **status.effective_budgets,
                "restrict_to_workspace": bool(
                    workspace_scope.restrict_to_workspace if workspace_scope else self.restrict_to_workspace
                ),
            },
            "model": model or self.model,
            "model_preset": status.effective_budgets.get("model_preset"),
            "provider": self._provider_name(provider),
            "iterations": int(status.iteration),
            "stop_reason": status.stop_reason,
            "phase": status.phase,
            "activity": status.activity,
            "error": status.error,
            "usage": build_usage_block(
                status.usage,
                provider=self._provider_name(provider),
                model=model or self.model,
            ),
            # Bounded persistence: only the most recent events are kept so
            # 121-iteration runs stay compact. Keys are additive (args/preview)
            # and absent on old records, which remain readable.
            "tool_events": list(status.tool_events)[-MAX_PERSISTED_TOOL_EVENTS:],
            "context": dict(status.context_report),
            "result": result_text,
            "retry_of": status.retry_of,
        }
        try:
            return write_run_record(self.records_dir, task_id, record)
        except OSError:
            # Records are observability-only; never let a write failure
            # propagate into the subagent announce path.
            logger.warning("Subagent [{}] could not write run record", task_id, exc_info=True)
