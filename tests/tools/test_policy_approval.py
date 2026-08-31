"""Ask-approval lifecycle and interaction-mode wiring for the policy engine."""

from __future__ import annotations

from typing import Any

import pytest

from nanobot.agent.tools.approval import ApprovalStore
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.context import RequestContext, bind_request_context, reset_request_context
from nanobot.agent.tools.policy import (
    ToolPolicy,
    default_audit_read_only_rule,
    default_exploration_mutation_ban_rule,
    effective_policy_rules,
    interaction_mode_from_metadata,
)
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import ToolPolicyRuleConfig


def _rule(rule_id: str, outcome: str, **match):
    return ToolPolicyRuleConfig(id=rule_id, outcome=outcome, **match)


class _Context:
    """Context managerish helper for binding a request context with a store."""

    def __init__(self, store: ApprovalStore | None = None, metadata: dict | None = None):
        self.store = store
        self.metadata = metadata or {}

    def bind(self):
        return bind_request_context(RequestContext(
            channel="cli",
            chat_id="direct",
            metadata=dict(self.metadata),
            approvals=self.store,
        ))

    def __enter__(self):
        self.token = self.bind()
        return self

    def __exit__(self, *exc):
        reset_request_context(self.token)
        return False


class _CountingTool(Tool):
    """Write tool that records executions; never read-only."""

    def __init__(self, name: str = "write_state"):
        self._name = name
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "Counting write tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:
        self.calls += 1
        return f"executed {self._name}"


# ---------------------------------------------------------------------------
# ApprovalStore unit behavior
# ---------------------------------------------------------------------------


def test_ask_records_pending_and_dedupes_same_call() -> None:
    store = ApprovalStore()
    first = store.record_ask("deploy", "exec")
    second = store.record_ask("deploy", "exec")

    assert first.token == second.token
    assert first.rule_id == "deploy"
    assert first.tool == "exec"
    assert len(store.pending_list()) == 1


def test_approve_caches_decision_with_bounded_ttl(monkeypatch) -> None:
    now = {"t": 1000.0}
    store = ApprovalStore(timeout_s=60, cache_ttl_s=30, now=lambda: now["t"])
    pending = store.record_ask("deploy", "exec")

    resolution = store.approve(pending.token)
    assert resolution is not None
    assert resolution.action == "approved"
    assert store.is_approved("deploy", "exec")
    assert store.pending_list() == []

    now["t"] += 29
    assert store.is_approved("deploy", "exec") is True
    now["t"] += 2  # past cache TTL
    assert store.is_approved("deploy", "exec") is False


def test_pending_approval_expires_after_timeout(monkeypatch) -> None:
    now = {"t": 1000.0}
    store = ApprovalStore(timeout_s=60, cache_ttl_s=600, now=lambda: now["t"])
    pending = store.record_ask("deploy", "exec")
    assert store.pending_list() == [pending]

    now["t"] += 61
    assert store.pending_list() == []
    assert store.approve(pending.token) is None

    # A fresh ask re-requests with a new token.
    renewed = store.record_ask("deploy", "exec")
    assert renewed.token != pending.token


def test_deny_caches_block_with_bounded_ttl(monkeypatch) -> None:
    now = {"t": 1000.0}
    store = ApprovalStore(timeout_s=60, cache_ttl_s=30, now=lambda: now["t"])
    pending = store.record_ask("deploy", "exec")

    resolution = store.deny(pending.token)
    assert resolution is not None
    assert resolution.action == "denied"
    assert store.is_denied("deploy", "exec") is True

    now["t"] += 31
    assert store.is_denied("deploy", "exec") is False


def test_approve_accepts_rule_id_when_unambiguous() -> None:
    store = ApprovalStore()
    store.record_ask("deploy", "exec")
    assert store.approve("deploy") is not None


def test_approve_rule_id_ambiguous_returns_none() -> None:
    store = ApprovalStore()
    store.record_ask("deploy", "exec", resource="/a")
    store.record_ask("deploy", "exec", resource="/b")

    assert store.approve("deploy") is None
    assert len(store.pending_list()) == 2
    # Exact tokens still work.
    pending = store.pending_list()[0]
    assert store.approve(pending.token) is not None


# ---------------------------------------------------------------------------
# Policy ask lifecycle (ToolPolicy.evaluate)
# ---------------------------------------------------------------------------


def test_ask_pending_then_approve_then_allow() -> None:
    store = ApprovalStore()
    policy = ToolPolicy([_rule("deploy", "ask", tool="exec")])

    with _Context(store=store) as ctx:
        pending = policy.evaluate("exec", {}, read_only=False)
        assert pending.outcome == "ask"
        assert pending.approval_token is not None

        assert store.approve(pending.approval_token) is not None
        approved = policy.evaluate("exec", {}, read_only=False)
        assert approved.outcome == "allow"

    assert ctx.store is store


