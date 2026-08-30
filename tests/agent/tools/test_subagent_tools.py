"""Tests for subagent tool registration and wiring."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.runner import AgentRunResult
from nanobot.config.schema import AgentDefaults

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


@pytest.mark.asyncio
async def test_subagent_exec_tool_receives_allowed_env_keys(tmp_path):
    """allowed_env_keys from ExecToolConfig must be forwarded to the subagent's ExecTool."""
    from nanobot.agent.subagent import SubagentManager, SubagentStatus
    from nanobot.agent.tools.shell import ExecToolConfig
    from nanobot.bus.queue import MessageBus
    from nanobot.config.schema import ToolsConfig

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        tools_config=ToolsConfig(exec=ExecToolConfig(allowed_env_keys=["GOPATH", "JAVA_HOME"])),
    )
    mgr._announce_result = AsyncMock()

    async def fake_run(spec):
        exec_tool = spec.tools.get("exec")
        assert exec_tool is not None
        assert exec_tool.allowed_env_keys == ["GOPATH", "JAVA_HOME"]
        return SimpleNamespace(
            stop_reason="done",
            final_content="done",
            error=None,
            tool_events=[],
        )

    mgr.runner.run = AsyncMock(side_effect=fake_run)

    status = SubagentStatus(
        task_id="sub-1", label="label", task_description="do task", started_at=time.monotonic()
    )
    await mgr._run_subagent(
        "sub-1", "do task", "label", {"channel": "test", "chat_id": "c1"}, status
    )

    mgr.runner.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_subagent_uses_configured_max_iterations(tmp_path):
    """Subagents should honor the configured tool-iteration limit."""
    from nanobot.agent.subagent import SubagentManager, SubagentStatus
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        max_iterations=37,
    )
    mgr._announce_result = AsyncMock()

    async def fake_run(spec):
        assert spec.max_iterations == 37
        return SimpleNamespace(
            stop_reason="done",
            final_content="done",
            error=None,
            tool_events=[],
        )

    mgr.runner.run = AsyncMock(side_effect=fake_run)

    status = SubagentStatus(
        task_id="sub-1", label="label", task_description="do task", started_at=time.monotonic()
    )
    await mgr._run_subagent(
        "sub-1", "do task", "label", {"channel": "test", "chat_id": "c1"}, status
    )

    mgr.runner.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_spawn_forwards_temperature_to_run_spec(tmp_path):
    """A temperature passed to spawn() should reach the AgentRunSpec."""
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )
    mgr._announce_result = AsyncMock()

    seen = {}

    async def fake_run(spec):
        seen["temperature"] = spec.temperature
        return SimpleNamespace(
            stop_reason="done", final_content="done", error=None, tool_events=[],
        )

    mgr.runner.run = AsyncMock(side_effect=fake_run)

    await mgr.spawn(task="do task", temperature=0.9)
    await asyncio.gather(*mgr._running_tasks.values(), return_exceptions=True)

    assert seen["temperature"] == 0.9


@pytest.mark.asyncio
async def test_spawn_model_preset_overrides_provider_and_model(tmp_path):
    """model_preset should resolve to a (provider, model) snapshot used by the run."""
    from nanobot.agent import subagent as subagent_mod
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.context import RequestContext
    from nanobot.agent.tools.spawn import SpawnTool
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "default-model"
    preset_provider = MagicMock()
    preset_provider.get_default_model.return_value = "deepseek-chat"
    snapshot = SimpleNamespace(
        provider=preset_provider, model="deepseek-chat", context_window_tokens=64000
    )
    presets = {"default": object(), "deepseek": object()}
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        preset_snapshot_loader=lambda name: snapshot,
        presets=presets,
    )
    mgr._announce_result = AsyncMock()

    captured = {}

    class FakeRunner:
        def __init__(self, prov):
            captured["provider"] = prov

        async def run(self, spec):
            captured["model"] = spec.model
            captured["ctx"] = spec.context_window_tokens
            return SimpleNamespace(
                stop_reason="done", final_content="done", error=None, tool_events=[],
            )

    tool = SpawnTool(mgr)
    tool.set_context(RequestContext(channel="test", chat_id="c1", session_key="test:c1"))
    with patch.object(subagent_mod, "AgentRunner", FakeRunner):
        result = await tool.execute(task="do task", model_preset="deepseek")
        assert "started" in result
        await asyncio.gather(*mgr._running_tasks.values(), return_exceptions=True)

    assert captured["provider"] is preset_provider
    assert captured["model"] == "deepseek-chat"
    assert captured["ctx"] == 64000


