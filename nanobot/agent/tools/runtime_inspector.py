"""Read-only effective-runtime snapshot for operational diagnosis."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any


def fingerprint_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


class RuntimeInspector:
    """Build a detached, non-secret snapshot from a live AgentLoop-like target."""

    def __init__(self, runtime: Any):
        self.runtime = runtime

    def snapshot(self, *, session_key: str | None = None) -> dict[str, Any]:
        return {
            "version": 1,
            "runtime": self._runtime(),
            "config": self._config(),
            "session": self._session(session_key),
            "telegram": self._telegram_reply_context(),
            "delegated_work": self._delegated_work(),
            "cron": self._cron(),
            "environment": self._environment(),
            "repository": self._repository(),
            "capabilities": self._capabilities(),
        }

    def _runtime(self) -> dict[str, Any]:
        runtime = self.runtime
        provider = getattr(runtime, "provider", None)
        preset = getattr(runtime, "model_preset", None)
        generation = getattr(provider, "generation", None)
        return {
            "provider": type(provider).__name__ if provider is not None else None,
            "model": self._primitive(getattr(runtime, "model", None)),
            "preset": self._primitive(preset),
            "routing_source": f"preset:{preset}" if preset else "configured_default",
            "budgets": {
                "context_window_tokens": self._primitive(
                    getattr(runtime, "context_window_tokens", None)
                ),
                "max_iterations": self._primitive(getattr(runtime, "max_iterations", None)),
                "max_tool_result_chars": self._primitive(
                    getattr(runtime, "max_tool_result_chars", None)
                ),
                "max_completion_tokens": self._primitive(
                    getattr(generation, "max_tokens", None)
                ),
                "provider_retry_mode": self._primitive(
                    getattr(runtime, "provider_retry_mode", None)
                ),
            },
        }

    def _config(self) -> dict[str, Any]:
        from nanobot.config.loader import get_config_path

        path = Path(getattr(self.runtime, "_loaded_config_path", None) or get_config_path())
        loaded = self._primitive(getattr(self.runtime, "_loaded_config_fingerprint", None))
        on_disk = fingerprint_file(path)
        available = path.exists()
        restart_required = loaded != on_disk if loaded else None
        return {
            "available": available,
            "path": str(path),
            "loaded_fingerprint": loaded,
            "on_disk_fingerprint": on_disk,
            "restart_required": restart_required,
            "drift_status": (
                "missing"
                if loaded and not available
                else "untracked"
                if not loaded
                else "changed"
                if loaded != on_disk
                else "current"
            ),
        }

    def _session(self, session_key: str | None) -> dict[str, Any]:
        session = None
        sessions = getattr(self.runtime, "sessions", None)
        cache = getattr(sessions, "_cache", None)
        if session_key and isinstance(cache, dict):
            session = cache.get(session_key)
        metadata = getattr(session, "metadata", None)
        if isinstance(metadata, dict):
            from nanobot.session.goal_state import goal_state_ws_blob

            goal = goal_state_ws_blob(metadata)
        else:
            goal = None
        return {
            "available": session is not None,
            "key": session_key,
            "active_goal": goal,
        }

    def _telegram_reply_context(self) -> dict[str, Any]:
        """Telegram reply-context reachability diagnostics (flags/lengths/ids only).

        Resolves the active TelegramChannel through the runtime's channel
        manager and relays the read-only observation buffer. Reports
        ``available: False`` when the telegram channel is not active or does
        not expose the observation accessor. Never includes raw message
        content.
        """
        manager = getattr(self.runtime, "channel_manager", None)
        channels = getattr(manager, "channels", None) if manager is not None else None
        channel = channels.get("telegram", None) if isinstance(channels, dict) else None
        if channel is None:
            return {
                "available": False,
                "reason": "telegram channel not active",
            }
        accessor = getattr(channel, "reply_context_observations", None)
        if not callable(accessor):
            return {
                "available": False,
                "reason": "telegram channel lacks observations accessor",
            }
        try:
            data = accessor()
        except Exception as exc:
            return {
                "available": False,
                "reason": f"observations accessor failed: {type(exc).__name__}",
            }
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return {
                "available": False,
                "reason": "telegram observations accessor returned unexpected shape",
            }

        def _entry_summary(entry: dict) -> dict[str, Any]:
            return {
                "ts": self._primitive(entry.get("ts")),
                "chat_id": self._primitive(entry.get("chat_id")),
                "message_id": self._primitive(entry.get("message_id")),
                "reply_to_message_id": self._primitive(
                    entry.get("reply_to_message_id")
                ),
                "reply_id_present": bool(entry.get("reply_id_present", False)),
                "replied_to_bot": entry.get("replied_to_bot"),
                "has_reply_source": bool(entry.get("has_reply_source", False)),
                "context_attached": bool(entry.get("context_attached", False)),
                "text_len": entry.get("text_len"),
                "caption_len": entry.get("caption_len"),
                "quote_len": entry.get("quote_len"),
                "media_count": entry.get("media_count"),
                "media_file_id_present": bool(
                    entry.get("media_file_id_present", False)
                ),
                "content_unavailable": bool(entry.get("content_unavailable", False)),
            }

        last_reply = None
        for entry in reversed(entries):
            if not isinstance(entry, dict):
                continue
            summary = _entry_summary(entry)
            if summary["context_attached"]:
                last_reply = summary
                break
        return {
            "available": True,
            "buffer_entries": len(entries),
            "buffer_limit": self._primitive(data.get("limit")),
            "replies_seen": sum(
                1
                for entry in entries
                if isinstance(entry, dict) and entry.get("context_attached")
            ),
            "replies_seen_total": self._primitive(data.get("total_seen")),
            "last_reply": last_reply,
        }

    def _delegated_work(self) -> dict[str, Any]:
        manager = getattr(self.runtime, "subagents", None)
        statuses: dict[str, Any] = {}
        runtime_statuses = getattr(manager, "runtime_statuses", None)
        if callable(runtime_statuses):
            try:
                statuses = runtime_statuses()
            except Exception:
                statuses = {}
        runs = []
        for task_id, status in statuses.items():
            runs.append({
                "task_id": str(task_id),
                "label": self._primitive(getattr(status, "label", None)),
                "phase": self._primitive(getattr(status, "phase", None)),
                "activity": self._primitive(getattr(status, "activity", None)),
                "iteration": self._primitive(getattr(status, "iteration", None)),
                "stop_reason": self._primitive(getattr(status, "stop_reason", None)),
                "error": self._primitive(getattr(status, "error", None)),
                "effective_budgets": {
                    "available": bool(getattr(status, "effective_budgets", None)),
                    **dict(getattr(status, "effective_budgets", None) or {}),
                },
            })
        queued = (
            manager.get_queued_count()
            if manager is not None and hasattr(manager, "get_queued_count")
            else None
        )
        queue_capacity = self._primitive(getattr(manager, "max_queued_subagents", None))
        executing = (
            manager.get_executing_count()
            if manager is not None and hasattr(manager, "get_executing_count")
            else None
        )
        execution_capacity = self._primitive(
            getattr(manager, "max_concurrent_subagents", None)
        )
        return {
            "available": manager is not None,
            "runs": runs,
            "queue": {
                "available": manager is not None,
                "queued": queued,
                "capacity": queue_capacity,
                "available_slots": (
                    max(0, queue_capacity - queued)
                    if isinstance(queue_capacity, int) and isinstance(queued, int)
                    else None
                ),
            },
            "execution_capacity": {
                "available": manager is not None,
                "in_use": executing,
                "capacity": execution_capacity,
                "available_slots": (
                    max(0, execution_capacity - executing)
                    if isinstance(execution_capacity, int) and isinstance(executing, int)
                    else None
                ),
            },
            "manager_budgets": {
                "max_iterations": self._primitive(getattr(manager, "max_iterations", None)),
                "max_concurrent": self._primitive(
                    getattr(manager, "max_concurrent_subagents", None)
                ),
                "max_tool_result_chars": self._primitive(
                    getattr(manager, "max_tool_result_chars", None)
                ),
            },
        }

    def _cron(self) -> dict[str, Any]:
        service = getattr(self.runtime, "cron_service", None)
        jobs: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        if service is not None:
            try:
                for job in service.list_jobs():
                    try:
                        state = getattr(job, "state", None)
                        history = list(getattr(state, "run_history", None) or [])
                        recent = history[-1] if history else None
                        jobs.append({
                            "id": self._primitive(getattr(job, "id", None)),
                            "name": self._primitive(getattr(job, "name", None)),
                            "enabled": self._primitive(getattr(job, "enabled", None)),
                            "last_status": self._primitive(
                                getattr(state, "last_status", None)
                            ),
                            "last_error": self._primitive(getattr(state, "last_error", None)),
                            "last_run_at_ms": self._primitive(
                                getattr(state, "last_run_at_ms", None)
                            ),
                            "next_run_at_ms": self._primitive(
                                getattr(state, "next_run_at_ms", None)
                            ),
                            "recent_terminal": (
                                {
                                    "run_at_ms": self._primitive(
                                        getattr(recent, "run_at_ms", None)
                                    ),
                                    "status": self._primitive(
                                        getattr(recent, "status", None)
                                    ),
                                    "duration_ms": self._primitive(
                                        getattr(recent, "duration_ms", None)
                                    ),
                                    "error": self._primitive(
                                        getattr(recent, "error", None)
                                    ),
                                }
                                if recent is not None
                                else None
                            ),
                            "delivery": {
                                "available": True,
                                "status": self._primitive(
                                    getattr(state, "last_delivery_status", None)
                                ),
                                "error": self._primitive(
                                    getattr(state, "last_delivery_error", None)
                                ),
                            },
                        })
                    except Exception as exc:
                        errors.append({
                            "job_id": self._primitive(getattr(job, "id", None)),
                            "error": f"{type(exc).__name__}: {exc}",
                        })
            except Exception as exc:
                errors.append({"job_id": None, "error": f"{type(exc).__name__}: {exc}"})
        return {"available": service is not None, "jobs": jobs, "errors": errors}

    def _environment(self) -> dict[str, Any]:
        exec_config = getattr(self.runtime, "exec_config", None)
        allowed = getattr(exec_config, "allowed_env_keys", None)
        return {
            "available": exec_config is not None,
            "allowed_names": sorted(str(name) for name in allowed or []),
        }

    def _repository(self) -> dict[str, Any]:
        workspace = Path(getattr(self.runtime, "workspace", Path.cwd()))
        source = Path(__file__).resolve().parents[3]
        repository = self._repository_at(source)
        repository["workspace"] = self._repository_at(workspace)
        return repository

    def _repository_at(self, path: Path) -> dict[str, Any]:
        root = self._git(path, "rev-parse", "--show-toplevel")
        if root is None:
            return {
                "available": False,
                "path": str(path),
                "branch": None,
                "commit": None,
                "dirty": None,
                "upstream": None,
                "origin": None,
                "ahead": None,
                "behind": None,
            }
        root_path = Path(root)
        upstream = self._git(root_path, "rev-parse", "--abbrev-ref", "@{upstream}")
        divergence = (
            self._git(root_path, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
            if upstream
            else None
        )
        behind = ahead = None
        if divergence:
            parts = divergence.split()
            if len(parts) == 2:
                behind, ahead = (int(parts[0]), int(parts[1]))
        status = self._git(root_path, "status", "--porcelain")
        return {
            "available": True,
            "path": root,
            "branch": self._git(root_path, "branch", "--show-current"),
            "commit": self._git(root_path, "rev-parse", "HEAD"),
            "dirty": bool(status),
            "upstream": upstream,
            "origin": self._git(root_path, "remote", "get-url", "origin"),
            "ahead": ahead,
            "behind": behind,
        }

    def _capabilities(self) -> dict[str, Any]:
        tools = getattr(self.runtime, "tools", None)
        tool_names = getattr(tools, "tool_names", None)
        skills_loader = getattr(getattr(self.runtime, "context", None), "skills", None)
        skills = []
        if skills_loader is not None:
            try:
                skills = [entry["name"] for entry in skills_loader.list_skills()]
            except Exception:
                skills = []
        context_config = getattr(getattr(self.runtime, "context", None), "context_retrieval", None)
        return {
            "tools": sorted(str(name) for name in tool_names or []),
            "skills": sorted(skills),
            "context": {
                "mode": self._primitive(getattr(context_config, "mode", None))
                or "all_pinned",
                "constitutional_budget_chars": self._primitive(
                    getattr(context_config, "constitutional_budget_chars", None)
                ),
                "current_budget_chars": self._primitive(
                    getattr(context_config, "current_budget_chars", None)
                ),
                "retrieved_budget_chars": self._primitive(
                    getattr(context_config, "retrieved_budget_chars", None)
                ),
            },
        }

    @staticmethod
    def _git(cwd: Path, *args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    @staticmethod
    def _primitive(value: Any) -> str | int | float | bool | None:
        return value if isinstance(value, (str, int, float, bool, type(None))) else None


def render_snapshot(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=True)
