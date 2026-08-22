"""Incident-derived lifecycle regressions that must not depend on a live model."""

from __future__ import annotations

import pytest

from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE
from tests.harness.helpers import run_script, unused_tools


@pytest.mark.asyncio
async def test_iteration_exhaustion_uses_reserved_final_synthesis() -> None:
    tools = unused_tools()
    tools.execute.return_value = "plausible progress"

    result, provider = await run_script(
        [
            LLMResponse(
                content="working",
                tool_calls=[ToolCallRequest(id="call_1", name="work", arguments={})],
            ),
            LLMResponse(content="I reached the budget before verification."),
        ],
        tools=tools,
        max_iterations=1,
    )

    assert result.stop_reason == "max_iterations"
    assert result.final_content == "I reached the budget before verification."
    assert result.messages[-1] == {
        "role": "assistant",
        "content": "I reached the budget before verification.",
    }
    assert len(provider.requests) == 2
    assert provider.requests[-1]["tools"] is None


@pytest.mark.asyncio
async def test_missing_final_synthesis_is_not_reported_as_success() -> None:
    result, _ = await run_script(
        [
            LLMResponse(content="", finish_reason="stop"),
            LLMResponse(content="", finish_reason="stop"),
            LLMResponse(content="", finish_reason="stop"),
        ],
        max_iterations=2,
    )

    assert result.stop_reason == "empty_final_response"
    assert result.error == EMPTY_FINAL_RESPONSE_MESSAGE
    assert result.final_content == EMPTY_FINAL_RESPONSE_MESSAGE
