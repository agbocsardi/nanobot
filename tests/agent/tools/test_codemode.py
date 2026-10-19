"""Python equivalents of Pi's codemode lifetime/discovery contracts, plus OS isolation."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from mcp.types import Tool as MCPTool

from nanobot.agent.hook import AgentHook
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools import codemode
from nanobot.agent.tools._codemode_sandbox import (
    DEFAULT_BWRAP_PATH,
    sandbox_command,
    validate_bwrap,
)
from nanobot.agent.tools.action_receipts import ActionReceiptStore
from nanobot.agent.tools.approval import ApprovalStore
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.codemode import CodemodeConfig, CodemodeTool
from nanobot.agent.tools.context import (
    RequestContext,
    bind_request_context,
    current_request_context,
    reset_request_context,
)
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.mcp import MCPToolWrapper
from nanobot.agent.tools.policy import ToolPolicy
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import ToolsConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest


class _Tool(Tool):
    def __init__(self, name="echo", *, handler=None, read_only=True):
        self._name = name
        self.handler = handler or AsyncMock(return_value=ToolResult("raw payload", data={"value": 42}))
        self._read_only = read_only

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return f"{self.name} documentation " + "large schema description " * 100

    @property
    def parameters(self):
        return {
            "type": "object", "properties": {"value": {"type": "string"}, "path": {"type": "string"}},
            "additionalProperties": False,
        }

    @property
    def read_only(self):
        return self._read_only

    def effect_for(self, params):
        return "read" if self.read_only else "external_write"

    async def execute(self, **kwargs):
        return await self.handler(**kwargs)


@pytest.fixture
def registry(tmp_path: Path):
    registry = ToolRegistry(receipt_store=ActionReceiptStore(tmp_path))
    registry.register(CodemodeTool(CodemodeConfig(enable=True), registry, tmp_path))
    return registry


@pytest.fixture(scope="module")
async def sandbox_available():
    if sys.platform != "linux" or not Path(DEFAULT_BWRAP_PATH).is_file():
        pytest.skip("Bubblewrap integration requires Linux and /usr/bin/bwrap")
    try:
        binary = await validate_bwrap(DEFAULT_BWRAP_PATH)
    except RuntimeError as exc:
        if "no user-namespace-authorized AppArmor profile" in str(exc):
            pytest.skip(str(exc))
        raise
    with sandbox_command(binary) as (argv, fd):
        result = subprocess.run(
            argv, pass_fds=(fd,), input=json.dumps({"code": "return 1", "memory_mb": 256, "timeout_s": 3}),
            text=True, capture_output=True, timeout=5,
        )
    if result.returncode and any(message in result.stderr.lower() for message in (
        "operation not permitted", "permission denied", "no permissions",
    )):
        pytest.skip(f"Host blocks Bubblewrap user namespaces: {result.stderr.strip()}")
    assert result.returncode == 0, result.stderr
    assert '"finished", "ok": true' in result.stdout, result.stdout


async def _execute(registry, code, *, call_id="outer", concurrent=True, **spec_kwargs):
    results, events, _targets, error = await AgentRunner(MagicMock())._execute_tools(
        AgentRunSpec(
            initial_messages=[], tools=registry, model="test", max_iterations=1,
            max_tool_result_chars=16000, concurrent_tools=concurrent, **spec_kwargs,
        ),
        [ToolCallRequest(id=call_id, name="codemode", arguments={"code": code})], {}, {},
    )
    return results, events, error


def test_mcp_schemas_are_deferred_and_cache_tracks_registration(registry):
    registry.register(_Tool("local"))
    registry.register(_Tool("mcp_docs_search"))
    assert [d["function"]["name"] for d in registry.get_definitions()] == ["codemode", "local"]
    assert "mcp_docs_search documentation" not in json.dumps(registry.get_definitions())
    assert registry.get("mcp_docs_search") is not None
    registry.unregister("codemode")
    assert "mcp_docs_search" in [d["function"]["name"] for d in registry.get_definitions()]


def test_model_schema_size_is_constant_as_mcp_catalog_grows(registry):
    before = json.dumps(registry.get_definitions())
    for index in range(100):
        registry.register(_Tool(f"mcp_catalog_tool_{index}"))
    assert json.dumps(registry.get_definitions()) == before


def test_discovery_is_bounded_paginated_and_uses_live_registry(registry):
    for index in range(30):
        registry.register(_Tool(f"mcp_docs_search_{index:02}"))
    tool = registry.get("codemode")
    first = tool._search({"namespace": "mcp", "limit": 20})
    second = tool._search({"namespace": "mcp", "limit": 20, "offset": 20})
    assert len(first) == 20 and len(second) == 10
    assert all(len(item["description"]) <= 300 for item in first)
    assert tool._search({"query": "missingtopic"}) == []
    assert len(tool._describe(first[0]["name"])["description"]) > 300
    registry.unregister(first[0]["name"])
    assert len(tool._search({"namespace": "mcp", "limit": 20, "offset": 20})) == 9
    _, _, error = registry.prepare_call("missingtool", {})
    assert "search_tools()" in error
    assert "mcp_docs_search_29" not in error


@pytest.mark.parametrize("search_request", [{"limit": 21}, {"offset": -1}, {"query": []}, {"namespace": 5}])
def test_discovery_rejects_invalid_arguments(registry, search_request):
    with pytest.raises(ValueError):
        registry.get("codemode")._search(search_request)


def test_config_defaults_and_plugin_loading(tmp_path):
    from nanobot.agent.tools.context import ToolContext
    config = ToolsConfig()
    assert not config.codemode.enable
    registry = ToolRegistry()
    loader = ToolLoader(test_classes=[CodemodeTool])
    ctx = ToolContext(config=config, workspace=str(tmp_path))
    assert loader.load(ctx, registry) == []
    config.codemode.enable = True
    assert loader.load(ctx, registry) == ["codemode"]
    assert registry.get("codemode").registry is registry
    assert ToolsConfig.model_validate({"codemode": {"timeoutS": 5}}).codemode.timeout_s == 5


async def test_direct_registry_use_fails_closed(registry):
    result = await registry.execute("codemode", {"code": "return 1"})
    assert result.status == "policy_block"


async def test_missing_sandbox_never_runs_native(registry, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    registry.get("codemode").config.bwrap_path = "/nonexistent/nanobot-bwrap"
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    results, events, _ = await _execute(registry, "return 1")
    assert "Bubblewrap executable /nonexistent/nanobot-bwrap is unavailable" in results[0]
    assert events[0]["status"] != "success"
    spawn.assert_not_awaited()
    assert codemode._active_workers == 0


@pytest.mark.parametrize("reason", ["no user-namespace-authorized AppArmor profile", "Unsupported Bubblewrap 0.8.0"])
async def test_broken_binary_validation_never_starts_a_worker(registry, monkeypatch, reason):
    validation = AsyncMock(side_effect=RuntimeError(reason))
    spawn = AsyncMock()
    monkeypatch.setattr(codemode, "validate_bwrap", validation)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    results, events, _ = await _execute(registry, "return 1")
    assert reason in results[0] and events[0]["status"] != "success"
    validation.assert_awaited_once_with(DEFAULT_BWRAP_PATH)
    spawn.assert_not_awaited()
    assert codemode._active_workers == 0


@pytest.mark.parametrize("active, same_registry", [(False, True), (True, False)])
async def test_stale_or_other_registry_execution_context_is_rejected(registry, active, same_registry):
    execution = codemode.CodemodeExecution(
        registry if same_registry else ToolRegistry(), AsyncMock(), active=active,
    )
    token = codemode.CODEMODE_EXECUTION.set(execution)
    try:
        result = await registry.execute("codemode", {"code": "return 1"})
    finally:
        codemode.CODEMODE_EXECUTION.reset(token)
    assert result.status == "policy_block"
    execution.dispatch.assert_not_awaited()


async def test_worker_capacity_is_fail_fast(registry, monkeypatch):
    monkeypatch.setattr(codemode, "_active_workers", 2)
    results, _, _ = await _execute(registry, "return 1")
    assert "capacity reached" in results[0]
    assert codemode._active_workers == 2


@pytest.mark.parametrize("read_only", [False, True])
async def test_mcp_preserves_structured_content_errors_and_mutation_hint(read_only):
    response = CallToolResult(
        content=[TextContent(type="text", text="display")],
        structuredContent={"items": [{"id": 1}]}, isError=True,
    )
    wrapper = MCPToolWrapper(
        SimpleNamespace(call_tool=AsyncMock(return_value=response)), "docs",
        MCPTool(name="search", inputSchema={"type": "object"}, annotations=ToolAnnotations(readOnlyHint=read_only)),
    )
    result = await wrapper.execute()
    assert result == "display" and result.status == "retryable_error"
    assert result.data["structuredContent"] == {"items": [{"id": 1}]}
    assert result.data["isError"] is True
    assert wrapper.concurrency_safe is read_only
    assert wrapper.effect_for({}) == ("read" if read_only else "external_write")


@pytest.mark.usefixtures("sandbox_available")
class TestSandbox:
    async def test_async_calls_discovery_and_only_selected_output_reach_transcript(self, registry):
        registry.register(_Tool("mcp_docs_search"))
        provider = MagicMock()
        provider.chat_with_retry = AsyncMock(side_effect=[
            LLMResponse(content="", tool_calls=[ToolCallRequest(id="outer", name="codemode", arguments={"code": (
                'found = await search_tools("docs", namespace="mcp")\n'
                'schema = await describe_tool(found[0]["name"])\n'
                'result = await tools[schema["name"]]()\n'
                'return result["data"]["value"]'
            )})]),
            LLMResponse(content="done"),
        ])
        result = await AgentRunner(provider).run(AgentRunSpec(
            initial_messages=[{"role": "user", "content": "go"}], tools=registry, model="test",
            max_iterations=2, max_tool_result_chars=16000, concurrent_tools=True,
        ))
        messages = [m for m in result.messages if m["role"] == "tool"]
        assert len(messages) == 1
        assert "42" in messages[0]["content"] and "raw payload" not in messages[0]["content"]
        child = next(e for e in result.tool_events if e["name"] == "mcp_docs_search")
        assert child["parent_call_id"] == "outer" and child["call_id"].startswith("outer/codemode/")
        assert "data" not in child
        assert "mcp_docs_search" in result.tools_used
        for call in provider.chat_with_retry.await_args_list:
            assert "mcp_docs_search" not in json.dumps(call.kwargs["tools"])

    @pytest.mark.parametrize("code, expected", [
        ('text("first")\nprint("second")\nreturn {"third": 3}', 'first\nsecond\n{"third": 3}'),
        ('text("before")\nraise ValueError("broken")', "before"),
        ('if invalid syntax', "SyntaxError"),
        ('return {1, 2}', "not JSON serializable"),
        ('# comment-only script', "Script completed"),
        ('text("first")\nraise ValueError("line-check")', '<codemode>\", line 2'),
    ])
    async def test_output_and_errors(self, registry, code, expected):
        results, _, _ = await _execute(registry, code)
        assert expected in results[0]

    async def test_policy_and_invalid_arguments_remain_catchable_and_recorded(self, registry):
        blocked = _Tool("mcp_private_write", read_only=False)
        registry.register(blocked)
        registry.policy = ToolPolicy([{"tool": blocked.name, "outcome": "deny", "id": "deny-write"}])
        results, events, _ = await _execute(registry, (
            'try:\n    await tools.mcp_private_write()\n'
            'except ToolError as error:\n    text(error.result["status"])\n'
            'try:\n    await tools.mcp_private_write(unknown=1)\n'
            'except ToolError as error:\n    text(error.result["status"])'
        ))
        assert "policy_block" in results[0] and "invalid_arguments" in results[0]
        assert events[0]["status"] == "success"
        assert [e["status"] for e in events[1:]] == ["policy_block", "invalid_arguments"]
        blocked.handler.assert_not_awaited()

    async def test_inner_policy_approval_survives_audit_mode(self, registry):
        write = _Tool("mcp_write", read_only=False)
        registry.register(write)
        registry.policy = ToolPolicy([
            {"id": "approve-write", "tool": write.name, "outcome": "ask", "mode": "audit"},
        ])
        token = bind_request_context(RequestContext(
            channel="cli", chat_id="chat", session_key="audit-session",
            metadata={"interaction_mode": "audit"}, approvals=ApprovalStore(),
        ))
        try:
            results, events, _ = await _execute(registry, 'await tools.mcp_write()')
        finally:
            reset_request_context(token)
        assert "Script failed" in results[0] and "/policy approve" in results[0]
        assert events[1]["evidence"][0]["approval_token"]
        write.handler.assert_not_awaited()

    async def test_catching_inner_error_cannot_hide_fail_on_tool_error(self, registry):
        registry.register(_Tool(handler=AsyncMock(return_value=ToolResult.retryable_error("failure"))))
        _, _, fatal = await _execute(registry, (
            'try:\n    await tools.echo()\nexcept ToolError: pass'
        ), fail_on_tool_error=True)
        assert fatal is not None

    async def test_nested_events_do_not_shift_outer_hook_pairing(self, registry):
        registry.register(_Tool())
        provider = MagicMock()
        provider.chat_with_retry = AsyncMock(side_effect=[
            LLMResponse(content="", tool_calls=[
                ToolCallRequest(id="outer", name="codemode", arguments={"code": "await tools.echo()"}),
                ToolCallRequest(id="direct", name="echo", arguments={}),
            ]),
            LLMResponse(content="done"),
        ])
        seen = []

        class Capture(AgentHook):
            async def after_iteration(self, context):
                if context.tool_calls:
                    seen.extend(e["name"] for e in context.tool_events)

        result = await AgentRunner(provider).run(AgentRunSpec(
            initial_messages=[{"role": "user", "content": "go"}], tools=registry, model="test",
            max_iterations=2, max_tool_result_chars=16000, hook=Capture(),
        ))
        assert seen == ["codemode", "echo"]
        assert len(result.tool_events) == 3
        assert len([m for m in result.messages if m["role"] == "tool"]) == 2

    @pytest.mark.parametrize("status, stop_reason", [("nonzero_exit", None), ("ask_user", "ask_user")])
    async def test_nonfailure_control_results_are_returned_as_data(self, registry, status, stop_reason):
        registry.register(_Tool(handler=AsyncMock(return_value=ToolResult("result", status=status))))
        results, events, error = await _execute(registry, 'return (await tools.echo())["status"]')
        assert results[0].splitlines()[-1] == status and error is None
        assert events[0]["status"] == "success"
        assert AgentRunner._terminal_tool_stop_reason(events) == stop_reason

    @pytest.mark.parametrize("recovered", [False, True])
    async def test_nested_targets_remain_aligned_and_cross_tool_recovery_is_honored(self, registry, recovered):
        registry.register(_Tool("first", handler=AsyncMock(return_value=ToolResult.retryable_error("failed"))))
        registry.register(_Tool("second"))
        registry.register(_Tool("after"))
        recovery_path = "/same" if recovered else "/different"
        code = (
            'try:\n    await tools.first(path="/same")\nexcept ToolError: pass\n'
            f'await tools.second(path={recovery_path!r})\nreturn "done"'
        )
        runner = AgentRunner(MagicMock())
        _, events, targets, error = await runner._execute_tools(
            AgentRunSpec(initial_messages=[], tools=registry, model="test", max_iterations=1,
                         max_tool_result_chars=16000),
            [ToolCallRequest(id="outer", name="codemode", arguments={"code": code}),
             ToolCallRequest(id="after", name="after", arguments={"path": "/other"})], {}, {},
        )
        assert error is None
        assert [event["name"] for event in events] == ["codemode", "first", "second", "after"]
        assert targets == [[], ["/same"], [recovery_path], ["/other"]]
        assert runner._terminal_tool_stop_reason(events, targets) == (None if recovered else "partial_completion")

    async def test_caught_bad_arguments_do_not_trigger_a_verification_turn(self, registry):
        registry.register(_Tool())
        code = 'try:\n    await tools.echo(unknown=1)\nexcept ToolError: pass\nreturn "handled"'
        provider = MagicMock()
        provider.chat_with_retry = AsyncMock(side_effect=[
            LLMResponse(content="", tool_calls=[ToolCallRequest(id="outer", name="codemode", arguments={"code": code})]),
            LLMResponse(content="Done."),
        ])
        result = await AgentRunner(provider).run(AgentRunSpec(
            initial_messages=[{"role": "user", "content": "go"}], tools=registry, model="test",
            max_iterations=3, max_tool_result_chars=16000,
        ))
        assert result.stop_reason == "completed" and provider.chat_with_retry.await_count == 2
        assert [event["status"] for event in result.tool_events] == ["success", "invalid_arguments"]

    async def test_read_calls_overlap_and_writes_are_ordering_barriers(self, registry):
        order = []
        both = asyncio.Event()
        readers = 0

        async def read():
            nonlocal readers
            readers += 1
            order.append("read-start")
            if readers == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 2)
            order.append("read-end")
            return "read"

        async def write():
            order.append("write")
            return "written"

        registry.register(_Tool("read", handler=read))
        registry.register(_Tool("write", handler=write, read_only=False))
        results, _, _ = await _execute(registry, (
            'await asyncio.gather(tools.read(), tools.read(), tools.write(), tools.read())\nreturn "done"'
        ))
        assert "Script completed" in results[0]
        assert order[:2] == ["read-start", "read-start"]
        assert order.index("write") == 4 and order[5:] == ["read-start", "read-end"]

    async def test_structured_mcp_response_can_be_filtered_without_parsing_display_text(self, registry):
        session = SimpleNamespace(call_tool=AsyncMock(return_value=CallToolResult(
            content=[TextContent(type="text", text="unfiltered " * 10000)],
            structuredContent={"rows": [{"id": 7}]},
        )))
        registry.register(MCPToolWrapper(session, "docs", MCPTool(
            name="search", inputSchema={"type": "object"}, annotations=ToolAnnotations(readOnlyHint=True),
        )))
        results, events, _ = await _execute(registry, (
            'return (await tools.mcp_docs_search())["data"]["structuredContent"]["rows"][0]["id"]'
        ))
        assert results[0].splitlines()[-1] == "7"
        assert "unfiltered" not in results[0]
        assert "data" not in events[1]

    async def test_execution_context_is_revoked_after_script_completion(self, registry):
        captured = []

        async def capture():
            captured.append(codemode.CODEMODE_EXECUTION.get())
            return "captured"

        registry.register(_Tool("capture", handler=capture))
        await _execute(registry, "await tools.capture()")
        assert captured[0].registry is registry and not captured[0].active
        assert codemode.CODEMODE_EXECUTION.get() is None

    async def test_concurrent_sessions_keep_their_own_context(self, registry):
        async def who():
            return ToolResult("context", data=current_request_context().sender_id)

        registry.register(_Tool("who", handler=who))

        async def run(sender):
            token = bind_request_context(RequestContext(
                channel="cli", chat_id=sender, session_key=sender, sender_id=sender,
            ))
            try:
                return await _execute(registry, 'return (await tools.who())["data"]', call_id=sender)
            finally:
                reset_request_context(token)

        alice, bob = await asyncio.gather(run("alice"), run("bob"))
        assert alice[0][0].splitlines()[-1] == "alice"
        assert bob[0][0].splitlines()[-1] == "bob"

    async def test_concurrent_burst_admits_only_two_workers(self, registry):
        results = await asyncio.gather(*(_execute(registry, "return 1") for _ in range(6)))
        assert sum(events[0]["status"] == "success" for _, events, _ in results) == 2
        assert sum("capacity reached" in outputs[0] for outputs, _, _ in results) == 4
        assert codemode._active_workers == 0

    async def test_nested_calls_keep_session_sender_context(self, registry):
        async def who():
            ctx = current_request_context()
            return ToolResult("context", data={"session": ctx.session_key, "sender": ctx.sender_id})

        registry.register(_Tool("who", handler=who, read_only=False))
        token = bind_request_context(RequestContext(
            channel="telegram", chat_id="chat", session_key="owner", sender_id="alice",
        ))
        try:
            results, _, _ = await _execute(registry, 'return (await tools.who())["data"]')
        finally:
            reset_request_context(token)
        assert '"session": "owner", "sender": "alice"' in results[0]
        receipt = registry.receipt_store.get("outer/codemode/1")
        assert receipt.session_key == "owner" and receipt.sender_id == "alice"

    async def test_receipts_replay_nested_mutation_without_reexecuting(self, registry):
        write = _Tool("mcp_write", read_only=False)
        registry.register(write)
        code = 'await tools.mcp_write()\nreturn "done"'
        await _execute(registry, code)
        await _execute(registry, code)
        write.handler.assert_awaited_once()
        assert registry.receipt_store.get("outer/codemode/1").status == "succeeded"

    @pytest.mark.parametrize("code", ["while True: pass", "await asyncio.Future()", "return len(bytearray(1024**3))"])
    async def test_runaway_cpu_stalled_await_and_memory_are_bounded(self, registry, code):
        registry.get("codemode").config.timeout_s = 1
        results, events, _ = await asyncio.wait_for(_execute(registry, code), 5)
        assert "Script failed" in results[0] and events[0]["status"] != "success"
        assert codemode._active_workers == 0

    async def test_output_flood_cannot_be_caught_and_full_partial_output_is_offloaded(self, registry):
        results, _, _ = await asyncio.wait_for(_execute(registry, (
            'while True:\n    try: text("x" * 300000)\n    except Exception: pass'
        )), 5)
        assert "Script output exceeded" in results[0]
        artifacts = list(registry.get("codemode").workspace.glob("**/codemode-*.txt"))
        assert len(artifacts) == 1
        assert 900000 <= artifacts[0].stat().st_size < 1100000
        assert len(results[0]) < 16000

    async def test_external_cancellation_cleans_up_worker_and_marks_receipt_unknown(self, registry, monkeypatch):
        original_spawn = asyncio.create_subprocess_exec
        processes = []

        async def capture_spawn(*args, **kwargs):
            proc = await original_spawn(*args, **kwargs)
            processes.append(proc)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_spawn)
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def wait():
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        registry.register(_Tool("wait", handler=wait, read_only=False))
        pending = asyncio.create_task(_execute(registry, (
            'import ctypes\nctypes.CDLL(None).prctl(1, 0)\nawait tools.wait()'
        )))
        await asyncio.wait_for(started.wait(), 3)
        descendants = []
        todo = [processes[-1].pid]
        while todo:
            pid = todo.pop()
            children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
            descendants.extend(int(child) for child in children)
            todo.extend(int(child) for child in children)
        assert descendants
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 3)
        assert cancelled.is_set() and codemode._active_workers == 0
        assert registry.receipt_store.get("outer/codemode/1").status == "unknown"
        for _ in range(50):
            if not any(Path(f"/proc/{pid}").exists() for pid in descendants):
                break
            await asyncio.sleep(0.01)
        assert not any(Path(f"/proc/{pid}").exists() for pid in descendants)

    async def test_return_cancels_unawaited_mutation(self, registry):
        cancelled = asyncio.Event()

        async def wait():
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        registry.register(_Tool("wait", handler=wait, read_only=False))
        results, events, _ = await asyncio.wait_for(_execute(registry, (
            'asyncio.create_task(tools.wait())\nawait asyncio.sleep(0.1)\nreturn "early"'
        )), 3)
        assert "early" in results[0] and cancelled.is_set()
        assert events[0]["status"] == "success" and events[1]["status"] == "partial"
        assert registry.receipt_store.get("outer/codemode/1").status == "unknown"

    @pytest.mark.parametrize("exec_again", [False, True])
    async def test_setup_capabilities_are_gone_before_user_code_and_cannot_return_on_exec(self, registry, exec_again):
        state = (
            '{key: value.strip() for key, value in '
            '(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines()) '
            'if key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb", "NoNewPrivs")}'
        )
        if exec_again:
            probe = (
                f'from pathlib import Path; import json; state = {state}; '
                'print(json.dumps({"type": "output", "text": json.dumps(state) + "\\n"})); '
                'print(json.dumps({"type": "finished", "ok": True}))'
            )
            code = f'import os, sys\nos.execv(sys.executable, [sys.executable, "-I", "-S", "-c", {probe!r}])'
        else:
            code = f'from pathlib import Path\nreturn {state}'
        results, events, _ = await _execute(registry, code)
        assert events[0]["status"] == "success", results[0]
        values = json.loads(results[0].splitlines()[-1])
        assert len(values) == 6 and values["NoNewPrivs"] == "1"
        assert all(int(value, 16) == 0 for key, value in values.items() if key.startswith("Cap"))

    async def test_host_files_environment_network_processes_and_root_writes_are_blocked(self, registry, monkeypatch):
        secret = registry.get("codemode").workspace / "secret.txt"
        secret.write_text("secret")
        monkeypatch.setenv("NANOBOT_SANDBOX_SECRET", "secret")
        results, _, _ = await _execute(registry, f'''
import os, socket, resource
checks = {{"environment": "NANOBOT_SANDBOX_SECRET" not in os.environ}}
for name, operation in [
    ("file", lambda: open({str(secret)!r}).read()),
    ("proc-root", lambda: open("/proc/1/root" + {str(secret)!r}).read()),
    ("network", lambda: socket.socket()),
    ("fork", lambda: os.fork()),
    ("root", lambda: open("/escape", "w")),
    ("limits", lambda: resource.setrlimit(resource.RLIMIT_AS, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))),
]:
    try:
        operation()
        checks[name] = False
    except (OSError, ValueError):
        checks[name] = True
return checks
''')
        assert "Script completed" in results[0]
        checks = json.loads(results[0].splitlines()[-1])
        assert all(checks.values()), checks

    async def test_recursion_and_request_flood_are_blocked(self, registry):
        results, _, _ = await _execute(registry, 'await tools.codemode(code="return 1")')
        assert "non-codemode" in results[0]
        registry.get("codemode").config.max_tool_calls = 2
        results, _, _ = await _execute(registry, 'for _ in range(3): await search_tools()')
        assert "request limit exceeded" in results[0]

    async def test_successive_scripts_do_not_share_globals_or_tmp(self, registry):
        await _execute(registry, 'globals()["shared"] = True\nopen("/tmp/shared", "w").close()')
        results, _, _ = await _execute(registry, 'import os\nreturn ["shared" in globals(), os.path.exists("/tmp/shared")]')
        assert "[false, false]" in results[0]