@pytest.mark.asyncio
async def test_spawn_invalid_model_preset_returns_clear_error(tmp_path):
    """An unknown model_preset must fail clearly at call time, not crash the turn."""
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.context import RequestContext
    from nanobot.agent.tools.spawn import SpawnTool
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "default-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        preset_snapshot_loader=lambda name: SimpleNamespace(
            provider=provider, model="x", context_window_tokens=1
        ),
        presets={"default": object()},
    )
    mgr._announce_result = AsyncMock()

    tool = SpawnTool(mgr)
    tool.set_context(RequestContext(channel="test", chat_id="c1", session_key="test:c1"))
    result = await tool.execute(task="do task", model_preset="nope")

    assert "Cannot spawn subagent" in result
    assert "nope" in result
    # No task scheduled: validation raised before create_task.
    assert mgr.get_running_count() == 0


@pytest.mark.asyncio
async def test_spawn_max_iterations_override_threads_to_spec(tmp_path):
    """max_iterations passed to spawn should reach the AgentRunSpec."""
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "default-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        max_iterations=10,
    )
    mgr._announce_result = AsyncMock()

    seen = {}

    async def fake_run(spec):
        seen["max_iterations"] = spec.max_iterations
        return SimpleNamespace(
            stop_reason="done", final_content="done", error=None, tool_events=[],
        )

    mgr.runner.run = AsyncMock(side_effect=fake_run)

    await mgr.spawn(task="do task", max_iterations=4)
    await asyncio.gather(*mgr._running_tasks.values(), return_exceptions=True)

    assert seen["max_iterations"] == 4


@pytest.mark.asyncio
async def test_spawn_tool_queues_when_at_concurrency_limit(tmp_path):
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.spawn import SpawnTool
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )
    mgr._announce_result = AsyncMock()

    # Block the first subagent so it stays "running"
    release = asyncio.Event()

    async def fake_run(spec):
        await release.wait()
        return SimpleNamespace(
            stop_reason="done",
            final_content="done",
            error=None,
            tool_events=[],
        )

    mgr.runner.run = AsyncMock(side_effect=fake_run)

    from nanobot.agent.tools.context import RequestContext

    tool = SpawnTool(mgr)
    tool.set_context(RequestContext(channel="test", chat_id="c1", session_key="test:c1"))

    # First spawn succeeds
    result = await tool.execute(task="first task")
    assert "started" in result

    # Second spawn waits behind the first (default execution limit is 1).
    result = await tool.execute(task="second task")
    assert "queued" in result
    assert mgr.get_queued_count() == 1

    # Release the first subagent
    release.set()
    # Allow cleanup
    await asyncio.gather(*mgr._running_tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_spawn_max_tokens_override_threads_to_spec(tmp_path):
    """max_tokens passed to spawn should reach the AgentRunSpec."""
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "default-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )
    mgr._announce_result = AsyncMock()

    seen = {}

    async def fake_run(spec):
        seen["max_tokens"] = spec.max_tokens
        return SimpleNamespace(
            stop_reason="done", final_content="done", error=None, tool_events=[],
        )

    mgr.runner.run = AsyncMock(side_effect=fake_run)

    await mgr.spawn(task="do task", max_tokens=3072)
    await asyncio.gather(*mgr._running_tasks.values(), return_exceptions=True)

    assert seen["max_tokens"] == 3072


@pytest.mark.asyncio
async def test_spawn_tool_max_tokens_reaches_manager_and_spec(tmp_path):
    """The spawn tool schema must accept max_tokens and forward it end-to-end."""
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.context import RequestContext
    from nanobot.agent.tools.spawn import SpawnTool
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )
    mgr._announce_result = AsyncMock()

    seen = {}

    async def fake_run(spec):
        seen["max_tokens"] = spec.max_tokens
        return SimpleNamespace(
            stop_reason="done", final_content="done", error=None, tool_events=[],
        )

    mgr.runner.run = AsyncMock(side_effect=fake_run)
    tool = SpawnTool(mgr)
    tool.set_context(RequestContext(channel="test", chat_id="c1", session_key="test:c1"))

    result = await tool.execute(task="do task", max_tokens=5120)
    assert "started" in result
    await asyncio.gather(*mgr._running_tasks.values(), return_exceptions=True)
    assert seen["max_tokens"] == 5120


