"""False-success regressions derived from operational incidents."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.providers.base import LLMResponse, ToolCallRequest
from tests.harness.helpers import StaticTool, run_script


def _tool_turn(name: str, arguments: dict | None = None) -> list[LLMResponse]:
    return [
        LLMResponse(
            content="working",
            tool_calls=[ToolCallRequest(id="call_1", name=name, arguments=arguments or {})],
        ),
        LLMResponse(content="done"),
    ]


@pytest.mark.asyncio
async def test_exit_127_cannot_be_recorded_as_success() -> None:
    process = AsyncMock()
    process.communicate.return_value = (b"plausible output\n", b"not found\n")
    process.returncode = 127
    tools = ToolRegistry()
    tools.register(ExecTool())

    with patch.object(ExecTool, "_spawn", return_value=process):
        result, _ = await run_script(
            _tool_turn("exec", {"command": "missing-command"}),
            tools=tools,
        )

    event = result.tool_events[0]
    assert event["status"] == "retryable_error"
    assert event["execution_succeeded"] is True
    assert event["operational_success"] is False
    assert event["verified"] is False
    assert event["retryable"] is True
    assert event["exit_code"] == 127
    assert event["stdout"] == "plausible output\n"
    assert event["stderr"] == "not found\n"
    assert result.tools_used == []


@pytest.mark.asyncio
async def test_unchecked_side_effect_is_recorded_as_partial() -> None:
    tools = ToolRegistry()
    tools.register(StaticTool(
        "deliver",
        ToolResult(
            "accepted",
            side_effects=[{"kind": "message", "recipient": "user-1"}],
            postcondition="unchecked",
        ),
    ))

    result, _ = await run_script(_tool_turn("deliver"), tools=tools)

    event = result.tool_events[0]
    assert event["status"] == "partial"
    assert event["postcondition"] == "unchecked"
    assert event["verified"] is False
    assert event["side_effects"] == [{"kind": "message", "recipient": "user-1"}]
    assert result.tools_used == []


@pytest.mark.asyncio
async def test_policy_block_stays_distinct_from_operational_failure() -> None:
    tools = ToolRegistry()
    tools.register(StaticTool(
        "dangerous_action",
        ToolResult.policy_block(
            "blocked",
            evidence=[{"kind": "policy", "rule": "deny"}],
        ),
    ))

    result, _ = await run_script(_tool_turn("dangerous_action"), tools=tools)

    event = result.tool_events[0]
    assert event["status"] == "policy_block"
    assert event["retryable"] is False
    assert event["evidence"] == [{"kind": "policy", "rule": "deny"}]
