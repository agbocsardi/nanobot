"""Built-in slash command handlers."""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nanobot import __version__
from nanobot.agent.subagent import SubagentManager
from nanobot.bus.events import OutboundMessage
from nanobot.command.router import CommandContext, CommandRouter
from nanobot.utils.helpers import build_status_content
from nanobot.utils.restart import set_restart_notice_to_env
from nanobot.utils.usage import usage_delta, usage_snapshot


@dataclass(frozen=True)
class BuiltinCommandSpec:
    command: str
    title: str
    description: str
    icon: str
    arg_hint: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "command": self.command,
            "title": self.title,
            "description": self.description,
            "icon": self.icon,
            "arg_hint": self.arg_hint,
        }


BUILTIN_COMMAND_SPECS: tuple[BuiltinCommandSpec, ...] = (
    BuiltinCommandSpec(
        "/policy",
        "Policy approvals",
        "List, approve or deny pending tool-policy approvals.",
        "shield-check",
        arg_hint="[approve|deny <token>]",
    ),
    BuiltinCommandSpec(
        "/new",
        "New chat",
        "Stop the current task and start a fresh conversation.",
        "square-pen",
    ),
    BuiltinCommandSpec(
        "/stop",
        "Stop current task",
        "Cancel the active agent turn for this chat.",
        "square",
    ),
    BuiltinCommandSpec(
        "/restart",
        "Restart nanobot",
        "Restart the bot process in place.",
        "rotate-cw",
    ),
    BuiltinCommandSpec(
        "/status",
        "Show status",
        "Display runtime, provider, and channel status.",
        "activity",
    ),
    BuiltinCommandSpec(
        "/model",
        "Switch model preset",
        "Show or switch the active model preset.",
        "brain",
        "[preset]",
    ),
    BuiltinCommandSpec(
        "/history",
        "Show conversation history",
        "Print the last N persisted conversation messages.",
        "history",
        "[n]",
    ),
    BuiltinCommandSpec(
        "/goal",
        "Start long-running goal",
        "Tell the agent to treat the request as a long-running goal.",
        "activity",
        "<goal>",
    ),
    BuiltinCommandSpec(
        "/dream",
        "Run Dream",
        "Manually trigger memory consolidation.",
        "sparkles",
    ),
    BuiltinCommandSpec(
        "/dream-log",
        "Show Dream log",
        "Show what the last Dream consolidation changed.",
        "book-open",
    ),
    BuiltinCommandSpec(
        "/dream-restore",
        "Restore memory",
        "Revert memory to a previous Dream snapshot.",
        "undo-2",
    ),
    BuiltinCommandSpec(
        "/remember",
        "Remember",
        "Save a short note into curated topic memory under memory/.",
        "bookmark",
        arg_hint="[<topic>:] <text>",
    ),
    BuiltinCommandSpec(
        "/btw",
        "Side question",
        "Ask a quick ephemeral side question without disturbing active work.",
        "message-square",
    ),
    BuiltinCommandSpec(
        "/tasks",
        "Background tasks",
        "List queued/running/waiting and recent background tasks.",
        "list-tree",
    ),
    BuiltinCommandSpec(
        "/task",
        "Task control",
        "Inspect, stop, retrieve, or retry one background task.",
        "workflow",
        arg_hint="[stop|result|retry] <id>",
    ),
    BuiltinCommandSpec(
        "/skill",
        "List skills",
        "List all enabled skills available to the agent.",
        "wrench",
    ),
    BuiltinCommandSpec(
        "/help",
        "Show help",
        "List available slash commands.",
        "circle-help",
    ),
    BuiltinCommandSpec(
        "/pairing",
        "Manage pairing",
        "List, approve, deny or revoke pairing requests.",
        "shield",
        "[list|approve <code>|deny <code>|revoke <user_id>]",
    ),
)


def builtin_command_palette() -> list[dict[str, str]]:
    """Return structured command metadata for UI command palettes."""
    return [spec.as_dict() for spec in BUILTIN_COMMAND_SPECS]


async def cmd_stop(ctx: CommandContext) -> OutboundMessage:
    """Cancel all active tasks and subagents for the session."""
    loop = ctx.loop
    msg = ctx.msg
    total = await loop._cancel_active_tasks(ctx.key)
    content = f"Stopped {total} task(s)." if total else "No active task to stop."
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content=content,
        metadata=dict(msg.metadata or {})
    )


async def cmd_restart(ctx: CommandContext) -> OutboundMessage:
    """Restart the process in-place via os.execv."""
    msg = ctx.msg
    set_restart_notice_to_env(
        channel=msg.channel,
        chat_id=msg.chat_id,
        metadata=dict(msg.metadata or {}),
    )

    async def _do_restart():
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable, "-m", "nanobot"] + sys.argv[1:])

    asyncio.create_task(_do_restart())
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content="Restarting...",
        metadata=dict(msg.metadata or {})
    )