def test_ask_pending_then_deny_then_structured_deny() -> None:
    store = ApprovalStore()
    policy = ToolPolicy([_rule("deploy", "ask", tool="exec")])

    with _Context(store=store):
        pending = policy.evaluate("exec", {}, read_only=False)
        assert pending.outcome == "ask"

        assert store.deny(pending.approval_token) is not None
        denied = policy.evaluate("exec", {}, read_only=False)
        assert denied.outcome == "deny"
        assert denied.rule_id == "deploy"


def test_ask_not_re_prompted_within_cache_ttl(monkeypatch) -> None:
    now = {"t": 1000.0}
    store = ApprovalStore(timeout_s=60, cache_ttl_s=30, now=lambda: now["t"])
    policy = ToolPolicy([_rule("deploy", "ask", tool="exec")])

    with _Context(store=store):
        pending = policy.evaluate("exec", {}, read_only=False)
        store.approve(pending.approval_token)

        assert policy.evaluate("exec", {}, read_only=False).outcome == "allow"
        now["t"] += 29
        assert policy.evaluate("exec", {}, read_only=False).outcome == "allow"
        now["t"] += 2
        re_asked = policy.evaluate("exec", {}, read_only=False)
        assert re_asked.outcome == "ask"
        assert re_asked.approval_token != pending.approval_token


def test_ask_without_store_keeps_legacy_blocking_behavior() -> None:
    policy = ToolPolicy([_rule("deploy", "ask", tool="exec")])
    with _Context(store=None):
        decision = policy.evaluate("exec", {}, read_only=False)
    assert decision.outcome == "ask"
    assert decision.approval_token is None


def test_legacy_approved_policy_rules_metadata_still_allows() -> None:
    policy = ToolPolicy([_rule("deploy", "ask", tool="exec")])
    with _Context(metadata={"approved_policy_rules": ["deploy"]}):
        assert policy.evaluate("exec", {}, read_only=False).outcome == "allow"


# ---------------------------------------------------------------------------
# Registry: structured policy_block with approval token / deny shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_ask_block_surfaces_approval_token() -> None:
    store = ApprovalStore()
    tool = _CountingTool()
    registry = ToolRegistry(policy=ToolPolicy([_rule("deploy", "ask", tool=tool.name)]))
    registry.register(tool)

    with _Context(store=store):
        result = await registry.execute(tool.name, {})

    assert result.status == "policy_block"
    assert result.data["decision"] == "ask"
    assert result.data["rule_id"] == "deploy"
    assert "approval_token" in result.data
    assert "/policy approve" in str(result)
    assert result.evidence[0]["approval_token"] == result.data["approval_token"]
    assert tool.calls == 0


@pytest.mark.asyncio
async def test_registry_ask_after_approve_executes() -> None:
    store = ApprovalStore()
    tool = _CountingTool()
    registry = ToolRegistry(policy=ToolPolicy([_rule("deploy", "ask", tool=tool.name)]))
    registry.register(tool)

    with _Context(store=store):
        blocked = await registry.execute(tool.name, {})
        assert store.approve(blocked.data["approval_token"]) is not None
        allowed = await registry.execute(tool.name, {})

    assert allowed.status == "success"
    assert "executed" in str(allowed)
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_registry_ask_after_deny_blocks_like_deny_rule() -> None:
    store = ApprovalStore()
    tool = _CountingTool()
    registry = ToolRegistry(policy=ToolPolicy([_rule("deploy", "ask", tool=tool.name)]))
    registry.register(tool)

    with _Context(store=store):
        blocked = await registry.execute(tool.name, {})
        assert store.deny(blocked.data["approval_token"]) is not None
        denied = await registry.execute(tool.name, {})

    assert denied.status == "policy_block"
    assert denied.data == {
        "decision": "deny",
        "rule_id": "deploy",
        "resource": None,
    }
    assert denied.evidence[0]["decision"] == "deny"
    assert tool.calls == 0


# ---------------------------------------------------------------------------
# Interaction-mode resolution and default audit/exploration rules
# ---------------------------------------------------------------------------


