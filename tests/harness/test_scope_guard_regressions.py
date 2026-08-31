"""Deterministic scope-guard regressions: unapproved repository targets and
closed scopes must be denied with a structured record and no execution.

Incident-derived: a repo-scoped operation was once run against an unapproved
fork and a closed scope accepted an out-of-scope item; both must settle as
explicit policy blocks whose persisted record cannot be read as success.
"""

from __future__ import annotations

import pytest

from nanobot.agent.tools.policy import ToolPolicy
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import ToolPolicyRuleConfig
from tests.harness.helpers import StaticTool, persisted_final_content, run_script
from tests.harness.test_tool_outcome_regressions import _tool_turn


def _tracked(tool: StaticTool) -> tuple[StaticTool, list[int]]:
    """Wrap execute so the fixture can prove the tool never ran."""
    calls: list[int] = []
    original_execute = tool.execute

    async def tracked_execute(**kwargs):
        calls.append(1)
        return await original_execute(**kwargs)

    tool.execute = tracked_execute  # type: ignore[method-assign]
    return tool, calls


# Last matching rule wins, so the deny must be scoped to the unapproved
# target: a broad "deny repo:*" would also veto approved repositories.
_REPO_POLICY_RULES = [
    ToolPolicyRuleConfig(
        id="repo-deny-unapproved",
        outcome="deny",
        resource="repo:evilcorp/*",
        reason="repository is not in the approved allowlist",
    ),
]


@pytest.mark.asyncio
async def test_wrong_repository_target_is_denied_with_structured_record() -> None:
    tool, calls = _tracked(StaticTool("sync_repo", "pushed upstream"))
    registry = ToolRegistry(policy=ToolPolicy(_REPO_POLICY_RULES))
    registry.register(tool)

    result, _ = await run_script(
        _tool_turn("sync_repo", {"repo": "evilcorp/nanobot"}),
        tools=registry,
    )

    # The denial happens before execution: the target repo was never touched.
    assert calls == []
    event = result.tool_events[0]
    assert event["status"] == "policy_block"
    assert event["execution_succeeded"] is False
    assert event["data"] == {
        "decision": "deny",
        "rule_id": "repo-deny-unapproved",
        "resource": "repo:evilcorp/nanobot",
    }
    assert event["evidence"] == [{
        "kind": "tool_policy",
        "decision": "deny",
        "rule_id": "repo-deny-unapproved",
    }]
    assert result.tools_used == []
    assert result.stop_reason == "policy_block"
    # Neither the in-memory nor the persisted record reads as success.
    assert result.final_content.startswith(
        "Incomplete: one or more required operations were blocked by policy."
    )
    persisted = persisted_final_content(result)
    assert persisted.startswith(
        "Incomplete: one or more required operations were blocked by policy."
    )
    assert "pushed upstream" not in result.final_content
    assert "pushed upstream" not in persisted


@pytest.mark.asyncio
async def test_approved_repository_still_executes_under_same_policy() -> None:
    tool, calls = _tracked(StaticTool("sync_repo", "pushed upstream"))
    registry = ToolRegistry(policy=ToolPolicy(_REPO_POLICY_RULES))
    registry.register(tool)

    result, _ = await run_script(
        _tool_turn("sync_repo", {"repo": "agbocsardi/nanobot"}),
        tools=registry,
    )

    assert calls == [1]
    assert result.tool_events[0]["status"] == "success"
    assert result.tools_used == ["sync_repo"]
    assert result.stop_reason == "completed"


# Last matching rule wins; the deny comes after the open-scope allow so a
# closed scope is rejected while open scopes keep working.
_CLOSED_SCOPE_RULES = [
    ToolPolicyRuleConfig(
        id="scope-open",
        outcome="allow",
        resource="scope:open-*",
    ),
    ToolPolicyRuleConfig(
        id="scope-closed",
        outcome="deny",
        resource="scope:closed-*",
        reason="scope is closed; new items are not accepted",
    ),
]


@pytest.mark.asyncio
async def test_closed_scope_denies_inbound_item_without_execution() -> None:
    tool, calls = _tracked(StaticTool("file_to_scope", "stored"))
    registry = ToolRegistry(policy=ToolPolicy(_CLOSED_SCOPE_RULES))
    registry.register(tool)

    result, _ = await run_script(
        _tool_turn("file_to_scope", {"scope": "closed-2024-q4"}),
        tools=registry,
    )

    assert calls == []
    event = result.tool_events[0]
    assert event["status"] == "policy_block"
    assert event["execution_succeeded"] is False
    assert event["data"] == {
        "decision": "deny",
        "rule_id": "scope-closed",
        "resource": "scope:closed-2024-q4",
    }
    assert event["evidence"] == [{
        "kind": "tool_policy",
        "decision": "deny",
        "rule_id": "scope-closed",
    }]
    assert result.tools_used == []
    assert result.stop_reason == "policy_block"
    assert persisted_final_content(result).startswith(
        "Incomplete: one or more required operations were blocked by policy."
    )


@pytest.mark.asyncio
async def test_open_scope_still_accepts_items_under_same_policy() -> None:
    tool, calls = _tracked(StaticTool("file_to_scope", "stored"))
    registry = ToolRegistry(policy=ToolPolicy(_CLOSED_SCOPE_RULES))
    registry.register(tool)

    result, _ = await run_script(
        _tool_turn("file_to_scope", {"scope": "open-docs"}),
        tools=registry,
    )

    assert calls == [1]
    assert result.tool_events[0]["status"] == "success"
    assert result.stop_reason == "completed"