async def cmd_status(ctx: CommandContext) -> OutboundMessage:
    """Build an outbound status message for a session."""
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    ctx_est = 0
    with suppress(Exception):
        ctx_est, _ = loop.consolidator.estimate_session_prompt_tokens(session)
    if ctx_est <= 0:
        ctx_est = loop._last_usage.get("prompt_tokens", 0)

    # Fetch web search provider usage (best-effort, never blocks the response)
    search_usage_text: str | None = None
    # Never let usage fetch break /status
    with suppress(Exception):
        from nanobot.utils.searchusage import fetch_search_usage
        web_cfg = getattr(loop, "web_config", None)
        search_cfg = getattr(web_cfg, "search", None) if web_cfg else None
        if search_cfg is not None:
            provider = getattr(search_cfg, "provider", "duckduckgo")
            api_key = getattr(search_cfg, "api_key", "") or None
            usage = await fetch_search_usage(provider=provider, api_key=api_key)
            search_usage_text = usage.format()
    active_tasks = loop._active_tasks.get(ctx.key, [])
    task_count = sum(1 for t in active_tasks if not t.done())
    with suppress(Exception):
        task_count += loop.subagents.get_running_count_by_session(ctx.key)
    base_content = build_status_content(
        version=__version__, model=loop.model,
        start_time=loop._start_time, last_usage=loop._last_usage,
        context_window_tokens=loop.context_window_tokens,
        session_msg_count=len(session.get_history(max_messages=0)),
        context_tokens_estimate=ctx_est,
        search_usage_text=search_usage_text,
        active_task_count=task_count,
        max_completion_tokens=getattr(
            getattr(loop.provider, "generation", None), "max_tokens", 8192
        ),
    )
    # Operational sections (best-effort; /status must never fail the turn).
    extra: list[str] = []
    with suppress(Exception):
        cron_section = _status_cron_section(loop)
        if cron_section:
            extra.append(cron_section)
    with suppress(Exception):
        extra.append(_status_model_chain(loop))
    content = base_content + ("\n\n" + "\n\n".join(extra) if extra else "")
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )



def _format_utc_ms(ts_ms: int | None) -> str:
    """Format an epoch-ms timestamp as a compact UTC string."""
    if not ts_ms:
        return "never"
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _format_cron_schedule(schedule: Any) -> str:
    """Human-readable schedule, mirroring the cron tool's timing format."""
    kind = getattr(schedule, "kind", "?")
    if kind == "cron":
        tz = f" ({schedule.tz})" if getattr(schedule, "tz", None) else ""
        return f"cron: {getattr(schedule, 'expr', '?')}{tz}"
    if kind == "every" and getattr(schedule, "every_ms", None):
        ms = schedule.every_ms
        if ms % 3_600_000 == 0:
            return f"every {ms // 3_600_000}h"
        if ms % 60_000 == 0:
            return f"every {ms // 60_000}m"
        if ms % 1000 == 0:
            return f"every {ms // 1000}s"
        return f"every {ms}ms"
    if kind == "at" and getattr(schedule, "at_ms", None):
        return f"at {_format_utc_ms(schedule.at_ms)}"
    return kind


def _status_cron_section(loop: Any) -> str:
    """Operational cron view: enabled jobs, schedule, last + next run.

    Run history and exit state come from the persisted cron store
    (``jobs.json`` state.runHistory); jobs without a recorded run render as
    ``never``. Returns an empty string when no cron service is attached
    (e.g. REPL mode).
    """
    cron = getattr(loop, "cron_service", None)
    if cron is None:
        return ""
    lines = ["## Cron jobs"]
    try:
        enabled = cron.list_jobs()
        all_jobs = cron.list_jobs(include_disabled=True)
    except Exception:
        return "## Cron jobs\ncron store unavailable"
    if not all_jobs:
        lines.append("no scheduled jobs")
        return "\n".join(lines)
    for job in enabled:
        state = job.state
        if state.last_run_at_ms:
            last_line = (
                f"  last run: {_format_utc_ms(state.last_run_at_ms)} "
                f"({state.last_status or 'unknown'})"
            )
        else:
            last_line = "  last run: never"
        if state.last_error:
            last_line += f" — {state.last_error[:100]}"
        lines.append(f"- {job.name} — {_format_cron_schedule(job.schedule)}")
        lines.append(last_line)
        lines.append(f"  next: {_format_utc_ms(state.next_run_at_ms)}")
    disabled = len(all_jobs) - len(enabled)
    if disabled:
        lines.append(f"({disabled} disabled)")
    return "\n".join(lines)


def _snapshot_model(loop: Any, attr: str) -> str | None:
    snap = getattr(loop, attr, None)
    if snap is None:
        return None
    model = getattr(snap, "model", None)
    return str(model) if model else None


def _status_model_chain(loop: Any) -> str:
    """Effective model chain: foreground, cron, subagents, dream/consolidator.

    Never prints tokens, keys, or provider credentials — only preset/model
    names.
    """
    active_preset = getattr(loop, "model_preset", None) or "default"
    foreground_model = getattr(loop, "model", None) or "?"
    consolidator = getattr(loop, "consolidator", None)
    consolidator_model = getattr(consolidator, "model", None)
    return "\n".join([
        "## Model chain",
        f"- foreground: {active_preset} ({foreground_model})",
        f"- cron: {_snapshot_model(loop, '_cron_run_snapshot') or 'foreground preset'}",
        f"- subagents: {_snapshot_model(loop, '_subagent_run_snapshot') or 'foreground preset'}",
        f"- dream/consolidator: {consolidator_model or 'foreground provider'}",
        "- fallback order: per-job override -> runPresets[kind] -> active modelPreset -> default",
    ])