def test_interaction_mode_resolution_precedence() -> None:
    assert interaction_mode_from_metadata({}) == "foreground"
    assert interaction_mode_from_metadata({"interaction_mode": "audit"}) == "audit"
    assert interaction_mode_from_metadata({"_interaction_mode": "exploration"}) == "exploration"
    assert interaction_mode_from_metadata({"run_type": "cron"}) == "cron"
    assert interaction_mode_from_metadata({"heartbeat": True}) == "heartbeat"
    assert interaction_mode_from_metadata({"cron_job_id": "job-1"}) == "cron"
    assert interaction_mode_from_metadata({"_cron_trigger": {"job_id": "j"}}) == "cron"
    assert interaction_mode_from_metadata(
        {"injected_event": "subagent_result"}
    ) == "delegated"
    # Explicit mode beats legacy signals.
    assert interaction_mode_from_metadata(
        {"interaction_mode": "audit", "cron_job_id": "job-1"}
    ) == "audit"
    # fallback wins when nothing is stamped.
    assert interaction_mode_from_metadata(
        {}, fallback="delegated"
    ) == "delegated"


def test_effective_policy_rules_prepends_defaults_only_when_enabled() -> None:
    rules = [_rule("explicit", "allow")]
    assert effective_policy_rules(rules) == rules
    assert effective_policy_rules(rules, audit_mode_read_only=True)[0] == default_audit_read_only_rule()
    assert effective_policy_rules(
        rules, exploration_mode_deny_mutations=True
    )[0] == default_exploration_mutation_ban_rule()
    combined = effective_policy_rules(
        rules, audit_mode_read_only=True, exploration_mode_deny_mutations=True
    )
    assert {r["id"] for r in combined[:2]} == {
        "audit-readonly-default",
        "exploration-mutation-ban-default",
    }


def test_audit_read_only_default_denies_writes_but_keeps_reads() -> None:
    policy = ToolPolicy(
        effective_policy_rules([], audit_mode_read_only=True),
        default_context=lambda: {"mode": "audit"},
    )
    with _Context(metadata={"interaction_mode": "audit"}):
        assert policy.evaluate("write_file", {}, read_only=False).outcome == "deny"
        assert policy.evaluate("read_file", {}, read_only=True).outcome == "allow"
    # Foreground mode is untouched by the audit default.
    with _Context(metadata={"interaction_mode": "foreground"}):
        assert policy.evaluate("write_file", {}, read_only=False).outcome == "allow"


def test_exploration_mutation_ban_default() -> None:
    policy = ToolPolicy(
        effective_policy_rules([], exploration_mode_deny_mutations=True),
        default_context=lambda: {"mode": "exploration"},
    )
    with _Context(metadata={"interaction_mode": "exploration"}):
        assert policy.evaluate("exec", {}, read_only=False).outcome == "deny"
        assert policy.evaluate("read_file", {}, read_only=True).outcome == "allow"
    with _Context(metadata={"interaction_mode": "cron"}):
        assert policy.evaluate("exec", {}, read_only=False).outcome == "allow"


def test_explicit_rule_can_override_audit_default() -> None:
    policy = ToolPolicy(
        effective_policy_rules(
            [_rule("audit-write-ok", "allow", mode="audit", tool="write_file")],
            audit_mode_read_only=True,
        ),
        default_context=lambda: {"mode": "audit"},
    )
    with _Context(metadata={"interaction_mode": "audit"}):
        assert policy.evaluate("write_file", {}, read_only=False).outcome == "allow"
        assert policy.evaluate("exec", {}, read_only=False).outcome == "deny"


# ---------------------------------------------------------------------------
# Interaction-mode propagation through the agent loop entry point
# ---------------------------------------------------------------------------


def _make_loop(tmp_path):
    from unittest.mock import AsyncMock, MagicMock, patch

    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    with patch("nanobot.agent.loop.ContextBuilder"), \
         patch("nanobot.agent.loop.SessionManager"), \
         patch("nanobot.agent.loop.SubagentManager") as mock_sub_mgr:
        mock_sub_mgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)
    return loop


def _tool_then_final_response(tool_name: str):
    from nanobot.agent.runner import LLMResponse, ToolCallRequest

    return [
        LLMResponse(
            content="working",
            tool_calls=[ToolCallRequest(id="call_1", name=tool_name, arguments={})],
            usage={},
        ),
        LLMResponse(content="done", tool_calls=[], usage={}),
    ]


@pytest.mark.asyncio
async def test_loop_stamps_metadata_mode_into_policy(tmp_path) -> None:
    from unittest.mock import AsyncMock


    loop = _make_loop(tmp_path)
    tool = _CountingTool("write_state")
    policy = ToolPolicy(
        effective_policy_rules([], audit_mode_read_only=True),
        default_context=lambda: {"mode": "foreground"},
    )
    registry = ToolRegistry(policy=policy)
    registry.register(tool)
    loop.tools = registry

    responses = iter(_tool_then_final_response(tool.name))
    loop.provider.chat_with_retry = AsyncMock(side_effect=lambda **kwargs: next(responses))

    final_content, _, _, stop_reason, _ = await loop._run_agent_loop(
        [],
        session_key="cli:direct",
        metadata={"_interaction_mode": "audit"},
    )

    assert stop_reason == "policy_block"
    assert tool.calls == 0
    assert loop._approval_stores["cli:direct"] is not None


