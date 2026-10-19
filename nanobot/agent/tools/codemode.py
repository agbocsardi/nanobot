"""Python orchestration with deferred tool discovery and a fail-closed OS sandbox."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import Field

from nanobot.agent.tools._codemode_sandbox import (
    DEFAULT_BWRAP_PATH,
    sandbox_command,
    validate_bwrap,
    worker_exit_error,
)
from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext, current_request_session_key
from nanobot.config_base import Base
from nanobot.security.workspace_access import current_tool_workspace
from nanobot.utils.helpers import maybe_persist_tool_result

# Bound by AgentRunner for one outer invocation, never shared between sessions.
CodemodeDispatch = Callable[[str, dict[str, Any], int], Awaitable[dict[str, Any]]]


@dataclass
class CodemodeExecution:
    registry: Any
    dispatch: CodemodeDispatch
    active: bool = True


CODEMODE_EXECUTION: ContextVar[CodemodeExecution | None] = ContextVar("codemode_execution", default=None)
_MAX_FRAME_BYTES = 4 * 1024 * 1024
_MAX_OUTPUT_BYTES = 1024 * 1024
_MAX_WORKERS = 2
_active_workers = 0


class CodemodeConfig(Base):
    enable: bool = False
    bwrap_path: str = Field(default=DEFAULT_BWRAP_PATH, description="Absolute Bubblewrap path; PATH is never searched")
    timeout_s: float = Field(default=60, ge=1, le=600)
    memory_mb: int = Field(default=256, ge=64, le=2048)
    max_output_chars: int = Field(default=16000, ge=1000, le=100000)
    max_tool_calls: int = Field(default=64, ge=1, le=256)
    max_concurrency: int = Field(default=4, ge=1, le=16)


@tool_parameters({
    "type": "object",
    "properties": {
        "code": {"type": "string", "maxLength": 65536, "description": "Python async function body; top-level await and return work."},
    },
    "required": ["code"],
    "additionalProperties": False,
})
class CodemodeTool(Tool):
    """Compose calls without exposing every MCP tool's schema to the model."""

    _scopes = {"core", "subagent"}
    config_key = "codemode"

    def __init__(self, config: CodemodeConfig, registry: Any, workspace: Path):
        self.config = config
        self.registry = registry
        self.workspace = workspace

    @classmethod
    def config_cls(cls) -> type[Base]:
        return CodemodeConfig

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return ctx.config.codemode.enable

    @classmethod
    def create(cls, ctx: ToolContext) -> CodemodeTool:
        return cls(ctx.config.codemode, ctx.registry, Path(ctx.workspace))

    @property
    def name(self) -> str:
        return "codemode"

    @property
    def description(self) -> str:
        return (
            "Run Python to discover and compose tools, especially MCP tools whose schemas are hidden "
            "to save context. Input is an async function body: top-level await/return work. "
            "Use await search_tools(query, namespace=None, limit=8, offset=0) to find names and short descriptions; "
            "await describe_tool(name) returns the full description and JSON parameter schema. "
            "Search with an empty query and namespace='mcp' to browse MCP tools. "
            "Call await tools.exact_name(arg=value) or await tools['exact_name'](arg=value). "
            "Results are dicts with content, status, data and operational metadata; structured MCP "
            "responses are in data (content, isError, and structuredContent when the server supplies it). "
            "Unsuccessful outcomes raise ToolError; its .result preserves the result dict. "
            "asyncio and json are available; asyncio.gather(..., return_exceptions=True) handles "
            "independent calls. Only text(value), print(), and a non-None return reach the model: "
            "filter large results before emitting. No workspace/home contents, credentials or host network are exposed; "
            "access them through tools, subject to their usual policies and approvals. "
            "No recursive codemode calls. Side effects are real, not rolled back on script failure. "
            f"Limits: {self.config.timeout_s:g}s, {self.config.max_tool_calls} discovery/tool requests, "
            f"{self.config.max_concurrency} concurrent safe calls."
        )

    @property
    def read_only(self) -> bool:
        # The orchestration envelope does not mutate; every inner call is checked separately.
        return True

    @property
    def exclusive(self) -> bool:
        return True

    def _describe(self, name: Any) -> dict[str, Any]:
        if not isinstance(name, str) or name == self.name:
            raise ValueError("Use an exact non-codemode tool name")
        tool = self.registry.get(name)
        if tool is None:
            raise ValueError(f"Unknown tool: {name}")
        return {"name": tool.name, "description": tool.description, "parameters": tool.parameters}

    def _search(self, request: dict[str, Any]) -> list[dict[str, str]]:
        query = request.get("query", "")
        namespace = request.get("namespace")
        limit = request.get("limit", 8)
        offset = request.get("offset", 0)
        if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= 100000:
            raise ValueError("offset must be between 0 and 100000")
        if not isinstance(query, str) or len(query) > 1000:
            raise ValueError("query must be a string of at most 1000 characters")
        if namespace is not None and not isinstance(namespace, str):
            raise ValueError("namespace must be a string")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        terms = set(re.findall(r"\w+", query.lower()))
        matches = []
        for name in self.registry.tool_names:
            if name == self.name:
                continue
            tool = self.registry.get(name)
            server = getattr(tool, "_server_name", None)
            if namespace and not (
                namespace == server or (namespace == "mcp" and name.startswith("mcp_"))
            ):
                continue
            description = tool.description
            words = set(re.findall(r"\w+", name.lower().replace("_", " ") + " " + description.lower()))
            score = len(terms & words) + sum(term in name.lower() for term in terms)
            if terms and not score:
                continue
            matches.append((-score, name, description))
        matches.sort()
        return [{"name": name, "description": description[:300]} for _, name, description in matches[offset:offset + limit]]

    async def execute(self, code: str, **kwargs: Any) -> ToolResult:
        global _active_workers
        execution = CODEMODE_EXECUTION.get()
        if execution is None or not execution.active or execution.registry is not self.registry:
            return ToolResult.policy_block("Codemode requires an active AgentRunner execution context for this registry")
        if _active_workers >= _MAX_WORKERS:
            return ToolResult.retryable_error("Codemode worker capacity reached; retry after an active script ends")
        _active_workers += 1
        start = time.monotonic()
        output: list[str] = []
        try:
            async with asyncio.timeout(self.config.timeout_s):
                await self._run_worker(code, execution.dispatch, output)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            status = "retryable_error"
            summary = f"Script failed: timed out after {self.config.timeout_s:g}s"
        except Exception as exc:
            status = "retryable_error"
            summary = f"Script failed: {type(exc).__name__}: {exc}"
        else:
            status = "success"
            summary = "Script completed"
        finally:
            _active_workers -= 1
        content = f"{summary}\nWall time: {time.monotonic() - start:.3f}s\n" + "".join(output)
        content = maybe_persist_tool_result(
            current_tool_workspace(self.workspace).project_path,
            current_request_session_key(), f"codemode-{uuid.uuid4().hex}",
            content, max_chars=self.config.max_output_chars,
        )
        return ToolResult(str(content), status=status)

    async def _run_worker(
        self, code: str, dispatch: CodemodeDispatch, output: list[str],
    ) -> None:
        tasks: set[asyncio.Task] = set()
        proc = None
        stderr_task = None
        output_bytes = 0
        request_bytes = 0
        messages = 0
        requests = 0
        ids: set[int] = set()
        writer_lock = asyncio.Lock()
        condition = asyncio.Condition()
        queue: deque[int] = deque()
        active = 0
        exclusive = False

        async def scheduled_reply(request: dict[str, Any], safe: bool) -> None:
            nonlocal active, exclusive
            request_id = request["id"]
            async with condition:
                queue.append(request_id)
                await condition.wait_for(lambda: (
                    queue[0] == request_id and not exclusive
                    and active < self.config.max_concurrency and (safe or active == 0)
                ))
                queue.popleft()
                active += 1
                exclusive = not safe
                condition.notify_all()
            try:
                await reply(request)
            finally:
                async with condition:
                    active -= 1
                    if not safe:
                        exclusive = False
                    condition.notify_all()

        async def reply(request: dict[str, Any]) -> None:
            request_id = request["id"]
            try:
                operation = request.get("operation")
                if operation == "search":
                    result = self._search(request)
                elif operation == "describe":
                    result = self._describe(request.get("name"))
                elif operation == "call":
                    name = request.get("name")
                    arguments = request.get("arguments")
                    if not isinstance(name, str) or name == self.name or not isinstance(arguments, dict):
                        raise ValueError("Tool requests require a non-codemode name and argument object")
                    result = await dispatch(name, arguments, request_id)
                else:
                    raise ValueError("Unknown bridge operation")
                response = {"id": request_id, "ok": True, "result": result}
                encoded = json.dumps(response, ensure_ascii=True, allow_nan=False).encode() + b"\n"
                if len(encoded) > _MAX_FRAME_BYTES:
                    raise ValueError("Tool result exceeds the 4 MiB bridge limit; request less data")
            except Exception as exc:
                encoded = json.dumps({"id": request_id, "ok": False, "error": str(exc)[:4000]}).encode() + b"\n"
            async with writer_lock:
                proc.stdin.write(encoded)
                await proc.stdin.drain()

        async def read_stderr() -> bytes:
            captured = bytearray()
            while chunk := await proc.stderr.read(4096):
                captured.extend(chunk)
                if len(captured) > 65536:
                    proc.kill()
                    raise RuntimeError("Worker stderr exceeded 64 KiB")
            return bytes(captured)

        try:
            binary = await validate_bwrap(self.config.bwrap_path)
            with sandbox_command(binary) as (argv, seccomp_fd):
                proc = await asyncio.create_subprocess_exec(
                    *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, pass_fds=(seccomp_fd,),
                    env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                    limit=_MAX_FRAME_BYTES, start_new_session=True,
                )
            stderr_task = asyncio.create_task(read_stderr())
            payload = {"code": code, "memory_mb": self.config.memory_mb, "timeout_s": self.config.timeout_s}
            try:
                proc.stdin.write(json.dumps(payload).encode() + b"\n")
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                # A setup failure can close stdin first; collect Bubblewrap's
                # actual diagnostic from stderr through the EOF path below.
                pass
            while line := await proc.stdout.readline():
                messages += 1
                if len(line) > _MAX_FRAME_BYTES or messages > 10000:
                    raise ValueError("Worker protocol limit exceeded")
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("Worker messages must be objects")
                kind = request.get("type")
                if kind == "output":
                    value = request.get("text")
                    if not isinstance(value, str):
                        raise ValueError("Worker output must be text")
                    output_bytes += len(value.encode())
                    if output_bytes > _MAX_OUTPUT_BYTES:
                        raise ValueError("Script output exceeded 1 MiB; filter results before emitting")
                    output.append(value)
                elif kind == "request":
                    request_id = request.get("id")
                    requests += 1
                    request_bytes += len(line)
                    if (request_bytes > 8 * 1024 * 1024
                            or requests > self.config.max_tool_calls or not isinstance(request_id, int)
                            or isinstance(request_id, bool) or request_id < 1 or request_id in ids):
                        raise ValueError("Discovery/tool request limit exceeded or invalid request ID")
                    ids.add(request_id)
                    tool = self.registry.get(request.get("name")) if isinstance(request.get("name"), str) else None
                    safe = request.get("operation") != "call" or bool(tool and tool.concurrency_safe)
                    tasks.add(asyncio.create_task(scheduled_reply(request, safe)))
                elif kind == "finished":
                    # Unawaited calls are cancelled, not allowed to outlive the script.
                    if request.get("ok") is not True:
                        raise RuntimeError(str(request.get("error", "Script failed"))[:8000])
                    return
                else:
                    raise ValueError("Unknown worker message")
            await proc.wait()
            stderr = (await stderr_task).decode(errors="replace").strip()
            raise worker_exit_error(binary, proc.returncode, stderr)
        finally:
            for task in tasks:
                task.cancel()
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if proc is not None:
                await proc.wait()
            if stderr_task is not None:
                await asyncio.gather(stderr_task, return_exceptions=True)