async def cmd_new(ctx: CommandContext) -> OutboundMessage:
    """Stop active task and start a fresh session."""
    loop = ctx.loop
    await loop._cancel_active_tasks(ctx.key)
    loop.discard_session_file_state(ctx.key)
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    snapshot = session.messages[session.last_consolidated:]
    snapshot_usage = usage_delta(
        usage_snapshot(session.metadata.get("usage")),
        usage_snapshot(session.metadata.get("_usage_archived")),
    )
    session.clear()
    loop.sessions.save(session)
    loop.sessions.invalidate(session.key)
    loop._last_usage = {}
    if snapshot:
        loop._schedule_background(
            loop.consolidator.archive(snapshot, session_key=ctx.key, usage=snapshot_usage)
        )
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content="New session started.",
        metadata=dict(ctx.msg.metadata or {})
    )


def _format_preset_names(names: list[str]) -> str:
    return ", ".join(f"`{name}`" for name in names) if names else "(none configured)"


def _model_preset_names(loop) -> list[str]:
    names = set(loop.model_presets)
    names.add("default")
    return ["default", *sorted(name for name in names if name != "default")]


def _active_model_preset_name(loop) -> str:
    return loop.model_preset or "default"


def _command_error_message(exc: Exception) -> str:
    return str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)


def _model_command_status(loop) -> str:
    names = _model_preset_names(loop)
    active = _active_model_preset_name(loop)
    return "\n".join([
        "## Model",
        f"- Current model: `{loop.model}`",
        f"- Current preset: `{active}`",
        f"- Available presets: {_format_preset_names(names)}",
    ])


async def cmd_model(ctx: CommandContext) -> OutboundMessage:
    """Show or switch model presets."""
    loop = ctx.loop
    args = ctx.args.strip()
    metadata = {**dict(ctx.msg.metadata or {}), "render_as": "text"}

    if not args:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=_model_command_status(loop),
            metadata=metadata,
        )

    parts = args.split()
    if len(parts) != 1:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Usage: `/model [preset]`",
            metadata=metadata,
        )

    name = parts[0]
    try:
        loop.set_model_preset(name)
    except (KeyError, ValueError) as exc:
        names = _model_preset_names(loop)
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=(
                f"Could not switch model preset: {_command_error_message(exc)}\n\n"
                f"Available presets: {_format_preset_names(names)}"
            ),
            metadata=metadata,
        )

    max_tokens = getattr(getattr(loop.provider, "generation", None), "max_tokens", None)
    lines = [
        f"Switched model preset to `{loop.model_preset}`.",
        f"- Model: `{loop.model}`",
        f"- Context window: {loop.context_window_tokens}",
    ]
    if max_tokens is not None:
        lines.append(f"- Max output tokens: {max_tokens}")
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content="\n".join(lines),
        metadata=metadata,
    )


async def cmd_dream(ctx: CommandContext) -> OutboundMessage:
    """Manually trigger a Dream consolidation run."""
    import time

    loop = ctx.loop
    msg = ctx.msg

    async def _run_dream():
        from nanobot.agent.memory import MemoryStore
        from nanobot.config.schema import DreamConfig

        dream_session_key = MemoryStore.dream_session_key
        prune_dream_sessions = MemoryStore.prune_dream_sessions

        store = loop.context.memory
        dream_cfg = getattr(loop, "dream_config", None) or DreamConfig()
        t0 = time.monotonic()
        try:
            key = dream_session_key()
            outcome = await store.run_dream(
                run=lambda prompt: loop.process_direct(
                    prompt,
                    session_key=key,
                    ephemeral=True,
                    tools=store.build_dream_tools(),
                    run_max_iterations=dream_cfg.max_iterations,
                    run_llm_timeout_s=dream_cfg.timeout_s,
                ),
                max_batch_size=dream_cfg.max_batch_size,
                max_iterations=dream_cfg.max_iterations,
                timeout_s=dream_cfg.timeout_s,
                max_changed_files=dream_cfg.max_changed_files,
                max_diff_chars=dream_cfg.max_diff_chars,
                model_label=loop.model,
                commit_prefix="dream: manual run",
                session_key=key,
                session_manager=loop.sessions,
            )
            elapsed = time.monotonic() - t0
            if outcome.reason == "nothing_to_process":
                await loop.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Dream: nothing to process.",
                ))
                return
            if outcome.completed:
                content = f"Dream completed in {elapsed:.1f}s."
                if outcome.commit_sha:
                    content += f" (commit {outcome.commit_sha})"
            else:
                content = (
                    f"Dream did not complete ({outcome.reason}) after {elapsed:.1f}s; "
                    "memory cursor was not advanced."
                )
        except Exception as e:
            elapsed = time.monotonic() - t0
            content = f"Dream failed after {elapsed:.1f}s: {e}"
        finally:
            store.compact_history()
            prune_dream_sessions(loop.sessions.sessions_dir)
        await loop.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    asyncio.create_task(_run_dream())
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content="Dreaming...",
    )


def _extract_changed_files(diff: str) -> list[str]:
    """Extract changed file paths from a unified diff."""
    files: list[str] = []
    seen: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        path = parts[3]
        if path.startswith("b/"):
            path = path[2:]
        if path in seen:
            continue
        seen.add(path)
        files.append(path)
    return files


