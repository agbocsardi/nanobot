"""Tests for AgentLoop integration with AgentRunner: streaming, think-filter, error handling, subagent."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.config.schema import AgentDefaults
from nanobot.providers.base import GenerationSettings, LLMResponse, ToolCallRequest
from nanobot.session.manager import Session

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


def _make_loop(tmp_path):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=4096)

    with patch("nanobot.agent.loop.ContextBuilder"), \
         patch("nanobot.agent.loop.SessionManager"), \
         patch("nanobot.agent.loop.SubagentManager") as mock_sub_mgr:
        mock_sub_mgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)
    return loop

@pytest.mark.asyncio
async def test_loop_max_iterations_message_stays_stable(tmp_path):
    loop = _make_loop(tmp_path)
    loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(
        content="working",
        tool_calls=[ToolCallRequest(id="call_1", name="list_dir", arguments={})],
    ))
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.tools.execute = AsyncMock(return_value="ok")
    loop.max_iterations = 2

    final_content, _, _, _, _ = await loop._run_agent_loop([])

    assert final_content == (
        "I reached the maximum number of tool call iterations (2) "
        "without completing the task. You can try breaking the task into smaller steps."
    )


@pytest.mark.asyncio
async def test_loop_records_session_usage_by_model(tmp_path):
    loop = _make_loop(tmp_path)
    loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(
        content="done",
        tool_calls=[],
        usage={"prompt_tokens": 10, "completion_tokens": 3},
    ))
    loop.tools.get_definitions = MagicMock(return_value=[])
    session = Session(key="cron:test")

    await loop._run_agent_loop([], session=session)

    usage = session.metadata["usage"]
    assert usage["prompt_tokens"] == 10
    assert usage["completion_tokens"] == 3
    assert usage["total_tokens"] == 13
    assert usage["requests"] == 1
    row = next(iter(usage["by_model"].values()))
    assert row["model"] == "test-model"
    assert row["total_tokens"] == 13


@pytest.mark.asyncio
async def test_cron_usage_sink_records_run_override(tmp_path):
    loop = _make_loop(tmp_path)
    background = MagicMock()
    background.generation = GenerationSettings(max_tokens=4096)
    background._spec.name = "background-provider"
    background.chat_with_retry = AsyncMock(return_value=LLMResponse(
        content="done", tool_calls=[],
        usage={"prompt_tokens": 10, "completion_tokens": 3},
    ))
    loop.tools.get_definitions = MagicMock(return_value=[])
    sink = {}

    await loop._run_agent_loop(
        [], metadata={"_cron_run_usage": sink},
        run_provider=background, run_model="background-model",
    )

    assert sink["provider"] == "background-provider"
    assert sink["model"] == "background-model"
    assert sink["prompt_tokens"] == 10
    assert sink["completion_tokens"] == 3
    assert loop.model == "test-model"


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_loop_keeps_tool_diagnostics_internal_for_chat(tmp_path, background):
    from nanobot.agent.tools.base import ToolResult

    loop = _make_loop(tmp_path)
    loop.max_iterations = 6
    answer = "The save failed; here is the explanation you asked for."
    loop.provider.chat_with_retry = AsyncMock(side_effect=[
        LLMResponse(content="saving", tool_calls=[
            ToolCallRequest(id="call", name="list_dir", arguments={}),
        ]),
        LLMResponse(content=answer),
        LLMResponse(content=answer),
    ])
    from nanobot.agent.tools.registry import ToolRegistry
    from tests.agent.tools.test_codemode import _Tool

    tools = ToolRegistry()
    tools.register(_Tool("list_dir", handler=AsyncMock(
        return_value=ToolResult.retryable_error("lookup failed"),
    )))
    content, _, messages, reason, _ = await loop._run_agent_loop(
        [], tools=tools, metadata={"_cron_run_usage": {}} if background else {},
    )
    assert reason == ("partial_completion" if background else "completed")
    assert loop.provider.chat_with_retry.await_count == (3 if background else 2)
    if background:
        assert content.startswith("Incomplete:")
    else:
        assert content == answer
        assert messages[-1]["content"] == answer


@pytest.mark.asyncio
async def test_loop_goal_turn_uses_standard_iteration_budget(tmp_path):
    loop = _make_loop(tmp_path)
    loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(
        content="working",
        tool_calls=[ToolCallRequest(id="call_1", name="list_dir", arguments={})],
    ))
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.tools.execute = AsyncMock(return_value="ok")
    loop.max_iterations = 2

    final_content, _, _, stop_reason, _ = await loop._run_agent_loop(
        [],
        metadata={"original_command": "/goal"},
    )

    assert stop_reason == "max_iterations"
    assert loop.provider.chat_with_retry.await_count == 3
    assert loop.provider.chat_with_retry.await_args_list[-1].kwargs["tools"] is None
    assert final_content == (
        "I reached the maximum number of tool call iterations (2) "
        "without completing the task. You can try breaking the task into smaller steps."
    )


@pytest.mark.asyncio
async def test_loop_stream_filter_handles_think_only_prefix_without_crashing(tmp_path):
    loop = _make_loop(tmp_path)
    deltas: list[str] = []
    endings: list[bool] = []

    async def chat_stream_with_retry(*, on_content_delta, **kwargs):
        await on_content_delta("<think>hidden")
        await on_content_delta("</think>Hello")
        return LLMResponse(content="<think>hidden</think>Hello", tool_calls=[], usage={})

    loop.provider.chat_stream_with_retry = chat_stream_with_retry

    async def on_stream(delta: str) -> None:
        deltas.append(delta)

    async def on_stream_end(*, resuming: bool = False) -> None:
        endings.append(resuming)

    final_content, _, _, _, _ = await loop._run_agent_loop(
        [],
        on_stream=on_stream,
        on_stream_end=on_stream_end,
    )

    assert final_content == "Hello"
    assert deltas == ["Hello"]
    assert endings == [False]


@pytest.mark.asyncio
async def test_loop_stream_filter_hides_partial_trailing_think_prefix(tmp_path):
    loop = _make_loop(tmp_path)
    deltas: list[str] = []

    async def chat_stream_with_retry(*, on_content_delta, **kwargs):
        await on_content_delta("Hello <thin")
        await on_content_delta("k>hidden</think>World")
        return LLMResponse(content="Hello <think>hidden</think>World", tool_calls=[], usage={})

    loop.provider.chat_stream_with_retry = chat_stream_with_retry

    async def on_stream(delta: str) -> None:
        deltas.append(delta)

    final_content, _, _, _, _ = await loop._run_agent_loop([], on_stream=on_stream)

    assert final_content == "Hello World"
    assert deltas == ["Hello", " World"]


@pytest.mark.asyncio
async def test_loop_stream_filter_hides_complete_trailing_think_tag(tmp_path):
    loop = _make_loop(tmp_path)
    deltas: list[str] = []

    async def chat_stream_with_retry(*, on_content_delta, **kwargs):
        await on_content_delta("Hello <think>")
        await on_content_delta("hidden</think>World")
        return LLMResponse(content="Hello <think>hidden</think>World", tool_calls=[], usage={})

    loop.provider.chat_stream_with_retry = chat_stream_with_retry

    async def on_stream(delta: str) -> None:
        deltas.append(delta)

    final_content, _, _, _, _ = await loop._run_agent_loop([], on_stream=on_stream)

    assert final_content == "Hello World"
    assert deltas == ["Hello", " World"]


@pytest.mark.asyncio
async def test_loop_retries_think_only_final_response(tmp_path):
    loop = _make_loop(tmp_path)
    call_count = {"n": 0}

    async def chat_with_retry(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(content="<think>hidden</think>", tool_calls=[], usage={})
        return LLMResponse(content="Recovered answer", tool_calls=[], usage={})

    loop.provider.chat_with_retry = chat_with_retry

    final_content, _, _, _, _ = await loop._run_agent_loop([])

    assert final_content == "Recovered answer"
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_streamed_flag_not_set_on_llm_error(tmp_path):
    """When LLM errors during a streaming-capable channel interaction,
    _streamed must NOT be set so ChannelManager delivers the error."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=4096)
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")
    error_resp = LLMResponse(
        content="503 service unavailable", finish_reason="error", tool_calls=[], usage={},
    )
    loop.provider.chat_with_retry = AsyncMock(return_value=error_resp)
    loop.provider.chat_stream_with_retry = AsyncMock(return_value=error_resp)
    loop.tools.get_definitions = MagicMock(return_value=[])

    msg = InboundMessage(
        channel="feishu", sender_id="u1", chat_id="c1", content="hi",
    )
    result = await loop._process_message(
        msg,
        on_stream=AsyncMock(),
        on_stream_end=AsyncMock(),
    )

    assert result is not None
    assert "503" in result.content
    assert not result.metadata.get("_streamed"), \
        "_streamed must not be set when stop_reason is error"


