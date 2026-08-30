"""Deterministic regressions: generated vault links must not be promoted as
verified success when the existence check (or postcondition) fails.

Incident-derived: a run pasted a freshly generated vault link and reported the
job done even though the link still 404'd; the run must settle as incomplete
and the persisted final message must carry the incomplete marker.
"""

from __future__ import annotations

import pytest

from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMResponse, ToolCallRequest
from tests.harness.helpers import StaticTool, persisted_final_content, run_script
from tests.harness.test_tool_outcome_regressions import _tool_turn

VAULT_URL = "https://vault.example/notes/ab12c3"


@pytest.mark.asyncio
async def test_vault_link_with_failed_existence_check_is_not_announced_as_success() -> None:
    tools = ToolRegistry()
    tools.register(StaticTool(
        "generate_link",
        ToolResult(
            f"candidate link: {VAULT_URL}",
            data={"url": VAULT_URL},
        ),
    ))
    tools.register(StaticTool(
        "check_link",
        ToolResult.retryable_error(
            "link check failed: vault note not found (HTTP 404)",
            data={"exists": False},
        ),
    ))
    responses = [
        LLMResponse(
            content="generating",
            tool_calls=[ToolCallRequest(id="call_1", name="generate_link", arguments={})],
        ),
        LLMResponse(
            content="verifying",
            tool_calls=[ToolCallRequest(
                id="call_2", name="check_link", arguments={"url": VAULT_URL},
            )],
        ),
        LLMResponse(content="done, link is ready"),
    ]

    result, _ = await run_script(responses, tools=tools)

    assert [event["status"] for event in result.tool_events] == [
        "success",
        "retryable_error",
    ]
    check = result.tool_events[1]
    assert check["operational_success"] is False
    assert check["verified"] is False
    assert check["retryable"] is True
    assert check["data"] == {"exists": False}
    # The unverified existence check is never counted as a used success.
    assert result.tools_used == ["generate_link"]
    assert result.stop_reason == "partial_completion"
    assert result.final_content.startswith(
        "Incomplete: one or more tool operations failed or could not be verified."
    )
    persisted = persisted_final_content(result)
    assert persisted.startswith(
        "Incomplete: one or more tool operations failed or could not be verified."
    )
    # The model's bare success claim is only reachable under the incomplete
    # marker, so the persisted record cannot be read as a verified success.
    assert "link is ready" in persisted


@pytest.mark.asyncio
async def test_vault_link_with_failed_postcondition_is_recorded_partial() -> None:
    tools = ToolRegistry()
    tools.register(StaticTool(
        "generate_link",
        ToolResult(
            f"candidate link: {VAULT_URL}",
            side_effects=[{"kind": "vault_note", "url": VAULT_URL}],
            postcondition="failed",  # existence could not be confirmed
        ),
    ))

    result, _ = await run_script(_tool_turn("generate_link"), tools=tools)

    event = result.tool_events[0]
    assert event["status"] == "partial"
    assert event["postcondition"] == "failed"
    assert event["verified"] is False
    assert result.tools_used == []
    assert result.stop_reason == "partial_completion"
    assert persisted_final_content(result).startswith(
        "Incomplete: one or more tool operations failed or could not be verified."
    )