def _format_changed_files(diff: str) -> str:
    files = _extract_changed_files(diff)
    if not files:
        return "No tracked memory files changed."
    return ", ".join(f"`{path}`" for path in files)


_DREAM_COMMIT_PREFIX = "dream:"


def _format_dream_log_content(commit, diff: str, *, requested_sha: str | None = None) -> str:
    files_line = _format_changed_files(diff)
    lines = [
        "## Dream Update",
        "",
        "Here is the selected Dream memory change." if requested_sha else "Here is the latest Dream memory change.",
        "",
        f"- Commit: `{commit.sha}`",
        f"- Time: {commit.timestamp}",
        f"- Changed files: {files_line}",
    ]
    if diff:
        lines.extend([
            "",
            f"Use `/dream-restore {commit.sha}` to undo this change.",
            "",
            "```diff",
            diff.rstrip(),
            "```",
        ])
    else:
        lines.extend([
            "",
            "Dream recorded this version, but there is no file diff to display.",
        ])
    return "\n".join(lines)


def _format_dream_restore_list(commits: list) -> str:
    lines = [
        "## Dream Restore",
        "",
        "Choose a Dream memory version to restore. Latest first:",
        "",
    ]
    for c in commits:
        lines.append(f"- `{c.sha}` {c.timestamp} - {c.message.splitlines()[0]}")
    lines.extend([
        "",
        "Preview a version with `/dream-log <sha>` before restoring it.",
        "Restore a version with `/dream-restore <sha>`.",
    ])
    return "\n".join(lines)


async def cmd_dream_log(ctx: CommandContext) -> OutboundMessage:
    """Show what the last Dream changed.

    Default: diff of the latest Dream commit versus its parent.
    With /dream-log <sha>: diff of that specific commit.
    """
    store = ctx.loop.consolidator.store
    git = store.git

    if not git.is_initialized():
        if store.get_last_dream_cursor() == 0:
            msg = "Dream has not run yet. Run `/dream`, or wait for the next scheduled Dream cycle."
        else:
            msg = "Dream history is not available because memory versioning is not initialized."
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=msg, metadata={"render_as": "text"},
        )

    args = ctx.args.strip()

    if args:
        # Show diff of a specific commit
        sha = args.split()[0]
        result = git.show_commit_diff(sha)
        if not result:
            content = (
                f"Couldn't find Dream change `{sha}`.\n\n"
                "Use `/dream-restore` to list recent versions, "
                "or `/dream-log` to inspect the latest one."
            )
        else:
            commit, diff = result
            content = _format_dream_log_content(commit, diff, requested_sha=sha)
    else:
        # Default: show the latest Dream commit's diff
        commits = git.log(max_entries=1, message_prefix=_DREAM_COMMIT_PREFIX)
        result = (
            git.show_commit_diff(
                commits[0].sha,
                max_entries=1,
                message_prefix=_DREAM_COMMIT_PREFIX,
            )
            if commits else None
        )
        if result:
            commit, diff = result
            content = _format_dream_log_content(commit, diff)
        else:
            content = "Dream memory has no saved versions yet."

    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=content, metadata={"render_as": "text"},
    )


async def cmd_dream_restore(ctx: CommandContext) -> OutboundMessage:
    """Restore memory files from a previous dream commit.

    Usage:
        /dream-restore          — list recent commits
        /dream-restore <sha>    — revert a specific commit
    """
    store = ctx.loop.consolidator.store
    git = store.git
    if not git.is_initialized():
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="Dream history is not available because memory versioning is not initialized.",
        )

    args = ctx.args.strip()
    if not args:
        # Show recent Dream commits for the user to pick
        commits = git.log(max_entries=10, message_prefix=_DREAM_COMMIT_PREFIX)
        if not commits:
            content = "Dream memory has no saved versions to restore yet."
        else:
            content = _format_dream_restore_list(commits)
    else:
        sha = args.split()[0]
        result = git.show_commit_diff(sha, message_prefix=_DREAM_COMMIT_PREFIX)
        if not result:
            content = (
                f"Couldn't restore Dream change `{sha}`.\n\n"
                "Only Dream memory versions can be restored. "
                "Use `/dream-restore` to list recent versions."
            )
        else:
            changed_files = _format_changed_files(result[1])
            new_sha = git.revert(sha, message_prefix=_DREAM_COMMIT_PREFIX)
            if new_sha:
                content = (
                    f"Restored Dream memory to the state before `{sha}`.\n\n"
                    f"- New safety commit: `{new_sha}`\n"
                    f"- Restored files: {changed_files}\n\n"
                    f"Use `/dream-log {new_sha}` to inspect the restore diff."
                )
            else:
                content = (
                    f"Couldn't restore Dream change `{sha}`.\n\n"
                    "It may be the first saved version with no earlier state to restore."
                )
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=content, metadata={"render_as": "text"},
    )


_HISTORY_DEFAULT_COUNT = 10
_HISTORY_MAX_COUNT = 50
_HISTORY_MAX_CONTENT_CHARS = 200


