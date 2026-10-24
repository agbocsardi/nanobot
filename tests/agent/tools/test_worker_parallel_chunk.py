"""Deterministic bounded fan-out and single-call chunks; fake providers only."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools import _readonly_worker as workers
from nanobot.agent.tools import codemode
from nanobot.agent.tools._readonly_worker import (
    WORKER_RUN,
    ArtifactTool,
    ReadonlyWorkerConfig,
    WorkerRegistry,
    child_slot,
    run_ask,
    run_worker,
)
from nanobot.providers.base import LLMResponse

from . import test_readonly_workers as worker_tests
from .test_codemode import sandbox_available  # noqa: F401
from .test_readonly_workers import (
    FakeProvider,
    parent_registry,
    spec,
    tool_call,
)

worker_context = worker_tests.worker_context


def assert_released(run):
    assert run.active_children == 0
    assert run.slots._value == run.config.max_concurrency
    assert workers._active_children == 0
    assert not workers._active_limits and not workers._child_waiters
    assert codemode._active_child_sandboxes == 0


def test_concurrency_and_iteration_schema_bounds():
    config = ReadonlyWorkerConfig()
    assert (config.max_workers, config.max_concurrency, config.global_max_concurrency) == (8, 4, 8)
    assert config.max_iterations == 8
    assert ReadonlyWorkerConfig(max_iterations=16).max_iterations == 16
    assert config.max_concurrency <= config.global_max_concurrency <= codemode._MAX_CHILD_SANDBOXES
    for args in ({"max_iterations": 17}, {"max_concurrency": 9},
                 {"global_max_concurrency": 9},
                 {"max_concurrency": 4, "global_max_concurrency": 3}):
        with pytest.raises(ValidationError):
            ReadonlyWorkerConfig(**args)


def test_budget_ceilings_allow_large_queued_fanout_without_widening_sandboxes():
    # A queued fan-out may exceed the simultaneous cap; it must not exceed it.
    config = ReadonlyWorkerConfig(max_workers=32, max_model_calls=128, max_tokens=1_000_000)
    assert config.max_workers == 32 and config.max_model_calls == 128
    assert config.max_concurrency <= config.global_max_concurrency <= codemode._MAX_CHILD_SANDBOXES
    for args in ({"max_workers": 33}, {"max_model_calls": 129}, {"max_tokens": 1_000_001}):
        with pytest.raises(ValidationError):
            ReadonlyWorkerConfig(**args)


async def test_parallel_workers_two_waves_and_request_local_usage(worker_context):
    run, provider, _ = worker_context
    n = run.config.max_concurrency
    entered = asyncio.Queue()
    release = asyncio.Event()

    async def handler(request, _):
        await entered.put(None)
        await release.wait()
        return LLMResponse(content=json.loads(request["messages"][1]["content"])["task"])

    provider.handler = handler
    for wave in range(2):
        release.clear()
        calls = [asyncio.create_task(run_worker(f"task {wave}/{i}")) for i in range(n)]
        for _ in range(n):
            await asyncio.wait_for(entered.get(), 5)
        assert run.active_children == workers._active_children == n
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*calls), 5)
        assert [r["content"] for r in results] == [f"task {wave}/{i}" for i in range(n)]
        assert all(r["status"] == "success" and len(r["usage"]) == 1 for r in results)
        assert_released(run)
    assert len(run.usage) == run.calls == 8


@pytest.mark.parametrize("mode", ["success", "timeout", "cancel", "exception"])
async def test_fanout_with_queued_requests_releases_all_slots(worker_context, monkeypatch, mode):
    run, provider, _ = worker_context
    entered = asyncio.Queue()
    ready = asyncio.Event()
    release = asyncio.Event()
    timeouts = []
    real_timeout = asyncio.timeout

    def controlled_timeout(delay):
        # Trigger real asyncio timeout cancellation deterministically, not by sleeping.
        scope = real_timeout(None)
        timeouts.append(scope)
        return scope

    monkeypatch.setattr(workers.asyncio, "timeout", controlled_timeout)

    async def handler(*_):
        await entered.put(None)
        await release.wait()
        if mode == "exception":
            raise RuntimeError("fake failure")
        return LLMResponse(content="answer")

    provider.handler = handler
    n = run.config.max_concurrency

    async def fanout():
        calls = [asyncio.create_task(run_worker(f"task {i}")) for i in range(n + 2)]
        ready.set()
        return await asyncio.gather(*calls)

    pending = asyncio.create_task(fanout())
    await ready.wait()
    for _ in range(n):
        await asyncio.wait_for(entered.get(), 5)
    assert run.active_children == workers._active_children == n
    assert len(run.slots._waiters) == 2
    if mode == "cancel":
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        if mode == "timeout":
            assert len(timeouts) == n + 2
            for scope in timeouts:
                scope.reschedule(asyncio.get_running_loop().time())
        else:
            release.set()
        results = await asyncio.wait_for(pending, 5)
        expected = {"success": "success", "timeout": "timeout", "exception": "partial"}[mode]
        assert all(result["status"] == expected for result in results)
    assert_released(run)


async def test_global_capacity_waits_and_cancelled_waiter_does_not_leak(worker_context):
    run, _, _ = worker_context
    # A second run shares the configurable process cap, not the first run's semaphore.
    other = workers.WorkerRun(ReadonlyWorkerConfig(enable=True, max_concurrency=1,
                                                 global_max_concurrency=1), run.snapshot_loader)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        async with child_slot(other):
            entered.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    await entered.wait()
    waiting = asyncio.Event()

    async def queued():
        waiting.set()
        return await run_ask("task", "chunk")

    pending = asyncio.create_task(queued())
    await waiting.wait()  # queued runs synchronously up to the global wait
    assert len(workers._child_waiters) == 1
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    waiting.clear()
    subsequent = asyncio.create_task(queued())
    await waiting.wait()
    assert len(workers._child_waiters) == 1
    release.set()
    await holder
    assert (await subsequent)["status"] == "success"
    assert_released(run)
    assert_released(other)
    other.artifacts.close()
    assert (await run_ask("task", "next chunk"))["status"] == "success"
    assert_released(run)


async def test_ask_one_call_no_tools_registry_or_sandbox_and_shared_usage(worker_context, monkeypatch):
    run, provider, _ = worker_context
    constructor = AsyncMock(side_effect=AssertionError("must not create child registry"))
    monkeypatch.setattr(workers, "WorkerRegistry", constructor)
    monkeypatch.setattr(codemode.CodemodeTool, "execute", AsyncMock(side_effect=AssertionError("no sandbox")))
    rows = []
    run.usage_callback = lambda *row: rows.append(row)
    text = "selected chunk " * 1000  # above the old 8000-byte inline ceiling
    result = await run_ask("find answer", text)
    assert result["status"] == "success" and result["content"] == "findings"
    assert result["evidence"] == [] and run.tool_calls == 0
    assert len(provider.requests) == run.calls == 1
    request = provider.requests[0]
    assert request["tools"] is None
    assert json.loads(request["messages"][1]["content"])["untrusted_material"] == text
    assert "PARENT_SECRET_BOOTSTRAP" not in json.dumps(request)
    assert len(result["usage"]) == len(run.usage) == len(rows) == 1
    assert rows[0][2] == result["usage"][0]["usage"]
    assert result["usage"][0]["usage"]["total_tokens"] == 10
    constructor.assert_not_called()
    assert_released(run)


@pytest.mark.parametrize("budget", ["input", "serialized", "calls", "shared_tokens", "workers"])
async def test_ask_budget_refusal_never_calls_provider(worker_context, budget):
    run, provider, _ = worker_context
    run.config.max_tokens = 4000
    text = "x" * 4001 if budget == "input" else "x" * 3500
    if budget == "calls":
        run.calls = run.config.max_workers * run.config.max_model_calls
    if budget == "shared_tokens":
        run.reserved_tokens = 3999
    if budget == "workers":
        run.workers = run.config.max_workers
    result = await run_ask("answer", text)
    assert result["status"] == "budget_exhausted" and "budget" in result["error"]
    assert provider.requests == [] and run.usage == []
    assert_released(run)


async def test_ask_and_worker_keep_separate_call_allowances_under_a_shared_total(worker_context):
    run, provider, _ = worker_context
    run.config.max_model_calls = 1
    # Each request carries its own model-call allowance; only the total is shared.
    assert (await run_ask("chunk", "text"))["status"] == "success"
    assert (await run_worker("navigate"))["status"] == "success"
    assert len(provider.requests) == 2 and len(run.usage) == 2
    run.calls = run.config.max_workers * run.config.max_model_calls
    refused = await run_ask("chunk", "text")
    assert refused["status"] == "budget_exhausted"
    assert "Run-wide model-call budget exhausted" in refused["error"]
    run.workers = run.config.max_workers
    assert (await run_ask("chunk", "text"))["status"] == "budget_exhausted"


async def test_ask_disabled_missing_route_and_recursion_fail_closed(worker_context):
    run, provider, _ = worker_context
    run.snapshot_loader = None
    assert (await run_ask("task", "text"))["status"] == "unavailable"
    run.config.enable = False
    with pytest.raises(ValueError, match="disabled"):
        await run_ask("task", "text")
    token = workers.WORKER_SCOPE.set(set())
    try:
        with pytest.raises(ValueError, match="cannot delegate"):
            await run_ask("task", "text")
    finally:
        workers.WORKER_SCOPE.reset(token)
    assert provider.requests == []
    assert_released(run)


@pytest.mark.parametrize("reason", ["error", "length", "tool_calls"])
async def test_ask_never_retries_or_executes_returned_tools(worker_context, reason):
    run, provider, _ = worker_context
    provider.handler = lambda *_: (tool_call("exec", command="forbidden") if reason == "tool_calls"
                                  else LLMResponse(content="incomplete", finish_reason=reason))
    result = await run_ask("task", "text")
    assert result["status"] != "success"
    assert len(provider.requests) == run.calls == len(run.usage) == 1
    assert run.tool_calls == 0
    assert_released(run)


@pytest.mark.usefixtures("sandbox_available")
class TestBridgeFanoutAndChunks:
    async def test_all_eight_child_sandboxes_in_use_and_subsequent_wave(self, tmp_path, monkeypatch):
        n = codemode._MAX_CHILD_SANDBOXES
        entered = asyncio.Queue()
        release = asyncio.Event()
        original = ArtifactTool.execute
        runs = []

        async def gated_read(self, *args, **kwargs):
            # Holding a bridged tool keeps each real child sandbox alive.
            await entered.put(None)
            await release.wait()
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(ArtifactTool, "execute", gated_read)

        def child_handler(request, _):
            if len(request["messages"]) == 2:
                ref = json.loads(request["messages"][1]["content"])["artifacts"][0]
                return tool_call("codemode", code=f'return await tools.artifact_read(artifact={ref!r})')
            return LLMResponse(content="answer")

        async def parent_run():
            child = FakeProvider(child_handler)
            registry = parent_registry(tmp_path, child, max_model_calls=32, max_tokens=256000)
            code = (
                'ref = (await artifact_put("selected"))["artifact"]\n'
                'for wave in range(2):\n'
                '    results = await asyncio.gather(*(worker(f"task {wave}/{i}", artifacts=[ref]) for i in range(4)))\n'
                '    assert all(r["status"] == "success" and len(r["usage"]) == 2 for r in results), results\n'
                'return "two waves complete"'
            )
            parent = FakeProvider(lambda request, i: tool_call("codemode", code=code) if i == 1 else LLMResponse(content="done"))
            result = await AgentRunner(parent).run(spec(registry))
            assert "two waves complete" in parent.requests[1]["messages"][-1]["content"]
            assert result.stop_reason == "completed" and len(result.worker_usage) == 16

        # Capture the run objects to assert both per-run semaphores after cleanup.
        original_prepare = WorkerRegistry.prepare_call

        def prepare(self, *args):
            if all(self.run is not run for run in runs):
                runs.append(self.run)
            return original_prepare(self, *args)

        monkeypatch.setattr(WorkerRegistry, "prepare_call", prepare)
        pending = asyncio.gather(parent_run(), parent_run())
        try:
            for _ in range(n):
                await asyncio.wait_for(entered.get(), 10)
            assert codemode._active_workers == 2
            assert workers._active_children == codemode._active_child_sandboxes == n
            assert len(runs) == 2 and all(run.active_children == 4 for run in runs)
            release.set()
            await asyncio.wait_for(pending, 15)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        for run in runs:
            assert_released(run)

    @pytest.mark.parametrize("stage", ["provider", "child_sandbox"])
    async def test_parent_cancel_mid_fanout_releases_all_counters(self, tmp_path, monkeypatch, stage):
        entered = asyncio.Queue()
        cancelled = []
        runs = []

        async def handler(*_, **kwargs):
            runs.append(WORKER_RUN.get())
            await entered.put(None)
            try:
                await asyncio.Future()
            finally:
                cancelled.append(True)

        if stage == "child_sandbox":
            monkeypatch.setattr(ArtifactTool, "execute", handler)

            def provider_handler(request, _):
                ref = json.loads(request["messages"][1]["content"])["artifacts"][0]
                return tool_call("codemode", code=f'return await tools.artifact_read(artifact={ref!r})')
        else:
            provider_handler = handler
        registry = parent_registry(tmp_path, FakeProvider(provider_handler))
        # Four active and four queued at the parent bridge/per-run limit.
        code = ('ref = (await artifact_put("selected"))["artifact"]\n'
                'return await asyncio.gather(*(worker(f"task {i}", artifacts=[ref]) for i in range(8)))')
        parent = FakeProvider(lambda *_: tool_call("codemode", code=code))
        pending = asyncio.create_task(AgentRunner(parent).run(spec(registry)))
        try:
            for _ in range(4):
                await asyncio.wait_for(entered.get(), 5)
            assert codemode._active_child_sandboxes == (4 if stage == "child_sandbox" else 0)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 5)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        assert len(cancelled) == 4 and codemode._active_workers == 0
        assert_released(runs[0])

    async def test_ask_bridge_accounting_and_no_child_sandbox(self, tmp_path):
        def handler(*_):
            assert codemode._active_child_sandboxes == 0
            return LLMResponse(content="42")

        child = FakeProvider(handler)
        registry = parent_registry(tmp_path, child)
        code = 'r = await ask("find planted code", "padding\\ncode=42")\nassert r["content"] == "42"\nreturn r'
        parent = FakeProvider(lambda request, i: tool_call("codemode", code=code) if i == 1 else LLMResponse(content="done"))
        rows = []
        result = await AgentRunner(parent).run(spec(registry, worker_usage_callback=lambda *row: rows.append(row)))
        assert result.stop_reason == "completed"
        assert len(child.requests) == len(result.worker_usage) == len(rows) == 1
        assert child.requests[0]["tools"] is None
        assert any(event["name"] == "ask" and event["status"] == "success" for event in result.tool_events)

    @pytest.mark.parametrize("failure", ["error", "budget", "validation", "timeout"])
    @pytest.mark.parametrize("fail_on_tool_error", [False, True])
    async def test_discarded_ask_failure_is_a_failed_parent_event(self, tmp_path, failure, fail_on_tool_error):
        async def handler(*_):
            if failure == "timeout":
                await asyncio.Future()
            return LLMResponse(content="fake failure", finish_reason="error")

        child = FakeProvider(handler)
        registry = parent_registry(tmp_path, child, timeout_s=1)
        text = "x" * 65000 if failure == "budget" else "chunk"
        task = "" if failure == "validation" else "analyze"
        code = f'try: await ask({task!r}, {text!r})\nexcept Exception: pass\nreturn "success"'
        parent = FakeProvider(lambda request, i: tool_call("codemode", code=code) if i == 1 else LLMResponse(content="success"))
        result = await AgentRunner(parent).run(spec(registry, fail_on_tool_error=fail_on_tool_error))
        event = next(event for event in result.tool_events if event["name"] == "ask")
        assert event["status"] != "success" and event["evidence"][0]["artifact"]
        assert result.stop_reason == ("tool_error" if fail_on_tool_error else "partial_completion")
        assert len(child.requests) == (0 if failure in {"budget", "validation"} else 1)
        assert len(result.worker_usage) == (1 if failure == "error" else 0)
