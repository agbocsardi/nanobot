"""Tests for the Nanobot programmatic facade."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.nanobot import Nanobot, RunResult


def _write_config(tmp_path: Path, overrides: dict | None = None) -> Path:
    data = {
        "providers": {"openrouter": {"apiKey": "sk-test-key"}},
        "agents": {"defaults": {"model": "openai/gpt-4.1"}},
    }
    if overrides:
        data.update(overrides)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(data))
    return config_path


def test_from_config_missing_file():
    with pytest.raises(FileNotFoundError):
        Nanobot.from_config("/nonexistent/config.json")


def test_from_config_creates_instance(tmp_path):
    config_path = _write_config(tmp_path)
    bot = Nanobot.from_config(config_path, workspace=tmp_path)
    assert bot._loop is not None
    assert bot._loop.workspace == tmp_path


def test_from_config_default_path():
    from nanobot.config.schema import Config

    with patch("nanobot.config.loader.load_config") as mock_load, \
         patch("nanobot.providers.factory.make_provider") as mock_prov:
        mock_load.return_value = Config()
        mock_prov.return_value = MagicMock()
        mock_prov.return_value.get_default_model.return_value = "test"
        mock_prov.return_value.generation.max_tokens = 4096
        Nanobot.from_config()
        mock_load.assert_called_once_with(None)


@pytest.mark.asyncio
async def test_run_returns_result(tmp_path):
    config_path = _write_config(tmp_path)
    bot = Nanobot.from_config(config_path, workspace=tmp_path)

    from nanobot.bus.events import OutboundMessage

    mock_response = OutboundMessage(
        channel="cli", chat_id="direct", content="Hello back!"
    )
    bot._loop.process_direct = AsyncMock(return_value=mock_response)

    result = await bot.run("hi")

    assert isinstance(result, RunResult)
    assert result.content == "Hello back!"
    bot._loop.process_direct.assert_awaited_once_with("hi", session_key="sdk:default")


@pytest.mark.asyncio
async def test_run_with_hooks(tmp_path):
    from nanobot.agent.hook import AgentHook, AgentHookContext
    from nanobot.agent.hook import SDKCaptureHook
    config_path = _write_config(tmp_path)
    bot = Nanobot.from_config(config_path, workspace=tmp_path)

    class TestHook(AgentHook):
        async def before_iteration(self, context: AgentHookContext) -> None:
            pass

    hooks_before = list(bot._loop._extra_hooks)
    mock_response = OutboundMessage(
        channel="cli", chat_id="direct", content="done"
    )

    result = await bot.run("hi", hooks=[TestHook()])

    assert result.content == "done"
    # Run-level hooks are scoped to the call; loop-level extras are untouched.
    assert bot._loop._extra_hooks == hooks_before
    assert all(not isinstance(h, SDKCaptureHook) for h in bot._loop._extra_hooks)


@pytest.mark.asyncio
async def test_run_hooks_restored_on_error(tmp_path):
    config_path = _write_config(tmp_path)
    bot = Nanobot.from_config(config_path, workspace=tmp_path)

    from nanobot.agent.hook import AgentHook

    bot._loop.process_direct = AsyncMock(side_effect=RuntimeError("boom"))
    original_hooks = bot._loop._extra_hooks

    with pytest.raises(RuntimeError):
        await bot.run("hi", hooks=[AgentHook()])

    assert bot._loop._extra_hooks is original_hooks


@pytest.mark.asyncio
async def test_run_none_response(tmp_path):
    config_path = _write_config(tmp_path)
    bot = Nanobot.from_config(config_path, workspace=tmp_path)
    bot._loop.process_direct = AsyncMock(return_value=None)

    result = await bot.run("hi")
    assert result.content == ""


def test_workspace_override(tmp_path):
    config_path = _write_config(tmp_path)
    custom_ws = tmp_path / "custom_workspace"
    custom_ws.mkdir()

    bot = Nanobot.from_config(config_path, workspace=custom_ws)
    assert bot._loop.workspace == custom_ws


@pytest.mark.asyncio
async def test_run_custom_session_key(tmp_path):
    from nanobot.bus.events import OutboundMessage

    config_path = _write_config(tmp_path)
    bot = Nanobot.from_config(config_path, workspace=tmp_path)

    mock_response = OutboundMessage(
        channel="cli", chat_id="direct", content="ok"
    )
    bot._loop.process_direct = AsyncMock(return_value=mock_response)

    await bot.run("hi", session_key="user-alice")
    bot._loop.process_direct.assert_awaited_once_with("hi", session_key="user-alice")


def test_import_from_top_level():
    import nanobot

    assert nanobot.Nanobot is Nanobot
    assert nanobot.RunResult is RunResult


# ---------------------------------------------------------------------------
# RunResult.tools_used / messages — populated from the agent iterations
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_populates_tools_used_across_iterations(tmp_path):
    """tools_used collects every tool name fired across all iterations, in order."""
    from nanobot.agent.hook import AgentHookContext
    from nanobot.bus.events import OutboundMessage
    from nanobot.providers.base import ToolCallRequest

    config_path = _write_config(tmp_path)
    bot = Nanobot.from_config(config_path, workspace=tmp_path)

    async def fake_process_direct(message, *, session_key):
        # Whatever hooks the SDK installed are now on the loop.
        extras = bot._loop._extra_hooks
        messages = [{"role": "user", "content": message}]
        ctx1 = AgentHookContext(iteration=0, messages=messages)
        ctx1.tool_calls = [
            ToolCallRequest(id="c1", name="read_file", arguments={}),
            ToolCallRequest(id="c2", name="grep", arguments={}),
        ]
        for h in extras:
            await h.after_iteration(ctx1)
        messages.append({"role": "assistant", "content": "ok"})
        ctx2 = AgentHookContext(iteration=1, messages=messages)
        ctx2.tool_calls = [ToolCallRequest(id="c3", name="web_fetch", arguments={})]
        for h in extras:
            await h.after_iteration(ctx2)
        return OutboundMessage(channel="cli", chat_id="direct", content="final")

    bot._loop.process_direct = fake_process_direct
    result = await bot.run("do stuff")
    assert result.content == "final"
    assert result.tools_used == ["read_file", "grep", "web_fetch"]


@pytest.mark.asyncio
async def test_run_populates_final_messages(tmp_path):
    """messages reflects the agent's message list at the last iteration."""
    from nanobot.agent.hook import AgentHookContext
    from nanobot.bus.events import OutboundMessage

    config_path = _write_config(tmp_path)
    bot = Nanobot.from_config(config_path, workspace=tmp_path)

    async def fake_process_direct(message, *, session_key):
        extras = bot._loop._extra_hooks
        messages = [
            {"role": "user", "content": message},
            {"role": "assistant", "content": "hi there"},
        ]
        ctx = AgentHookContext(iteration=0, messages=messages)
        for h in extras:
            await h.after_iteration(ctx)
        return OutboundMessage(channel="cli", chat_id="direct", content="hi there")

    bot._loop.process_direct = fake_process_direct
    result = await bot.run("hello")
    assert result.messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]