def _format_history_message(msg: dict) -> str | None:
    """Format a single history message for display. Returns None to skip."""
    role = msg.get("role")
    if role not in ("user", "assistant"):
        return None
    content = msg.get("content") or ""
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        content = " ".join(parts)
    content = str(content).strip()
    if not content:
        return None
    if len(content) > _HISTORY_MAX_CONTENT_CHARS:
        content = content[:_HISTORY_MAX_CONTENT_CHARS] + "…"
    label = "👤 You" if role == "user" else "🤖 Bot"
    return f"{label}: {content}"


async def cmd_history(ctx: CommandContext) -> OutboundMessage:
    """Show the last N messages of the current session (default 10, max 50).

    Usage: /history [count]
    """
    count = _HISTORY_DEFAULT_COUNT
    if ctx.args.strip():
        try:
            count = max(1, min(int(ctx.args.strip()), _HISTORY_MAX_COUNT))
        except ValueError:
            return OutboundMessage(
                channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
                content="Usage: /history [count] — e.g. /history 5 (default: 10, max: 50)",
                metadata=dict(ctx.msg.metadata or {}),
            )

    session = ctx.session or ctx.loop.sessions.get_or_create(ctx.key)
    history = session.get_history(max_messages=0)
    visible = [_format_history_message(m) for m in history]
    visible = [m for m in visible if m is not None]
    recent = visible[-count:]

    if not recent:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="No conversation history yet.",
            metadata=dict(ctx.msg.metadata or {}),
        )

    header = f"Last {len(recent)} message(s):\n"
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=header + "\n".join(recent),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


_GOAL_PROMPT_TEMPLATE = """The user declared a sustained objective for this thread.

Inspect or clarify if needed, then call `long_task` with the refined objective (and optional short ui_summary). Work proceeds as normal assistant turns using your usual tools. When the objective is fully done and verified, call `complete_goal` with a brief recap. If the user later cancels or changes direction, still call `complete_goal` with an honest recap (then `long_task` again only after there is no active goal). Do not use `long_task` / `complete_goal` for trivial one-shot answers.

Goal:
{goal}
"""


async def cmd_goal(ctx: CommandContext) -> OutboundMessage | None:
    """Rewrite /goal into a normal agent turn that nudges long_task use."""
    goal = ctx.args.strip()
    if not goal:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Usage: /goal <long-running task description>",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    if ctx.session is None:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=(
                "A task is already running for this chat. "
                "Use `/stop` first, then send `/goal <long-running task description>` again."
            ),
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )

    ctx.msg.metadata = {
        **dict(ctx.msg.metadata or {}),
        "original_command": "/goal",
        "original_content": ctx.raw,
        "goal_started_at": time.time(),
    }
    ctx.msg.content = _GOAL_PROMPT_TEMPLATE.format(goal=goal)
    return None


async def cmd_pairing(ctx: CommandContext) -> OutboundMessage:
    """List, approve, deny or revoke pairing requests."""
    from nanobot.pairing import PAIRING_COMMAND_META_KEY, handle_pairing_command

    reply = handle_pairing_command(ctx.msg.channel, ctx.args)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=reply,
        metadata={PAIRING_COMMAND_META_KEY: True},
    )


async def cmd_policy(ctx: CommandContext) -> OutboundMessage:
    """List, approve or deny pending tool-policy approvals for this session."""
    loop = ctx.loop
    content: str
    if loop is None or not hasattr(loop, "approval_store"):
        content = "Policy approvals are not available in this context."
    else:
        store = loop.approval_store(ctx.key)
        action, _, token = ctx.args.partition(" ")
        token = token.strip()
        if not ctx.args.strip() or action in {"help", "-h", "--help"}:
            pending = store.pending_list()
            if not pending:
                content = (
                    "No pending tool-policy approvals for this session. "
                    "An ask rule will emit one when it blocks a tool call."
                )
            else:
                lines = [f"Pending policy approvals ({len(pending)}):", ""]
                for item in pending:
                    resource = f" {item.resource}" if item.resource else ""
                    lines.append(
                        f"- `{item.token}` — rule `{item.rule_id}`, "
                        f"tool `{item.tool}`{resource} (requested {_format_age(item.requested_at)})"
                    )
                content = "\n".join(lines) + (
                    "\n\nApprove with `/policy approve <token|rule>` or "
                    "deny with `/policy deny <token|rule>`."
                )
        elif action in {"approve", "allow"}:
            if not token:
                content = "Usage: `/policy approve <token|rule-id>`"
            else:
                resolution = store.approve(token)
                if resolution is None:
                    content = _policy_unknown_token_message(store, token)
                else:
                    content = (
                        f"Approved rule `{resolution.rule_id}` for tool "
                        f"`{resolution.tool}` (token `{resolution.token}`). "
                        f"The decision is cached for {resolution.cache_ttl_s:.0f}s "
                        "in this session; the same call will not be re-prompted."
                    )
        elif action in {"deny", "block"}:
            if not token:
                content = "Usage: `/policy deny <token|rule-id>`"
            else:
                resolution = store.deny(token)
                if resolution is None:
                    content = _policy_unknown_token_message(store, token)
                else:
                    content = (
                        f"Denied rule `{resolution.rule_id}` for tool "
                        f"`{resolution.tool}` (token `{resolution.token}`). "
                        "Matching calls are blocked like a deny rule for "
                        f"{resolution.cache_ttl_s:.0f}s in this session."
                    )
        else:
            content = (
                "Usage: `/policy` (list), `/policy approve <token|rule-id>`, "
                "`/policy deny <token|rule-id>`."
            )
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata=dict(ctx.msg.metadata or {}),
    )