@pytest.mark.asyncio
async def test_spawn_tool_queue_full_rejection_is_structured(tmp_path):
    """Queue-full rejection message must carry length, position, and capacity."""
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.context import RequestContext
    from nanobot.agent.tools.spawn import SpawnTool
    from nanobot.bus.queue import MessageBus

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        max_concurrent_subagents=1,
        max_queued_subagents=1,
    )
    mgr._announce_result = AsyncMock()
    release = asyncio.Event()

    async def fake_run(spec):
        await release.wait()
        return AgentRunResult(final_content="done", messages=[], stop_reason="completed")

    mgr.runner.run = AsyncMock(side_effect=fake_run)
    tool = SpawnTool(mgr)
    tool.set_context(RequestContext(channel="test", chat_id="c1", session_key="test:c1"))

    assert "started" in await tool.execute(task="first")
    assert "queued" in await tool.execute(task="second")
    rejected = await tool.execute(task="third")

    assert "Cannot spawn subagent" in rejected
    assert "queue is full" in rejected
    assert "1/1" in rejected          # queue length / capacity
    assert "capacity 1" in rejected
    assert "position 2" in rejected   # would occupy the next (full) slot
    release.set()
    await asyncio.gather(*mgr._running_tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_spawn_tool_rejects_only_when_bounded_queue_is_full(tmp_path):
    from nanobot.agent.subagent import SubagentManager
    from nanobot.agent.tools.context import RequestContext
    from nanobot.agent.tools.spawn import SpawnTool
    from nanobot.bus.queue import MessageBus

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        max_concurrent_subagents=1,
        max_queued_subagents=1,
    )
    mgr._announce_result = AsyncMock()
    release = asyncio.Event()

    async def fake_run(spec):
        await release.wait()
        return AgentRunResult(final_content="done", messages=[], stop_reason="completed")

    mgr.runner.run = AsyncMock(side_effect=fake_run)
    tool = SpawnTool(mgr)
    tool.set_context(RequestContext(channel="test", chat_id="c1", session_key="test:c1"))

    assert "started" in await tool.execute(task="first")
    assert "queued" in await tool.execute(task="second")
    rejected = await tool.execute(task="third")

    assert "Cannot spawn subagent" in rejected
    assert "queue is full" in rejected
    release.set()
    await asyncio.gather(*mgr._running_tasks.values(), return_exceptions=True)


def test_subagent_default_max_concurrent_matches_agent_defaults(tmp_path):
    """Direct SubagentManager construction should use the agent default concurrency limit."""
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )

    assert mgr.max_concurrent_subagents == AgentDefaults().max_concurrent_subagents


def test_subagent_default_max_iterations_matches_agent_defaults(tmp_path):
    """Direct SubagentManager construction should use the agent default limit."""
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )

    assert mgr.max_iterations == AgentDefaults().max_tool_iterations