@pytest.mark.asyncio
async def test_run_no_iterations_leaves_defaults_empty(tmp_path):
    """If process_direct never triggers after_iteration, tools_used/messages stay []."""
    from nanobot.bus.events import OutboundMessage

    config_path = _write_config(tmp_path)
    bot = Nanobot.from_config(config_path, workspace=tmp_path)
    bot._loop.process_direct = AsyncMock(
        return_value=OutboundMessage(channel="cli", chat_id="direct", content="noop"),
    )
    result = await bot.run("hi")
    assert result.tools_used == []
    assert result.messages == []


@pytest.mark.asyncio
async def test_run_user_hooks_still_fire_alongside_capture(tmp_path):
    """Capture hook must not displace user-provided hooks."""
    from nanobot.agent.hook import AgentHook, AgentHookContext
    from nanobot.bus.events import OutboundMessage

    config_path = _write_config(tmp_path)
    bot = Nanobot.from_config(config_path, workspace=tmp_path)

    seen_iterations: list[int] = []

    class UserHook(AgentHook):
        async def after_iteration(self, context: AgentHookContext) -> None:
            seen_iterations.append(context.iteration)

    async def fake_process_direct(message, *, session_key):
        # The loop composes its extras; the fanout routes to this run's hooks.
        extras = bot._loop._extra_hooks
        ctx = AgentHookContext(iteration=7, messages=[])
        for h in extras:
            await h.after_iteration(ctx)
        return OutboundMessage(channel="cli", chat_id="direct", content="ok")

    bot._loop.process_direct = fake_process_direct
    await bot.run("x", hooks=[UserHook()])
    assert seen_iterations == [7]


@pytest.mark.asyncio
async def test_run_restores_extra_hooks_even_on_populated_iterations(tmp_path):
    """Previously-installed _extra_hooks must be restored regardless of capture state."""
    from nanobot.agent.hook import AgentHook, AgentHookContext
    from nanobot.bus.events import OutboundMessage

    config_path = _write_config(tmp_path)
    bot = Nanobot.from_config(config_path, workspace=tmp_path)

    sentinel_hook = AgentHook()
    bot._loop._extra_hooks = [sentinel_hook]

    async def fake_process_direct(message, *, session_key):
        ctx = AgentHookContext(iteration=0, messages=[])
        for h in bot._loop._extra_hooks:
            await h.after_iteration(ctx)
        return OutboundMessage(channel="cli", chat_id="direct", content="done")

    bot._loop.process_direct = fake_process_direct
    await bot.run("hello")
    assert bot._loop._extra_hooks == [sentinel_hook]


@pytest.mark.asyncio
async def test_sdk_capture_prefers_run_level_snapshot():
    from nanobot.agent.hook import AgentHookContext, AgentRunHookContext, SDKCaptureHook
    from nanobot.providers.base import ToolCallRequest

    hook = SDKCaptureHook()
    iter_messages = [{"role": "user", "content": "work"}]
    iter_context = AgentHookContext(iteration=0, messages=iter_messages)
    iter_context.tool_calls = [
        ToolCallRequest(id="call_1", name="read_file", arguments={}),
        ToolCallRequest(id="call_2", name="grep", arguments={}),
    ]
    await hook.after_iteration(iter_context)

    final_messages = [
        {"role": "user", "content": "work"},
        {"role": "assistant", "content": "done"},
    ]
    await hook.after_run(AgentRunHookContext(
        messages=final_messages,
        tools_used=["read_file"],
    ))

    assert hook.tools_used == ["read_file"]
    assert hook.messages == final_messages