def _format_age(epoch: float) -> str:
    age = max(0, time.time() - epoch)
    if age < 60:
        return f"{age:.0f}s ago"
    return f"{age / 60:.1f}m ago"


def _policy_unknown_token_message(store, token: str) -> str:
    pending = store.pending_list()
    if not pending:
        return f"No pending approval matches `{token}` (none pending)."
    lines = [f"No valid pending approval matches `{token}`. Pending:", ""]
    for item in pending:
        lines.append(f"- `{item.token}` — rule `{item.rule_id}`, tool `{item.tool}`")
    return "\n".join(lines)



# ---------------------------------------------------------------------------
# background task control (issue #27): /tasks and /task
# ---------------------------------------------------------------------------


def _task_command_line(record: dict) -> str:
    """One compact, deterministic line for /tasks output."""
    status = SubagentManager.task_status_vocabulary(record)
    created = int(record.get("created_at_ms") or 0)
    updated = int(record.get("updated_at_ms") or created)
    label = SubagentManager._truncate_chat(str(record.get("label") or ""), 60)
    if status in ("queued", "waiting", "running"):
        return f"[{status}] {record.get('task_id')} {label} (started {_format_utc_ms(created)})"
    return (
        f"[{status}] {record.get('task_id')} {label} "
        f"(started {_format_utc_ms(created)} -> ended {_format_utc_ms(updated)})"
    )


async def cmd_tasks(ctx: CommandContext) -> OutboundMessage:
    """List the most recent owned background tasks (newest first).

    Live tasks now have durable records from spawn time, so the list is one
    flat, newest-first view: queued/running/waiting and terminal alike.
    """
    loop = ctx.loop
    msg = ctx.msg
    if loop is None or not hasattr(loop, "subagents"):
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content="Task control is not available here.",
            metadata=dict(msg.metadata or {}),
        )
    try:
        records = loop.subagents.list_session_task_records(
            ctx.key, sender_id=getattr(msg, "sender_id", None)
        )
    except Exception:
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content="Error listing tasks.",
            metadata=dict(msg.metadata or {}),
        )
    if not records:
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content="No background tasks for this session.",
            metadata=dict(msg.metadata or {}),
        )
    lines = ["## Tasks"]
    lines.extend(f"- {_task_command_line(r)}" for r in records)
    waiting_store = getattr(loop, "waiting_runs", None)
    if waiting_store is not None:
        waiting = [
            w for w in waiting_store.list_for_owner(
                ctx.key, sender_id=getattr(msg, "sender_id", None)
            )
            if w.status in ("waiting", "resuming")
        ]
        if waiting:
            lines.append("waiting:")
            for w in waiting[:10]:
                label = _waiting_run_line(w)
                lines.append(f"- {label}")
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id,
        content="\n".join(lines),
        metadata=dict(msg.metadata or {}),
    )


def _waiting_run_line(run) -> str:
    """One compact waiting-run line."""
    label = SubagentManager._truncate_chat(str(run.note or "awaiting answer"), 60)
    return f"[{run.status}] {run.run_id} {label} (question {run.question_id})"


def _waiting_run_detail_lines(run) -> list[str]:
    """Bounded detail for a waiting/resumed run (issue #29)."""
    lines = [
        f"run: {run.run_id}",
        f"status: {run.status}",
        f"question: {run.question_id}",
    ]
    if run.note:
        lines.append("note: " + SubagentManager._truncate_chat(run.note, 300))
    if run.completed_tool_names:
        lines.append("completed tools: " + ", ".join(run.completed_tool_names[:10]))
    if run.budgets:
        parts = [f"{k}={v}" for k, v in run.budgets.items() if v is not None]
        if parts:
            lines.append("budgets: " + " ".join(parts))
    return lines


def _task_detail_lines(record: dict, result_limit: int = 400) -> list[str]:
    status = SubagentManager.task_status_vocabulary(record)
    created = int(record.get("created_at_ms") or 0)
    updated = int(record.get("updated_at_ms") or created)
    params = record.get("params") or {}
    lines = [
        f"task: {record.get('task_id')}",
        f"label: {SubagentManager._truncate_chat(str(record.get('label') or ''), 80)}",
        f"status: {status}",
        f"started: {_format_utc_ms(created)}",
    ]
    if status not in ("queued", "waiting", "running"):
        lines.append(f"ended: {_format_utc_ms(updated)}")
    budgets = []
    if params.get("max_iterations") is not None:
        budgets.append(f"iterations={params.get('max_iterations')}")
    if params.get("context_window_tokens") is not None:
        budgets.append(f"context={params.get('context_window_tokens')}")
    if params.get("max_tokens") is not None:
        budgets.append(f"max_tokens={params.get('max_tokens')}")
    if params.get("model_preset"):
        budgets.append(f"preset={params.get('model_preset')}")
    if budgets:
        lines.append("budgets: " + " ".join(budgets))
    if record.get("iterations") is not None:
        lines.append(f"iterations used: {record.get('iterations')}")
    if record.get("retry_of"):
        lines.append(f"retry of: {record.get('retry_of')}")
    if record.get("retried_by"):
        lines.append(f"retried as: {record.get('retried_by')}")
    if record.get("error"):
        lines.append("error: " + SubagentManager._truncate_chat(str(record.get("error")), 200))
    result = record.get("result")
    if status not in ("queued", "waiting", "running") and result:
        lines.append("result: " + SubagentManager._truncate_chat(str(result), result_limit))
    return lines