def test_agent_loop_passes_max_iterations_to_subagents(tmp_path):
    """AgentLoop's configured limit should be shared with spawned subagents."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        max_iterations=42,
    )

    assert loop.subagents.max_iterations == 42


@pytest.mark.asyncio
async def test_agent_loop_syncs_updated_max_iterations_before_run(tmp_path):
    """Runtime max_iterations changes should be reflected before tool execution."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        max_iterations=42,
    )
    loop.tools.get_definitions = MagicMock(return_value=[])

    async def fake_run(spec):
        assert spec.max_iterations == 55
        assert loop.subagents.max_iterations == 55
        return SimpleNamespace(
            stop_reason="done",
            final_content="done",
            error=None,
            tool_events=[],
            messages=[],
            usage={},
            had_injections=False,
            tools_used=[],
        )

    loop.runner.run = AsyncMock(side_effect=fake_run)
    loop.max_iterations = 55

    await loop._run_agent_loop([])

    loop.runner.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_drain_pending_blocks_while_subagents_running(tmp_path):
    """_drain_pending should block when no messages are available but sub-agents are still running."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus
    from nanobot.session.manager import Session

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")

    pending_queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
    session = Session(key="test:drain-block")
    injection_callback = None

    # Capture the injection_callback that _run_agent_loop creates
    async def fake_runner_run(spec):
        nonlocal injection_callback
        injection_callback = spec.injection_callback

        # Simulate: first call to injection_callback should block because
        # sub-agents are running and no messages are in the queue yet.
        # We'll resolve this from a concurrent task.
        return SimpleNamespace(
            stop_reason="done",
            final_content="done",
            error=None,
            tool_events=[],
            messages=[],
            usage={},
            had_injections=False,
            tools_used=[],
        )

    loop.runner.run = AsyncMock(side_effect=fake_runner_run)

    # Register a running sub-agent in the SubagentManager for this session
    async def _hang_forever():
        await asyncio.Event().wait()

    hang_task = asyncio.create_task(_hang_forever())
    loop.subagents._session_tasks.setdefault(session.key, set()).add("sub-drain-1")
    loop.subagents._running_tasks["sub-drain-1"] = hang_task

    # Run _run_agent_loop — this defines the _drain_pending closure
    await loop._run_agent_loop(
        [{"role": "user", "content": "test"}],
        session=session,
        channel="test",
        chat_id="c1",
        pending_queue=pending_queue,
    )

    assert injection_callback is not None

    # Now test the callback directly
    # With sub-agents running and an empty queue, it should block
    drain_task = asyncio.create_task(injection_callback())

    # Let the task enter the blocking queue wait.
    await asyncio.sleep(0)

    # Should still be running (blocked on pending_queue.get())
    assert not drain_task.done(), "drain should block while sub-agents are running"

    # Now put a message in the queue (simulating sub-agent completion)
    await pending_queue.put(InboundMessage(
        sender_id="subagent",
        channel="test",
        chat_id="c1",
        content="Sub-agent result",
        media=None,
        metadata={},
    ))

    # Should unblock and return results
    results = await asyncio.wait_for(drain_task, timeout=2.0)
    assert len(results) >= 1
    assert results[0]["role"] == "user"
    assert "Sub-agent result" in str(results[0]["content"])

    # Cleanup
    hang_task.cancel()
    try:
        await hang_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_drain_pending_no_block_when_no_subagents(tmp_path):
    """_drain_pending should not block when no sub-agents are running."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")

    pending_queue: asyncio.Queue = asyncio.Queue()
    injection_callback = None

    async def fake_runner_run(spec):
        nonlocal injection_callback
        injection_callback = spec.injection_callback
        return SimpleNamespace(
            stop_reason="done",
            final_content="done",
            error=None,
            tool_events=[],
            messages=[],
            usage={},
            had_injections=False,
            tools_used=[],
        )

    loop.runner.run = AsyncMock(side_effect=fake_runner_run)

    await loop._run_agent_loop(
        [{"role": "user", "content": "test"}],
        session=None,
        channel="test",
        chat_id="c1",
        pending_queue=pending_queue,
    )

    assert injection_callback is not None

    # With no sub-agents and empty queue, should return immediately
    results = await asyncio.wait_for(injection_callback(), timeout=1.0)
    assert results == []


@pytest.mark.asyncio
async def test_drain_pending_timeout(tmp_path):
    """_drain_pending should return empty after timeout when sub-agents hang."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.session.manager import Session

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")

    pending_queue: asyncio.Queue = asyncio.Queue()
    session = Session(key="test:drain-timeout")
    injection_callback = None

    async def fake_runner_run(spec):
        nonlocal injection_callback
        injection_callback = spec.injection_callback
        return SimpleNamespace(
            stop_reason="done",
            final_content="done",
            error=None,
            tool_events=[],
            messages=[],
            usage={},
            had_injections=False,
            tools_used=[],
        )

    loop.runner.run = AsyncMock(side_effect=fake_runner_run)

    # Register a "running" sub-agent that will never complete
    async def _hang_forever():
        await asyncio.Event().wait()

    hang_task = asyncio.create_task(_hang_forever())
    loop.subagents._session_tasks.setdefault(session.key, set()).add("sub-timeout-1")
    loop.subagents._running_tasks["sub-timeout-1"] = hang_task

    await loop._run_agent_loop(
        [{"role": "user", "content": "test"}],
        session=session,
        channel="test",
        chat_id="c1",
        pending_queue=pending_queue,
    )

    assert injection_callback is not None

    # Patch the timeout path without leaking the queue.get() coroutine.
    async def _timeout(awaitable, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    with patch("nanobot.agent.loop.asyncio.wait_for", side_effect=_timeout):
        results = await injection_callback()
        assert results == []

    # Cleanup
    hang_task.cancel()
    try:
        await hang_task
    except asyncio.CancelledError:
        pass
