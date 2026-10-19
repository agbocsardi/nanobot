"""Deterministic checkpoint/continuation acceptance fixtures; no network or private sessions."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.hook import AgentHook
from nanobot.agent.memory import Consolidator
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import GenerationSettings, LLMResponse, ToolCallRequest
from nanobot.providers.errors import is_context_overflow
from nanobot.session.compaction import (
    CHECKPOINTS,
    CompactionEngine,
    CompactionError,
    RequestBudget,
    UsageEstimate,
    ensure_ids,
    find_cut,
    latest_checkpoint,
    project,
)
from nanobot.session.manager import SessionManager


class SummaryProvider:
    def __init__(self):
        self.generation = GenerationSettings(max_tokens=256)
        self.requests = []
        self.response = LLMResponse(content="## Goal\nFinish the original request.\n## In Progress\nContinue remaining work.")

    def get_default_model(self):
        return "physical-model"

    async def chat_with_retry(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        return self.response


def transcript(batches=8, size=400):
    messages = [{"role": "user", "content": "Original goal: execute every action exactly once. " * 200}]
    for index in range(batches):
        messages.extend([
            {"role": "assistant", "content": "working", "tool_calls": [{
                "id": f"call-{index}", "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"work.txt"}'},
            }]},
            {"role": "tool", "tool_call_id": f"call-{index}", "name": "read_file",
             "content": "evidence " * size},
        ])
    ensure_ids(messages)
    return messages


def budget(provider, window=8192):
    return RequestBudget.freeze(provider, "physical-model", window)


@pytest.mark.parametrize("tail_size", [50, 5000])
def test_cut_preserves_latest_call_without_retaining_whole_user_span(tail_size):
    messages = transcript()
    messages[-1]["content"] = "tail " * tail_size
    cut, turn = find_cut(messages, 100)
    assert cut == len(messages) - 2
    assert turn == 0
    assert messages[cut]["tool_calls"][0]["id"] == messages[-1]["tool_call_id"]


async def test_split_summary_iteration_and_reload_parity(tmp_path):
    provider = SummaryProvider()
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("test:projection")
    session.messages = transcript()
    sessions.save(session)
    originals = deepcopy(session.messages)
    # Generate from retained authoritative session evidence.
    engine = CompactionEngine(provider, budget(provider))
    first = await engine.prepare(session.messages, recent_tokens=40)
    assert first["turn_prefix_summary"]
    assert "Original goal" in str(provider.requests)
    assert first["details"]["read_files"] == ["work.txt"]
    assert "evidence evidence" in str(provider.requests)
    session.metadata[CHECKPOINTS] = [first]
    sessions.save(session, fsync=True)
    projection = session.get_history(projected=True)
    sessions.invalidate(session.key)
    reloaded = sessions.get_or_create(session.key)
    assert reloaded.messages == originals
    assert reloaded.get_history(projected=True) == projection
    assert reloaded.last_consolidated == 0
    # Fold the existing summary and only newly compactable suffix progress.
    reloaded.add_message("user", "new work " * 300)
    reloaded.add_message("assistant", "done " * 300)
    provider.requests.clear()
    second = await engine.prepare(reloaded.messages, previous=first, recent_tokens=20)
    assert second["first_kept_entry_id"] != first["first_kept_entry_id"]
    assert "Previous checkpoint" in str(provider.requests)
    assert "Original goal: execute" not in str(provider.requests)


@pytest.mark.parametrize("reason,text", [("error", "failed"), ("length", "partial"),
                                         ("aborted", "partial"), ("stop", "")])
async def test_failed_summary_does_not_mutate_source(reason, text):
    provider = SummaryProvider()
    provider.response = LLMResponse(content=text, finish_reason=reason)
    messages = transcript()
    original = deepcopy(messages)
    with pytest.raises(CompactionError, match="Summary rejected"):
        await CompactionEngine(provider, budget(provider)).prepare(messages, recent_tokens=50)
    assert messages == original


async def test_cancellation_and_timeout_do_not_create_checkpoint():
    provider = SummaryProvider()
    entered = asyncio.Event()

    async def wait(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    provider.chat_with_retry = wait
    messages = transcript()
    original = deepcopy(messages)
    task = asyncio.create_task(CompactionEngine(provider, budget(provider)).prepare(messages, recent_tokens=50))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(CompactionError, match="TimeoutError"):
        await CompactionEngine(provider, budget(provider), timeout=0.01).prepare(messages, recent_tokens=50)
    assert messages == original


async def test_summary_partitions_input_and_bounds_calls():
    provider = SummaryProvider()
    messages = transcript(size=1000)
    small = budget(provider, 3072)
    engine = CompactionEngine(provider, small, max_calls=2)
    with pytest.raises(CompactionError, match="call limit"):
        await engine.prepare(messages, recent_tokens=50)
    assert len(provider.requests) == 2
    for request in provider.requests:
        assert small.estimate(provider, request["messages"]) <= small.window - request["max_tokens"] - small.safety


def test_budget_usage_invalidation_and_invalid_output():
    provider = SummaryProvider()
    frozen = budget(provider)
    provider.generation = GenerationSettings(max_tokens=7000)
    assert frozen.output == 256
    messages = [{"role": "system", "content": "fixed"}, {"role": "user", "content": "question"}]
    usage = UsageEstimate()
    usage.record(frozen.model, messages, [], {"prompt_tokens": 3000})
    tail = messages + [{"role": "assistant", "content": "answer"}]
    assert usage.estimate(frozen, provider, tail, []) > 3000
    edited = deepcopy(tail)
    edited[0]["content"] = "changed"
    assert usage.estimate(frozen, provider, edited, []) < 3000
    assert usage.estimate(RequestBudget.freeze(provider, "other", 12000), provider, tail, []) < 3000
    assert usage.estimate(frozen, provider, tail, [{"new": "schema"}]) < 3000
    usage.record(frozen.model, messages, [], {"prompt_tokens": 0})
    usage.record(frozen.model, messages, [], {"prompt_tokens": "unknown"})
    assert usage.estimate(frozen, provider, tail, []) > 3000
    tagged = deepcopy(tail)
    tagged[0]["_entry_id"] = "internal-identity"
    assert usage.estimate(frozen, provider, tagged, []) > 3000
    missing = UsageEstimate()
    missing.record(frozen.model, messages, [], {})
    assert missing.estimate(frozen, provider, tail, []) < 3000
    with pytest.raises(CompactionError, match="Invalid request budget"):
        RequestBudget.freeze(provider, "other", 1024, 4096)


@pytest.mark.parametrize("kind,text,expected", [
    ("context_length_exceeded", "rejected", True),
    (None, "Error: maximum context length exceeded", True),
    ("connection", "maximum context length", False),
    ("timeout", "request timeout", False),
    (None, "HTTP 502 bad gateway", False),
])
def test_explicit_overflow_only(kind, text, expected):
    assert is_context_overflow(LLMResponse(content=text, finish_reason="error", error_kind=kind)) is expected


class SideEffect(Tool):
    name = "action"
    description = "Perform one counted action."
    parameters = {"type": "object", "properties": {"index": {"type": "integer"}}, "required": ["index"]}

    def __init__(self):
        self.executed = []

    async def execute(self, index):
        self.executed.append(index)
        return "evidence " * 250


class CountingProvider(SummaryProvider):
    def __init__(self, side_effect):
        super().__init__()
        self.side_effect = side_effect
        self.rejected = set()
        self.model_requests = []

    async def chat_with_retry(self, **kwargs):
        if kwargs.get("tools") is None:
            return await super().chat_with_retry(**kwargs)
        self.model_requests.append(deepcopy(kwargs))
        done = len(self.side_effect.executed)
        if done in {3, 6, 9} and done not in self.rejected:
            self.rejected.add(done)
            return LLMResponse(content="context_length_exceeded", finish_reason="error")
        if done == 10:
            return LLMResponse(content="all actions completed")
        return LLMResponse(content="", tool_calls=[ToolCallRequest(
            id=f"action-{done}", name="action", arguments={"index": done},
        )])

    async def chat_stream_with_retry(self, **kwargs):
        callback = kwargs.pop("on_content_delta", None)
        kwargs.pop("on_thinking_delta", None)
        kwargs.pop("on_tool_call_delta", None)
        kwargs.pop("on_stream_recover", None)
        response = await self.chat_with_retry(**kwargs)
        if response.finish_reason != "error" and callback and response.content:
            await callback(response.content)
        return response


class StreamHook(AgentHook):
    def __init__(self):
        self.parts = []

    def wants_streaming(self):
        return True

    async def on_stream(self, context, delta):
        self.parts.append(delta)


@pytest.mark.parametrize("streaming", [False, True])
async def test_repeated_overflows_in_one_turn_execute_each_effect_once(streaming):
    tool = SideEffect()
    tools = ToolRegistry()
    tools.register(tool)
    provider = CountingProvider(tool)
    checkpoints = []

    async def commit(messages, checkpoint):
        assert messages[-1]["role"] == "tool"  # No tools in flight.
        checkpoints.append(deepcopy(checkpoint))

    hook = StreamHook() if streaming else None
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "system", "content": "instructions"},
                          {"role": "user", "content": "Execute ten actions exactly once. " * 250}],
        tools=tools, model="physical-model", max_iterations=16, max_tool_result_chars=20000,
        context_window_tokens=12000, compaction_callback=commit, hook=hook,
    ))
    assert result.stop_reason == "completed", result.error
    assert result.final_content == "all actions completed"
    assert tool.executed == list(range(10))
    assert provider.rejected == {3, 6, 9}
    assert len(checkpoints) >= 3
    assert len([m for m in result.messages if m.get("tool_calls")]) == 10
    if streaming:
        assert hook.parts == ["all actions completed"]
    for request in provider.model_requests:
        declared = {tc["id"] for m in request["messages"] for tc in m.get("tool_calls", [])}
        assert all(m["tool_call_id"] in declared for m in request["messages"] if m["role"] == "tool")


async def test_completed_answer_is_not_repeated_or_compacted():
    provider = SummaryProvider()
    registry = ToolRegistry()
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "question"}], tools=registry,
        model="physical-model", max_iterations=3, max_tool_result_chars=1000,
        context_window_tokens=8192,
    ))
    assert result.stop_reason == "completed"
    assert len(provider.requests) == 1


@pytest.mark.parametrize("fixed", [False, True])
async def test_irreducible_inputs_report_specific_component(fixed):
    provider = SummaryProvider()
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "system", "content": "large " * (5000 if fixed else 1)},
                          {"role": "user", "content": "huge " * (1 if fixed else 5000)}],
        tools=ToolRegistry(), model="physical-model", max_iterations=3, max_tool_result_chars=1000,
        context_window_tokens=4096,
    ))
    assert result.stop_reason == "context_budget_exceeded"
    assert ("fixed instructions" if fixed else "latest request/tool exchange") in result.final_content
    assert not provider.requests


async def test_idle_failure_and_lock_race_do_not_advance_checkpoint(tmp_path):
    provider = SummaryProvider()
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("test:race")
    session.messages = transcript()
    sessions.save(session)
    consolidator = Consolidator(
        store=SimpleNamespace(workspace=tmp_path), provider=provider, model="physical-model",
        sessions=sessions, context_window_tokens=8192, build_messages=lambda **kw: [],
        get_tool_definitions=lambda: [], max_completion_tokens=256,
    )
    consolidator.flush_before_compaction = AsyncMock()
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(**kwargs):
        entered.set()
        await release.wait()
        return provider.response

    provider.chat_with_retry = blocked
    task = asyncio.create_task(consolidator.compact_idle_session(session.key))
    await asyncio.wait_for(entered.wait(), timeout=5)
    session.add_message("user", "newer message must survive")
    sessions.save(session)
    expected = deepcopy(session.messages)
    release.set()
    assert await task is None
    assert session.messages == expected
    assert latest_checkpoint(session.metadata) is None
    assert session.last_consolidated == 0
    sessions.invalidate(session.key)
    assert sessions.get_or_create(session.key).messages == expected


async def test_frozen_provider_and_output_survive_hot_swap():
    provider = SummaryProvider()
    runner = AgentRunner(provider)
    replacement = SummaryProvider()

    class SwapHook(AgentHook):
        async def before_run(self, context):
            provider.generation = GenerationSettings(max_tokens=7000)
            runner.provider = replacement

    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "question"}],
        tools=ToolRegistry(), model="physical-model", context_window_tokens=8192,
        max_iterations=2, max_tool_result_chars=1000, hook=SwapHook(),
    ))
    assert result.stop_reason == "completed"
    assert provider.requests[0]["max_tokens"] == 256
    assert provider.requests[0]["model"] == "physical-model"
    assert not replacement.requests


async def test_byte_only_overflow_partitions_summary_and_fits_actual_payload(monkeypatch):
    from nanobot.agent import runner as runner_module
    from nanobot.session import compaction as compaction_module
    from nanobot.utils.helpers import estimate_request_bytes

    monkeypatch.setattr(runner_module, "_MAX_REQUEST_BYTES", 4000)
    monkeypatch.setattr(compaction_module, "MAX_BYTES", 4000)
    provider = SummaryProvider()
    source = [{"role": "user", "content": "old question " * 500},
              {"role": "assistant", "content": "old answer " * 500},
              {"role": "user", "content": "continue with the next task"}]
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=source, tools=ToolRegistry(), model="physical-model",
        context_window_tokens=20000, max_iterations=2, max_tool_result_chars=1000,
    ))
    assert result.stop_reason == "completed", result.error
    assert len(result.messages) == 4  # Originals were never snipped.
    assert len(provider.requests) > 2  # Bounded input partitioning, then the continuation.
    assert all(estimate_request_bytes(r["messages"], r["tools"]) <= 4000 for r in provider.requests)


async def test_post_transform_expansion_is_checked_before_send():
    provider = SummaryProvider()
    handoff = SimpleNamespace(transform=AsyncMock(return_value=[
        {"role": "user", "content": "expanded image description " * 5000},
    ]))
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "image"}], tools=ToolRegistry(),
        model="physical-model", context_window_tokens=4096, max_iterations=2,
        max_tool_result_chars=1000, vision_handoff=handoff,
    ))
    assert result.stop_reason == "context_budget_exceeded"
    assert "latest request/tool exchange" in result.error
    assert not provider.requests


async def test_deterministic_provider_rejection_is_bounded_without_reexecuting_tools():
    tool = SideEffect()
    provider = CountingProvider(tool)
    normal = provider.chat_with_retry

    async def rejecting(**kwargs):
        if kwargs.get("tools") is not None and len(tool.executed) >= 3:
            provider.model_requests.append(deepcopy(kwargs))
            return LLMResponse(content="context_length_exceeded", finish_reason="error")
        return await normal(**kwargs)

    provider.chat_with_retry = rejecting
    tools = ToolRegistry()
    tools.register(tool)
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "execute ten actions " * 500}],
        tools=tools, model="physical-model", context_window_tokens=12000,
        max_iterations=16, max_tool_result_chars=20000,
    ))
    assert result.stop_reason == "context_budget_exceeded"
    assert tool.executed == [0, 1, 2]
    assert len(provider.model_requests) <= 7


async def test_summary_uses_image_handoff_projection_not_opaque_original_blocks():
    provider = SummaryProvider()
    model_calls = 0
    original_chat = provider.chat_with_retry

    async def chat(**kwargs):
        nonlocal model_calls
        if kwargs.get("tools") is None:
            return await original_chat(**kwargs)
        model_calls += 1
        if model_calls == 1:
            return LLMResponse(content="context_length_exceeded", finish_reason="error")
        return LLMResponse(content="finished")

    async def transform(messages, model):
        result = deepcopy(messages)
        for message in result:
            if message.get("role") == "tool" and isinstance(message.get("content"), list):
                message["content"] = "CRITICAL_VISUAL_EVIDENCE " * 150
        return result

    source = transcript(batches=4, size=100)
    source[0]["content"] = "Original user goal. " * 50
    source[2]["content"] = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]
    provider.chat_with_retry = chat
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=source, tools=ToolRegistry(), model="physical-model",
        context_window_tokens=12000, max_iterations=2, max_tool_result_chars=1000,
        vision_handoff=SimpleNamespace(transform=transform),
    ))
    assert result.stop_reason == "completed", result.error
    assert "CRITICAL_VISUAL_EVIDENCE" in str(provider.requests)
    assert "base64" not in str(provider.requests)
    assert isinstance(result.messages[2]["content"], list)  # Original representation retained.


async def test_irreducible_fixed_prompt_with_old_history_never_commits_checkpoint():
    provider = SummaryProvider()
    commit = AsyncMock()
    source = [{"role": "system", "content": "fixed " * 8000},
              {"role": "user", "content": "old task " * 500},
              {"role": "assistant", "content": "old answer " * 500},
              {"role": "user", "content": "new task"}]
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=source, tools=ToolRegistry(), model="physical-model",
        context_window_tokens=4096, max_iterations=2, max_tool_result_chars=1000,
        compaction_callback=commit,
    ))
    assert result.stop_reason == "context_budget_exceeded"
    assert "fixed instructions" in result.error
    commit.assert_not_awaited()
    assert not provider.requests


@pytest.mark.parametrize("single_call", [False, True])
async def test_provider_retry_does_not_hide_overflow_or_exceed_summary_call_bound(single_call):
    from tests.providers.test_provider_retry import ScriptedProvider

    error = "503 transient service error" if single_call else "context_length_exceeded"
    provider = ScriptedProvider([LLMResponse(content=error, finish_reason="error")])
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "inspect image"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
    ]}]
    original = deepcopy(messages)
    response = await provider.chat_with_retry(
        messages=messages, retry_mode="none" if single_call else "standard",
    )
    assert response.finish_reason == "error"
    assert provider.calls == 1
    assert messages == original


def test_old_session_has_deterministic_ids_and_invalid_reference_is_rejected(tmp_path):
    messages = [{"role": "user", "content": "old session"}]
    left, right = deepcopy(messages), deepcopy(messages)
    ensure_ids(left)
    ensure_ids(right)
    assert left == right
    with pytest.raises(CompactionError, match="reference is missing"):
        project(left, {"version": 1, "summary": "goal", "first_kept_entry_id": "missing"})