async def cmd_task(ctx: CommandContext) -> OutboundMessage:
    """Inspect, stop, retrieve, or retry ONE background task."""
    loop = ctx.loop
    msg = ctx.msg
    if loop is None or not hasattr(loop, "subagents"):
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content="Task control is not available here.",
            metadata=dict(msg.metadata or {}),
        )
    parts = ctx.args.strip().split()
    if not parts:
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content="Usage: `/task <id>` | `/task stop|result|retry <id>`",
            metadata=dict(msg.metadata or {}),
        )
    action = parts[0]
    sender_id = getattr(msg, "sender_id", None)

    if action in ("stop", "result", "retry"):
        if len(parts) < 2:
            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content=f"Usage: `/task {action} <id>`",
                metadata=dict(msg.metadata or {}),
            )
        task_id = parts[1]
        if action == "stop":
            result = await loop.subagents.cancel_task(task_id, session_key=ctx.key)
            if result == "cancelled":
                content = f"Cancelled task {task_id}."
            elif result == "done":
                content = f"Task {task_id} already finished."
            else:
                waiting_store = getattr(loop, "waiting_runs", None)
                cancelled_waiting = (
                    waiting_store.cancel(task_id, session_key=ctx.key)
                    if waiting_store is not None
                    else False
                )
                content = (
                    f"Cancelled waiting run {task_id}."
                    if cancelled_waiting
                    else f"Task {task_id} not found."
                )
        elif action == "retry":
            status, new_id = await loop.subagents.retry_task(
                task_id, session_key=ctx.key, sender_id=sender_id
            )
            if status == "created":
                content = f"Retried {task_id} as {new_id} (same bounded model settings)."
            elif status == "still_active":
                content = f"Task {task_id} is still running."
            elif status == "queue_full":
                content = "Retry rejected: the delegated queue is full. Try later."
            else:
                content = f"Task {task_id} not found or not retryable."
        else:  # result
            record, _ = loop.subagents.owned_record(
                task_id, session_key=ctx.key, sender_id=sender_id
            )
            if record is None:
                content = f"Task {task_id} not found."
            else:
                status = SubagentManager.task_status_vocabulary(record)
                if status in ("queued", "waiting", "running"):
                    content = f"Task {task_id} is still {status}; use `/task {task_id}` for live state."
                elif record.get("result"):
                    content = SubagentManager._truncate_chat(str(record.get("result")), 800)
                else:
                    error = SubagentManager._truncate_chat(str(record.get("error")), 800)
                    content = f"Task {task_id} ended with no result." + (f"\n{error}" if error else "")
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
            metadata=dict(msg.metadata or {}),
        )

    # Bare task id -> detail view (session + recorded-sender gated), with a
    # fallback to durable waiting runs from the ask_user wait/resume surface.
    record, _ = loop.subagents.owned_record(
        action, session_key=ctx.key, sender_id=sender_id
    )
    if record is not None:
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id,
            content="\n".join(_task_detail_lines(record)),
            metadata=dict(msg.metadata or {}),
        )
    waiting_store = getattr(loop, "waiting_runs", None)
    if waiting_store is not None:
        run = waiting_store.get(action)
        if run is not None:
            origin_ok = run.session_key == ctx.key
            if origin_ok and sender_id and run.sender_id and run.sender_id != sender_id:
                origin_ok = False
            if origin_ok:
                return OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="\n".join(_waiting_run_detail_lines(run)),
                    metadata=dict(msg.metadata or {}),
                )
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id,
        content=f"Task {action} not found.",
        metadata=dict(msg.metadata or {}),
    )

async def cmd_skill(ctx: CommandContext) -> OutboundMessage:
    """List all enabled skills (name and description only)."""
    loop = ctx.loop
    skills = loop.context.skills.list_skills(filter_unavailable=False)
    if not skills:
        content = "No skills available."
    else:
        lines = [f"Available skills ({len(skills)}):", ""]
        for entry in skills:
            desc = loop.context.skills._get_skill_description(entry["name"])
            lines.append(f"- **{entry['name']}** — {desc}")
        content = "\n".join(lines)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata=dict(ctx.msg.metadata or {}),
    )

DEFAULT_REMEMBER_TOPIC = "user-notes.md"
"""Default curated topic file for /remember notes without an explicit topic."""


def _remember_usage() -> str:
    return "Usage: /remember <text>  |  /remember <topic>: <text>"


def _parse_remember_args(args: str) -> tuple[str | None, str]:
    """Split /remember args into (topic, text).

    One rule: a leading ``<slug>:`` (ASCII letters/digits/_/, lowercased)
    selects the topic; anything else is a note under the default topic.
    """
    raw = args.strip()
    prefix, sep, rest = raw.partition(":")
    topic = (
        prefix.lower()
        if (sep and re.fullmatch(r"[a-z][a-z0-9_-]*", prefix.lower()))
        else None
    )
    return topic, (rest.strip() if topic is not None else raw)


