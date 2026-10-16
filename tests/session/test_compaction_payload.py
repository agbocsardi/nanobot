"""All request/measurement sites share one governed-payload composition."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.runner import _BACKFILL_CONTENT, AgentRunner, AgentRunSpec
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMResponse
from nanobot.session.compaction import CompactionEngine, ensure_ids, project
from tests.session.test_compaction import SummaryProvider, budget


@pytest.mark.parametrize("no_tools", [False, True])
async def test_governed_wire_payload_matches_golden_repair_and_transform_order(no_tools):
    provider = SummaryProvider()
    runner = AgentRunner(provider)
    calls = [{"id": name, "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
             for name in ("a", "b")]
    source = [
        {"role": "system", "content": "fixed"},
        {"role": "tool", "tool_call_id": "orphan", "content": "discard orphan"},
        {"role": "user", "content": "goal"},
        {"role": "assistant", "content": "working", "tool_calls": calls},
        {"role": "tool", "tool_call_id": "a", "name": "read_file", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
        ]},
        {"role": "user", "content": "continue"},
    ]
    ensure_ids(source)
    original = deepcopy(source)
    stages = []
    drop, backfill, images = runner._drop_orphan_tool_results, runner._backfill_missing_tool_results, runner._prepare_umans_image_budget

    def observe_drop(messages):
        stages.append("drop")
        return drop(messages)

    def observe_backfill(messages):
        stages.append("backfill")
        return backfill(messages)

    def observe_images(messages):
        stages.append("images")
        return images(messages)

    async def vision(messages, model):
        stages.append("vision")
        updated = deepcopy(messages)
        for message in updated:
            if isinstance(message.get("content"), list):
                message["content"] = "visual evidence"
        return updated

    runner._drop_orphan_tool_results = observe_drop
    runner._backfill_missing_tool_results = observe_backfill
    runner._prepare_umans_image_budget = observe_images
    spec = AgentRunSpec(initial_messages=source, tools=ToolRegistry(), model="physical-model",
                        context_window_tokens=32768, max_iterations=2, max_tool_result_chars=1000,
                        vision_handoff=SimpleNamespace(transform=vision))
    if no_tools:
        await runner._request_no_tools(spec, source)
    else:
        governed = await runner._govern_payload(spec, source)
        await runner._request_model(spec, governed, AgentHook(), AgentHookContext(iteration=0, messages=source))
    assert stages == ["drop", "backfill", "vision", "images"]
    assert provider.requests[-1]["messages"] == [
        {"role": "system", "content": "fixed"}, {"role": "user", "content": "goal"},
        {"role": "assistant", "content": "working", "tool_calls": calls},
        {"role": "tool", "tool_call_id": "a", "name": "read_file", "content": "visual evidence"},
        {"role": "tool", "tool_call_id": "b", "name": "read_file", "content": _BACKFILL_CONTENT},
        {"role": "user", "content": "continue"},
    ]
    assert source == original


async def test_actual_payload_no_progress_rejects_checkpoint_before_commit():
    provider = SummaryProvider()
    provider.response = LLMResponse(content="context_length_exceeded", finish_reason="error")
    source = [{"role": "user", "content": "old work " * 100},
              {"role": "assistant", "content": "progress " * 100},
              {"role": "user", "content": "new work"}]
    ensure_ids(source)
    checkpoint = {"version": 1, "summary": "summary", "first_kept_entry_id": source[-1]["_entry_id"],
                  "source_end_id": source[-1]["_entry_id"]}

    async def fixed_payload(messages, model):
        return [{"role": "user", "content": "identical transformed payload", "_entry_id": "fixed"}]

    commit = AsyncMock()
    spec = AgentRunSpec(initial_messages=source, tools=ToolRegistry(), model="physical-model",
                        context_window_tokens=32768, max_iterations=2, max_tool_result_chars=1000,
                        compaction_engine_factory=lambda: SimpleNamespace(prepare=AsyncMock(return_value=checkpoint)),
                        compaction_callback=commit, vision_handoff=SimpleNamespace(transform=fixed_payload))
    result = await AgentRunner(provider).run(spec)
    assert result.stop_reason == "context_budget_exceeded"
    assert "no progress in the transformed request payload" in result.error
    commit.assert_not_awaited()
    assert len(provider.requests) == 1


async def test_backfill_projection_references_durable_call_not_synthetic_result():
    provider = SummaryProvider()
    source = [{"role": "user", "content": "original goal " * 500},
              {"role": "assistant", "content": "progress " * 100},
              {"role": "assistant", "content": "", "tool_calls": [{
                  "id": "missing", "type": "function", "function": {"name": "read_file", "arguments": "{}"},
              }]}]
    ensure_ids(source)
    runner = AgentRunner(provider)
    spec = AgentRunSpec(initial_messages=source, tools=ToolRegistry(), model="physical-model",
                        max_iterations=2, max_tool_result_chars=1000)
    governed = await runner._govern_payload(spec, source)
    assert governed[-1]["_source_entry_id"] == source[-1]["_entry_id"]
    checkpoint = await CompactionEngine(provider, budget(provider)).prepare(governed, recent_tokens=20)
    assert checkpoint["source_end_id"] == source[-1]["_entry_id"]
    assert project(source, checkpoint)[-1] == source[-1]
