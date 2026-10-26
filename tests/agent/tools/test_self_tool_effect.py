"""Per-call effect classification for the ``my`` tool.

A failed ``my check`` is a read helper failing, not a verdict on a chat reply,
so the action has to classify as a read. ``my set`` mutates running config and
must stay a write everywhere that classification is used: the runner's
read-only event flag, batching, and read-only policy modes.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.tools.context import (
    RequestContext,
    bind_request_context,
    reset_request_context,
)
from nanobot.agent.tools.policy import ToolPolicy
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.self import MyTool
from nanobot.config.schema import ToolPolicyRuleConfig


def _runtime_state() -> MagicMock:
    loop = MagicMock()
    loop.model = "test/model"
    loop.max_iterations = 40
    loop.context_window_tokens = 65_536
    loop.workspace = Path("/tmp/workspace")
    loop._runtime_vars = {}
    loop._current_iteration = 0
    loop._last_usage = {}
    return loop


def _tool() -> MyTool:
    return MyTool(runtime_state=_runtime_state())


def test_read_actions_classify_as_read() -> None:
    tool = _tool()
    for action in ("status", "check", "inspect"):
        assert tool.effect_for({"action": action}) == "read", action


def test_mutating_and_unknown_actions_stay_writes() -> None:
    tool = _tool()
    for action in ("set", "modify", "", "nonsense"):
        assert tool.effect_for({"action": action}) == "local_write", action
    assert tool.effect_for({}) == "local_write"


def test_tool_advertises_read_only_but_never_batches() -> None:
    tool = _tool()
    assert tool.read_only is True
    # `set` mutates live config, so it must not run alongside other calls.
    assert tool.exclusive is True
    assert tool.concurrency_safe is False


def _audit_registry() -> ToolRegistry:
    policy = ToolPolicy([
        ToolPolicyRuleConfig(id="audit-readonly", outcome="deny", mode="audit", mutation="write"),
    ])
    registry = ToolRegistry(policy=policy)
    registry.register(_tool())
    return registry


def test_audit_mode_allows_reading_and_denies_setting() -> None:
    registry = _audit_registry()
    tool = registry.get("my")
    assert tool is not None
    token = bind_request_context(RequestContext(
        channel="telegram",
        chat_id="1",
        metadata={"interaction_mode": "audit"},
    ))
    try:
        allowed = registry.evaluate_policy(tool, {"action": "check", "key": "model"})
        denied = registry.evaluate_policy(tool, {"action": "set", "key": "model", "value": "x"})
    finally:
        reset_request_context(token)

    assert allowed.outcome == "allow"
    assert denied.outcome == "deny"
    assert denied.rule_id == "audit-readonly"


def test_exploration_mode_denies_setting_too() -> None:
    policy = ToolPolicy([
        ToolPolicyRuleConfig(
            id="exploration-no-mutation", outcome="deny", mode="exploration", mutation="write",
        ),
    ])
    registry = ToolRegistry(policy=policy)
    registry.register(_tool())
    tool = registry.get("my")
    assert tool is not None
    token = bind_request_context(RequestContext(
        channel="telegram",
        chat_id="1",
        metadata={"interaction_mode": "exploration"},
    ))
    try:
        allowed = registry.evaluate_policy(tool, {"action": "status"})
        denied = registry.evaluate_policy(tool, {"action": "set", "key": "max_iterations", "value": 80})
    finally:
        reset_request_context(token)

    assert allowed.outcome == "allow"
    assert denied.outcome == "deny"


@pytest.mark.asyncio
async def test_failed_check_still_reports_its_error() -> None:
    """The read classification must not swallow the diagnostic itself."""
    tool = _tool()
    result = await tool.execute(action="check", key="tools")
    assert "not accessible" in result
