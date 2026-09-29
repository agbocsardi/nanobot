"""Umans caps one request at 20 images; old images become re-readable text."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec, ContextBudgetExceededError


def _tools():
    tools = MagicMock()
    tools.get_definitions.return_value = []
    return tools


def _umans_provider(primary=None):
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock()
    provider._spec = SimpleNamespace(name="umans")
    if primary is not None:
        provider._primary = primary
    return provider


def _image(idx, seen=False):
    message = {
        "role": "tool",
        "tool_call_id": f"call_{idx}",
        "name": "read_file",
        "content": [
            {"type": "text", "text": "(Image file)"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + "A" * 32},
                "_meta": {"path": f"/ws/tmp/context-images/{idx}.img"},
            },
        ],
    }
    if seen:
        message["_images_seen"] = True
    return message


def _batch(count, seen=False):
    """A realistic tool batch: user prompt, one assistant tool-call message, results."""
    tool_calls = [
        {"id": f"call_{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
        for i in range(count)
    ]
    messages = [
        {"role": "user", "content": "inspect screenshots"},
        {"role": "assistant", "content": "", "tool_calls": tool_calls},
    ]
    messages.extend(_image(i, seen=seen) for i in range(count))
    return messages


def _spec(messages, **kwargs):
    return AgentRunSpec(
        initial_messages=messages, tools=_tools(), model="test-model",
        max_iterations=1, max_tool_result_chars=1000, **kwargs,
    )


def test_non_umans_provider_keeps_all_images():
    runner = AgentRunner(MagicMock())
    messages = _batch(25, seen=True)
    assert runner._prepare_umans_image_budget(messages) is messages


def test_mock_provider_chain_walk_terminates():
    """A MagicMock mints a fresh object per attribute access; the walk must stop."""
    runner = AgentRunner(MagicMock())
    assert runner._is_umans_provider() is False


def test_fallback_wrapper_primary_is_detected():
    runner = AgentRunner(SimpleNamespace(_primary=_umans_provider()))
    assert runner._is_umans_provider() is True


def test_old_images_become_text_and_newest_twenty_stay():
    runner = AgentRunner(_umans_provider())
    messages = _batch(25, seen=True)
    result = runner._prepare_umans_image_budget(messages)

    blocks = [
        block
        for message in result
        for block in (message.get("content") or [])
        if isinstance(block, dict) and block.get("type") == "image_url"
    ]
    assert len(blocks) == 20
    placeholders = [
        block["text"]
        for message in result
        for block in (message.get("content") or [])
        if isinstance(block, dict) and block.get("type") == "text"
        and block.get("text", "").startswith("[image:")
    ]
    assert len(placeholders) == 5
    assert all("tmp/context-images" in text for text in placeholders)
    # The persisted conversation must not be modified.
    assert messages[2]["content"][1]["type"] == "image_url"
    assert messages[2].get("_images_seen") is True


def test_more_than_twenty_unseen_images_fail_locally():
    runner = AgentRunner(_umans_provider())
    with pytest.raises(ContextBudgetExceededError) as excinfo:
        runner._prepare_umans_image_budget(_batch(21))
    assert excinfo.value.unit == "images"
    assert excinfo.value.budget == 20


@pytest.mark.asyncio
async def test_run_reports_image_limit_without_calling_provider():
    provider = _umans_provider()
    result = await AgentRunner(provider).run(_spec(_batch(21), context_block_limit=5_000_000))
    assert result.stop_reason == "context_budget_exceeded"
    assert "20 images" in (result.final_content or "")
    provider.chat_with_retry.assert_not_awaited()