@pytest.mark.asyncio
async def test_ssrf_soft_block_can_finalize_after_streamed_tool_call(tmp_path):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=4096)
    tool_call_resp = LLMResponse(
        content="checking metadata",
        tool_calls=[ToolCallRequest(
            id="call_ssrf",
            name="exec",
            arguments={"command": "curl http://169.254.169.254/latest/meta-data/"},
        )],
        usage={},
    )
    provider.chat_stream_with_retry = AsyncMock(side_effect=[
        tool_call_resp,
        LLMResponse(
            content="I cannot access private URLs. Please share the local file.",
            tool_calls=[],
            usage={},
        ),
    ])

    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.tools.prepare_call = MagicMock(return_value=(None, {}, None))
    loop.tools.execute = AsyncMock(return_value=(
        "Error: Command blocked by safety guard (internal/private URL detected)"
    ))

    result = await loop._process_message(
        InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content="hi"),
        on_stream=AsyncMock(),
        on_stream_end=AsyncMock(),
    )

    assert result is not None
    assert result.content.endswith(
        "I cannot access private URLs. Please share the local file."
    )
    assert result.metadata.get("_streamed") is True


@pytest.mark.asyncio
async def test_next_turn_after_llm_error_keeps_turn_boundary(tmp_path):
    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.runner import _PERSISTED_MODEL_ERROR_PLACEHOLDER
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=4096)
    provider.chat_with_retry = AsyncMock(side_effect=[
        LLMResponse(content="429 rate limit exceeded", finish_reason="error", tool_calls=[], usage={}),
        LLMResponse(content="Recovered answer", tool_calls=[], usage={}),
    ])

    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]

    first = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="test", content="first question")
    )
    assert first is not None
    assert first.content == "429 rate limit exceeded"

    session = loop.sessions.get_or_create("cli:test")
    assert [
        {key: value for key, value in message.items() if key in {"role", "content"}}
        for message in session.messages
    ] == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": _PERSISTED_MODEL_ERROR_PLACEHOLDER},
    ]

    second = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="test", content="second question")
    )
    assert second is not None
    assert second.content == "Recovered answer"

    request_messages = provider.chat_with_retry.await_args_list[1].kwargs["messages"]
    non_system = [message for message in request_messages if message.get("role") != "system"]
    assert non_system[0]["role"] == "user"
    assert "first question" in non_system[0]["content"]
    assert non_system[1]["role"] == "assistant"
    assert _PERSISTED_MODEL_ERROR_PLACEHOLDER in non_system[1]["content"]
    assert non_system[2]["role"] == "user"
    assert "second question" in non_system[2]["content"]