@pytest.mark.asyncio
async def test_aclose_delegates_to_loop_close_mcp(tmp_path):
    config_path = _write_config(tmp_path)
    bot = Nanobot.from_config(config_path, workspace=tmp_path)
    bot._loop.close_mcp = AsyncMock()

    await bot.aclose()

    bot._loop.close_mcp.assert_awaited_once()


@pytest.mark.asyncio
async def test_context_manager_calls_aclose_on_exit(tmp_path):
    config_path = _write_config(tmp_path)
    bot = Nanobot.from_config(config_path, workspace=tmp_path)
    bot._loop.close_mcp = AsyncMock()

    async with bot as b:
        assert b is bot

    bot._loop.close_mcp.assert_awaited_once()


@pytest.mark.asyncio
async def test_context_manager_does_not_swallow_exceptions(tmp_path):
    config_path = _write_config(tmp_path)
    bot = Nanobot.from_config(config_path, workspace=tmp_path)
    bot._loop.close_mcp = AsyncMock()

    with pytest.raises(ValueError):
        async with bot as b:
            assert b is bot
            raise ValueError("boom")

    bot._loop.close_mcp.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_runs_capture_only_their_own_results(tmp_path):
    """Two overlapping SDK runs must each observe only their own hook events."""
    import asyncio

    from nanobot.agent.hook import AgentHookContext
    from nanobot.bus.events import OutboundMessage
    from nanobot.providers.base import ToolCallRequest

    config_path = _write_config(tmp_path)
    bot = Nanobot.from_config(config_path, workspace=tmp_path)

    entered_a, entered_b = asyncio.Event(), asyncio.Event()

    async def fake_process_direct(message, *, session_key):
        (entered_a if session_key == "a" else entered_b).set()
        # Hold both runs open at once so their hook contexts coexist.
        await (entered_b.wait() if session_key == "a" else entered_a.wait())
        for h in bot._loop._extra_hooks:
            await h.after_iteration(AgentHookContext(
                iteration=0,
                messages=[],
                tool_calls=[ToolCallRequest(id="c1", name=f"{session_key}_tool", arguments={})],
            ))
        return OutboundMessage(channel="cli", chat_id="direct", content=session_key)

    bot._loop.process_direct = fake_process_direct
    result_a, result_b = await asyncio.gather(
        bot.run("hi", session_key="a"),
        bot.run("hi", session_key="b"),
    )

    assert result_a.content == "a"
    assert result_a.tools_used == ["a_tool"]
    assert result_b.content == "b"
    assert result_b.tools_used == ["b_tool"]


@pytest.mark.asyncio
async def test_concurrent_instances_keep_own_config_path_and_whitelist(tmp_path):
    """Instances must not adopt each other's config path or SSRF policy."""
    import asyncio

    from nanobot.bus.events import OutboundMessage
    from nanobot.config.loader import get_config_path
    from nanobot.security.network import configure_ssrf_whitelist, validate_url_target

    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    path_a = _write_config(dir_a, {"tools": {"ssrf_whitelist": ["100.64.0.0/10"]}})
    path_b = _write_config(dir_b)

    bot_a = Nanobot.from_config(path_a, workspace=dir_a)
    bot_b = Nanobot.from_config(path_b, workspace=dir_b)
    assert bot_a._config_path == path_a
    assert bot_b._config_path == path_b

    seen: dict[tuple[str, str], object] = {}

    def make_fake(label: str) -> object:
        async def fake_process_direct(message, *, session_key):
            seen[("path", label)] = get_config_path()
            seen[("cgnat_allowed", label)] = validate_url_target("http://100.64.0.1/img")[0]
            async def probe() -> None:
                seen[("task_path", label)] = get_config_path()
            await asyncio.create_task(probe())
            return OutboundMessage(channel="cli", chat_id="direct", content=label)
        return fake_process_direct

    bot_a._loop.process_direct = make_fake("a")
    bot_b._loop.process_direct = make_fake("b")
    try:
        result_a, result_b = await asyncio.gather(bot_a.run("hi"), bot_b.run("hi"))
    finally:
        configure_ssrf_whitelist([])

    assert result_a.content == "a"
    assert result_b.content == "b"
    assert seen[("path", "a")] == path_a
    assert seen[("path", "b")] == path_b
    # Tasks spawned inside a run inherit the instance's scoped policy.
    assert seen[("task_path", "a")] == path_a
    assert seen[("task_path", "b")] == path_b
    # A's whitelist admits CGNAT addresses; B's default policy blocks them.
    assert seen[("cgnat_allowed", "a")] is True
    assert seen[("cgnat_allowed", "b")] is False
