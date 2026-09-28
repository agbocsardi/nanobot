"""Hard context-budget tests for multimodal prompts."""

from base64 import b64encode
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.utils.helpers import estimate_prompt_tokens
from nanobot.utils.image_budget import prepare_image


def _spec(messages, tools, **kwargs):
    return AgentRunSpec(
        initial_messages=messages, tools=tools, model="test-model",
        max_iterations=1, max_tool_result_chars=1000, **kwargs,
    )

def _tools():
    tools = MagicMock()
    tools.get_definitions.return_value = []
    return tools

@pytest.mark.asyncio
async def test_prompt_estimate_counts_multiple_image_blocks():
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "inspect"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 4000}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "B" * 4000}},
    ]}]
    assert estimate_prompt_tokens(messages) > estimate_prompt_tokens(
        [{"role": "user", "content": "inspect"}]
    )

@pytest.mark.asyncio
async def test_oversized_image_tail_never_calls_provider():
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock()
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "inspect"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 12000}},
    ]}]
    result = await AgentRunner(provider).run(_spec(
        messages, _tools(), context_block_limit=20,
    ))
    assert result.stop_reason == "context_budget_exceeded"
    assert "too large" in (result.final_content or "")
    provider.chat_with_retry.assert_not_awaited()

@pytest.mark.asyncio
async def test_governance_exception_still_rejects_oversized_prompt():
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock()
    runner = AgentRunner(provider)
    runner._snip_history = MagicMock(side_effect=RuntimeError("governance failed"))
    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 12000}},
    ]}]
    result = await runner.run(_spec(messages, _tools(), context_block_limit=20))
    assert result.stop_reason == "context_budget_exceeded"
    provider.chat_with_retry.assert_not_awaited()


def _prepared_png_block(color):
    out = BytesIO()
    Image.new("RGB", (320, 240), color).save(out, format="PNG")
    raw, mime = prepare_image(out.getvalue(), "image/png")
    return {"type": "image_url", "image_url": {
        "url": f"data:{mime};base64,{b64encode(raw).decode()}"
    }}

@pytest.mark.asyncio
async def test_multiple_prepared_pngs_are_budgeted_and_fail_locally():
    messages = [{"role": "user", "content": [
        _prepared_png_block((200, 20, 20)), _prepared_png_block((20, 20, 200)),
    ]}]
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock()
    result = await AgentRunner(provider).run(_spec(
        messages, _tools(), context_block_limit=20,
    ))
    assert result.stop_reason == "context_budget_exceeded"
    assert "images" in (result.final_content or "")
    provider.chat_with_retry.assert_not_awaited()
