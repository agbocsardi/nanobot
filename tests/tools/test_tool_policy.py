from __future__ import annotations

from nanobot.agent.tools.context import RequestContext, bind_request_context, reset_request_context
from nanobot.agent.tools.policy import ToolPolicy
from nanobot.config.schema import ToolPolicyRuleConfig


def _rule(rule_id: str, outcome: str, **match):
    return ToolPolicyRuleConfig(id=rule_id, outcome=outcome, **match)


def test_no_rules_preserve_current_allow_behavior() -> None:
    assert ToolPolicy().evaluate("exec", {"command": "git status"}, read_only=False).outcome == "allow"


def test_last_matching_rule_wins_with_wildcards() -> None:
    policy = ToolPolicy([
        _rule("audit-readonly", "deny", mode="audit", mutation="write"),
        _rule("audit-inspector", "allow", mode="audit", tool="my", mutation="write"),
    ])
    token = bind_request_context(RequestContext(
        channel="cli",
        chat_id="direct",
        metadata={"interaction_mode": "audit"},
    ))
    try:
        denied = policy.evaluate("exec", {}, read_only=False)
        allowed = policy.evaluate("my", {}, read_only=False)
    finally:
        reset_request_context(token)

    assert denied.outcome == "deny"
    assert denied.rule_id == "audit-readonly"
    assert allowed.outcome == "allow"
    assert allowed.rule_id == "audit-inspector"


def test_repository_and_path_resources_are_normalized(tmp_path) -> None:
    approved = tmp_path / "approved"
    outside = tmp_path / "outside"
    policy = ToolPolicy([
        _rule("outside-path", "deny", tool="write_file", resource=f"{outside}/*"),
        _rule("wrong-repo", "deny", tool="exec", resource="repo:upstream/*"),
    ])

    assert policy.evaluate(
        "write_file",
        {"path": str(approved / "note.md")},
        read_only=False,
    ).outcome == "allow"
    assert policy.evaluate(
        "write_file",
        {"path": str(outside / "note.md")},
        read_only=False,
    ).outcome == "deny"
    assert policy.evaluate(
        "exec",
        {"repo": "upstream/nanobot"},
        read_only=False,
    ).rule_id == "wrong-repo"


def test_ask_requires_explicit_rule_approval() -> None:
    policy = ToolPolicy([_rule("deploy", "ask", tool="exec")])
    denied_token = bind_request_context(RequestContext(channel="cli", chat_id="direct"))
    try:
        pending = policy.evaluate("exec", {}, read_only=False)
    finally:
        reset_request_context(denied_token)

    approved_token = bind_request_context(RequestContext(
        channel="cli",
        chat_id="direct",
        metadata={"approved_policy_rules": ["deploy"]},
    ))
    try:
        approved = policy.evaluate("exec", {}, read_only=False)
    finally:
        reset_request_context(approved_token)

    assert pending.outcome == "ask"
    assert approved.outcome == "allow"


def test_autonomous_model_and_preset_constraints() -> None:
    policy = ToolPolicy(
        [_rule(
            "autonomous-model",
            "deny",
            mode="cron",
            mutation="write",
            model="expensive-*",
            preset="autonomous",
        )],
        default_context=lambda: {
            "mode": "cron",
            "model": "expensive-opus",
            "preset": "autonomous",
        },
    )

    decision = policy.evaluate("write_file", {}, read_only=False)

    assert decision.outcome == "deny"
    assert decision.rule_id == "autonomous-model"