@pytest.mark.asyncio
async def test_subagent_max_iterations_announces_incomplete_fallback(tmp_path, monkeypatch):
    from nanobot.agent.subagent import SubagentManager, SubagentStatus
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=4096)
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(
        content="working",
        tool_calls=[ToolCallRequest(id="call_1", name="list_dir", arguments={"path": "."})],
    ))
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )
    mgr._announce_result = AsyncMock()

    async def fake_execute(self, **kwargs):
        return "tool result"

    monkeypatch.setattr("nanobot.agent.tools.filesystem.ListDirTool.execute", fake_execute)

    status = SubagentStatus(task_id="sub-1", label="label", task_description="do task", started_at=time.monotonic())
    await mgr._run_subagent("sub-1", "do task", "label", {"channel": "test", "chat_id": "c1"}, status)

    mgr._announce_result.assert_awaited_once()
    args = mgr._announce_result.await_args.args
    assert args[3] == "Task ended without a verified final synthesis."
    assert args[5] == "error"
    assert status.phase == "incomplete"
    assert status.stop_reason == "max_iterations"


@pytest.mark.asyncio
@pytest.mark.parametrize("final_text", ["", "duplicate reply"])
async def test_message_tool_delivery_suppresses_retry_and_stream(tmp_path, final_text):
    from nanobot.agent.hook import AgentHook
    from nanobot.agent.loop import TurnContext, TurnState
    from nanobot.agent.turn_delivery import _turn_outcome
    from nanobot.bus.events import DeliveryResult, InboundMessage
    from nanobot.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

    loop = _make_loop(tmp_path)
    contexts = []
    run_contexts = []

    class CaptureHook(AgentHook):
        async def after_iteration(self, context):
            contexts.append(context)

        async def after_run(self, context):
            run_contexts.append(context)

    loop._extra_hooks = [CaptureHook()]
    message_tool = loop.tools.get("message")
    sent = AsyncMock(return_value=DeliveryResult(status="delivered"))
    message_tool.set_send_callback(sent)
    message_tool.start_turn()
    tool_response = LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id="send", name="message", arguments={"content": "reply"})],
    )
    loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content=final_text))
    stream_calls = 0

    async def chat_stream_with_retry(*, on_content_delta, **kwargs):
        nonlocal stream_calls
        stream_calls += 1
        return tool_response

    loop.provider.chat_stream_with_retry = chat_stream_with_retry
    published = AsyncMock()

    async def on_stream(delta):
        await published(delta)

    final, _, messages, reason, injected = await loop._run_agent_loop(
        [], channel="telegram", chat_id="chat", on_stream=on_stream,
    )

    assert message_tool._sent_in_turn
    sent.assert_awaited_once()
    assert stream_calls == 1
    loop.provider.chat_with_retry.assert_awaited_once()
    assert reason == ("delivered_via_tool" if not final_text else "completed")
    assert contexts[-1].error is None
    assert run_contexts[-1].error is None
    assert all(m.get("content") != EMPTY_FINAL_RESPONSE_MESSAGE for m in messages)
    if not final_text:
        assert final is None
        assert messages[-1]["role"] == "tool"
        assert _turn_outcome(reason) == ("completed", None)
        session = Session(key="webui:tool-delivery")
        ctx = TurnContext(
            msg=InboundMessage(channel="websocket", sender_id="user", chat_id="chat", content="hi"),
            session_key=session.key,
            state=TurnState.SAVE,
            turn_id="tool-delivery",
            session=session,
            all_messages=[{"role": "system", "content": "system"}, *messages],
            stop_reason=reason,
            ephemeral=True,
        )
        await loop._state_save(ctx)
        assert ctx.final_content is None
        assert session.messages[-1]["role"] == "tool"
        assert all(m.get("content") != EMPTY_FINAL_RESPONSE_MESSAGE for m in session.messages)
    assert loop._assemble_outbound(
        InboundMessage(channel="telegram", sender_id="user", chat_id="chat", content="hi"),
        final, messages, reason, injected, on_stream,
    ) is None
    if not final_text:
        assert loop._assemble_outbound(
            InboundMessage(channel="telegram", sender_id="user", chat_id="chat", content="hi"),
            final, messages, reason, True, on_stream,
        ) is None
    published.assert_not_awaited()


@pytest.mark.asyncio
async def test_late_stream_delta_after_message_tool_send_is_not_published(tmp_path):
    from nanobot.agent.hook import AgentHookContext
    from nanobot.agent.progress_hook import AgentProgressHook

    loop = _make_loop(tmp_path)
    message_tool = loop.tools.get("message")
    message_tool.start_turn()
    published = AsyncMock()
    hook = AgentProgressHook(
        on_stream=published, reply_delivered=lambda: message_tool._sent_in_turn,
    )
    assert hook.wants_streaming()
    message_tool._sent_in_turn = True
    assert not hook.wants_streaming()
    await hook.on_stream(AgentHookContext(iteration=2, messages=[]), "duplicate reply")
    published.assert_not_awaited()