@pytest.mark.asyncio
async def test_loop_cron_mode_denies_write_deterministically(tmp_path) -> None:
    from unittest.mock import AsyncMock

    loop = _make_loop(tmp_path)
    tool = _CountingTool("write_state")
    policy = ToolPolicy(
        [ToolPolicyRuleConfig(
            id="cron-readonly",
            outcome="deny",
            mode="cron",
            mutation="write",
        )],
        default_context=lambda: {"mode": "foreground"},
    )
    registry = ToolRegistry(policy=policy)
    registry.register(tool)
    loop.tools = registry

    responses = iter(_tool_then_final_response(tool.name))
    loop.provider.chat_with_retry = AsyncMock(side_effect=lambda **kwargs: next(responses))

    _, _, _, stop_reason, _ = await loop._run_agent_loop(
        [],
        session_key="cli:direct",
        metadata={"_interaction_mode": "cron"},
    )

    assert stop_reason == "policy_block"
    assert tool.calls == 0


@pytest.mark.asyncio
async def test_loop_ask_approval_lifecycle_end_to_end(tmp_path) -> None:
    from unittest.mock import AsyncMock

    from nanobot.agent.runner import LLMResponse, ToolCallRequest

    loop = _make_loop(tmp_path)
    tool = _CountingTool("write_state")
    policy = ToolPolicy(
        [_rule("ask-write", "ask", tool=tool.name)],
        default_context=lambda: {"mode": "foreground"},
    )
    registry = ToolRegistry(policy=policy)
    registry.register(tool)
    loop.tools = registry

    async def scripted(**kwargs):
        return next(responses)

    responses = iter([
        *[LLMResponse(
            content="working",
            tool_calls=[ToolCallRequest(id="call_1", name=tool.name, arguments={})],
            usage={},
        )] * 2,
    ] + [LLMResponse(content="done", tool_calls=[], usage={})])
    loop.provider.chat_with_retry = AsyncMock(side_effect=scripted)

    # First turn: ask blocks the call and records a pending approval.
    _, _, _, stop_reason, _ = await loop._run_agent_loop(
        [],
        session_key="cli:direct",
        metadata={"_interaction_mode": "foreground"},
    )
    assert stop_reason == "policy_block"
    assert tool.calls == 0

    store = loop.approval_store("cli:direct")
    pending = store.pending_list()
    assert len(pending) == 1
    assert store.approve(pending[0].token) is not None

    # Second turn: approved cache allows the same call without re-prompting.
    responses = iter([
        LLMResponse(
            content="working",
            tool_calls=[ToolCallRequest(id="call_2", name=tool.name, arguments={})],
            usage={},
        ),
        LLMResponse(content="done", tool_calls=[], usage={}),
    ])
    _, _, _, stop_reason, _ = await loop._run_agent_loop(
        [],
        session_key="cli:direct",
        metadata={"_interaction_mode": "foreground"},
    )
    assert stop_reason != "policy_block"
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_process_direct_propagates_mode_metadata(tmp_path) -> None:
    """CLI/cron/heartbeat-style direct runs reach policy with the right mode."""
    from unittest.mock import AsyncMock, MagicMock

    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")

    tool = _CountingTool("write_state")
    policy = ToolPolicy(
        [_rule("cron-readonly", "deny", mode="cron", mutation="write")],
        default_context=lambda: {"mode": "foreground"},
    )
    registry = ToolRegistry(policy=policy)
    registry.register(tool)
    loop.tools = registry

    from nanobot.agent.runner import LLMResponse, ToolCallRequest

    responses = iter([
        LLMResponse(
            content="working",
            tool_calls=[ToolCallRequest(id="call_1", name=tool.name, arguments={})],
            usage={},
        ),
        LLMResponse(content="done", tool_calls=[], usage={}),
    ])
    loop.provider.chat_with_retry = AsyncMock(side_effect=lambda **kwargs: next(responses))

    response = await loop.process_direct(
        "do it",
        session_key="cron:job1",
        metadata={"_interaction_mode": "cron"},
    )

    assert tool.calls == 0
    assert response is not None
    assert response.content

    # A foreground direct run with the same tool passes (no cron rule applies).
    responses = iter([
        LLMResponse(
            content="working",
            tool_calls=[ToolCallRequest(id="call_2", name=tool.name, arguments={})],
            usage={},
        ),
        LLMResponse(content="done", tool_calls=[], usage={}),
    ])
    tool.calls = 0
    await loop.process_direct(
        "do it",
        session_key="sdk:default",
        metadata={"interaction_mode": "foreground"},
    )
    assert tool.calls == 1