def _strip_frontmatter(content: str) -> str:
    """Return the body below a ``---`` frontmatter block (local, minimal)."""
    if not content.startswith("---\n"):
        return content
    end = content.find("\n---", 3)
    if end == -1:
        return content
    return content[end + 4 :].lstrip("\n")


def _remember_reply(ctx: CommandContext, content: str) -> OutboundMessage:
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_remember(ctx: CommandContext) -> OutboundMessage:
    """Append a dated note to curated topic memory under memory/.

    ``/remember <text>`` -> ``memory/user-notes.md``; ``/remember <topic>:
    <text>`` -> ``memory/<topic>.md``. Delegates the write to
    ``MemoryWriteTool`` (path guardrails, frontmatter preservation, atomic
    writes, read-back verification). Never fails the turn.
    """
    loop = ctx.loop
    if loop is None or not hasattr(loop, "workspace"):
        return _remember_reply(ctx, "Error: /remember is unavailable in this context.")
    if not ctx.args.strip():
        return _remember_reply(ctx, _remember_usage())

    # Imported lazily: builtin.py is imported during agent boot, before the
    # agent module graph is fully initialized (mirrors /pairing's lazy import).
    from nanobot.agent.tools._memory_common import (
        MAX_BODY_BYTES,
        MemoryPathError,
        resolve_topic_path,
    )
    from nanobot.agent.tools.memory_write import MemoryWriteTool

    try:
        topic, text = _parse_remember_args(ctx.args)
        rel = topic + ".md" if topic is not None else DEFAULT_REMEMBER_TOPIC
        if not text:
            raise ValueError(f"Error: missing text after topic: {_remember_usage()}")

        # Read the existing body so notes append; MemoryWriteTool preserves the
        # existing frontmatter when title/description/tags are omitted.
        existing_body = ""
        resolved = resolve_topic_path(Path(loop.workspace), rel)
        if resolved.exists() and resolved.is_dir():
            return _remember_reply(
                ctx, f"Error: memory/{rel} is a directory, not a topic memory file."
            )
        if resolved.exists():
            try:
                content = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                return _remember_reply(ctx, f"Error: cannot read existing memory/{rel}: {e}")
            existing_body = _strip_frontmatter(content)

        heading = f"## {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        note = text.strip() + "\n"
        prefix = existing_body.rstrip() + "\n\n" if existing_body.strip() else ""
        new_body = prefix + heading + "\n\n" + note
        if len(new_body.encode("utf-8")) > MAX_BODY_BYTES:
            return _remember_reply(
                ctx,
                f"Error: memory/{rel} would exceed the body cap; keep the note shorter.",
            )

        result = await MemoryWriteTool(workspace=Path(loop.workspace)).execute(rel, new_body)
        if getattr(result, "status", "success") in ("success", "partial"):
            verb = "appended to" if existing_body.strip() else "created"
            return _remember_reply(ctx, f"remembered in memory/{rel} — {verb} topic memory.")
        return _remember_reply(ctx, f"Error: {result}")
    except MemoryPathError as e:
        return _remember_reply(ctx, str(e))
    except (ValueError, OSError) as e:
        return _remember_reply(ctx, str(e))
    except Exception as e:  # noqa: BLE001 - /remember must never fail the turn.
        return _remember_reply(ctx, f"Error: {e}")



async def cmd_help(ctx: CommandContext) -> OutboundMessage:
    """Return available slash commands."""
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=build_help_text(),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


def build_help_text() -> str:
    """Build canonical help text shared across channels."""
    lines = ["🐈 nanobot commands:"]
    for spec in BUILTIN_COMMAND_SPECS:
        command = spec.command
        if spec.arg_hint:
            command = f"{command} {spec.arg_hint}"
        lines.append(f"{command} — {spec.description}")
    return "\n".join(lines)


def register_builtin_commands(router: CommandRouter) -> None:
    """Register the default set of slash commands."""
    router.priority("/stop", cmd_stop)
    router.priority("/restart", cmd_restart)
    router.priority("/status", cmd_status)
    router.exact("/new", cmd_new)
    router.exact("/status", cmd_status)
    router.exact("/model", cmd_model)
    router.prefix("/model ", cmd_model)
    router.exact("/history", cmd_history)
    router.prefix("/history ", cmd_history)
    router.exact("/goal", cmd_goal)
    router.prefix("/goal ", cmd_goal)
    router.exact("/dream", cmd_dream)
    router.exact("/dream-log", cmd_dream_log)
    router.prefix("/dream-log ", cmd_dream_log)
    router.exact("/dream-restore", cmd_dream_restore)
    router.prefix("/dream-restore ", cmd_dream_restore)
    router.exact("/skill", cmd_skill)
    router.exact("/remember", cmd_remember)
    router.prefix("/remember ", cmd_remember)
    router.exact("/tasks", cmd_tasks)
    router.exact("/task", cmd_task)
    router.prefix("/task ", cmd_task)
    router.exact("/help", cmd_help)
    router.exact("/pairing", cmd_pairing)
    router.prefix("/pairing ", cmd_pairing)
    router.exact("/policy", cmd_policy)
    router.prefix("/policy ", cmd_policy)
