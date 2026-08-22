from __future__ import annotations

import pytest

from nanobot.agent.tools.policy import ToolPolicy
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import ToolPolicyRuleConfig
from tests.harness.helpers import StaticTool, run_script
from tests.harness.test_tool_outcome_regressions import _tool_turn


@pytest.mark.asyncio
async def test_policy_denial_is_structured_and_prevents_execution() -> None:
    tool = StaticTool("write_state", "mutated")
    calls = 0
    original_execute = tool.execute

    async def tracked_execute(**kwargs):
        nonlocal calls
        calls += 1
        return await original_execute(**kwargs)

    tool.execute = tracked_execute  # type: ignore[method-assign]
    policy = ToolPolicy(
        [ToolPolicyRuleConfig(
            id="audit-readonly",
            outcome="deny",
            mode="audit",
            mutation="write",
            reason="audit mode is read-only",
        )],
        default_context=lambda: {"mode": "audit"},
    )
    registry = ToolRegistry(policy=policy)
    registry.register(tool)

    result, _ = await run_script(_tool_turn("write_state"), tools=registry)

    assert calls == 0
    event = result.tool_events[0]
    assert event["status"] == "policy_block"
    assert event["execution_succeeded"] is False
    assert event["data"] == {
        "decision": "deny",
        "rule_id": "audit-readonly",
        "resource": None,
    }
    assert event["evidence"] == [{
        "kind": "tool_policy",
        "decision": "deny",
        "rule_id": "audit-readonly",
    }]
    assert result.stop_reason == "policy_block"
