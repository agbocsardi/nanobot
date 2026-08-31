"""Small declarative policy engine for tool calls."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping
from urllib.parse import urlparse

from nanobot.agent.tools.context import current_request_context

PolicyOutcome = Literal["allow", "deny", "ask"]

# Canonical request-metadata key for the run's interaction mode. Entry points
# (channels, CLI, cron, heartbeat, delegated runs) stamp it so policy rules can
# match mode deterministically.
INTERACTION_MODE_KEY = "interaction_mode"

# Recognized interaction modes. Policy rules match these with fnmatch so
# suffixes/subsets (e.g. mode="cron*") keep working.
INTERACTION_MODES = ("audit", "exploration", "foreground", "cron", "heartbeat", "delegated")


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    outcome: PolicyOutcome
    rule_id: str | None = None
    reason: str | None = None
    resource: str | None = None
    # Present on ask outcomes when an interactive approval was recorded;
    # surfaced to the user so they can approve/deny the exact request.
    approval_token: str | None = None


def interaction_mode_from_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    fallback: str = "foreground",
) -> str:
    """Resolve the deterministic interaction mode for a request.

    Precedence: canonical ``interaction_mode`` / ``_interaction_mode`` keys,
    then the legacy ``run_type`` key, then legacy heartbeat/cron/delegated
    signals. ``fallback`` (usually the policy ``default_context`` mode) wins
    when nothing is stamped.
    """
    meta = metadata or {}
    for key in (INTERACTION_MODE_KEY, "_interaction_mode", "run_type"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if meta.get("heartbeat"):
        return "heartbeat"
    if meta.get("cron_job_id") or meta.get("_cron_trigger"):
        return "cron"
    if meta.get("injected_event") == "subagent_result":
        return "delegated"
    return fallback


def default_audit_read_only_rule() -> dict[str, str]:
    """Default catch-all installed when ``tools.auditModeReadOnly`` is enabled.

    Prepended so it acts as a base deny; explicitly allowed writes in audit
    mode (listed later) keep working while everything else is denied.
    """
    return {
        "id": "audit-readonly-default",
        "outcome": "deny",
        "mode": "audit",
        "mutation": "write",
        "reason": "audit mode is read-only",
    }


def default_exploration_mutation_ban_rule() -> dict[str, str]:
    """Default catch-all installed when ``tools.explorationModeDenyMutations`` is set."""
    return {
        "id": "exploration-mutation-ban-default",
        "outcome": "deny",
        "mode": "exploration",
        "mutation": "write",
        "reason": "exploration mode forbids state-mutating tools",
    }


def effective_policy_rules(
    rules: Iterable[Any],
    *,
    audit_mode_read_only: bool = False,
    exploration_mode_deny_mutations: bool = False,
) -> list[Any]:
    """Return configured rules plus installed default catch-alls.

    Defaults are prepended so they act as a base deny; configured rules listed
    later still win for their specific matches (last matching rule wins), so
    an explicit rule can carve out a targeted write in audit/exploration mode.
    """
    effective = list(rules)
    if audit_mode_read_only:
        effective.insert(0, default_audit_read_only_rule())
    if exploration_mode_deny_mutations:
        effective.insert(0, default_exploration_mutation_ban_rule())
    return effective


class ToolPolicy:
    """Evaluate ordered wildcard rules; the last matching rule wins."""

    _RESOURCE_KEYS = (
        "path",
        "working_dir",
        "workdir",
        "workspace",
        "repository",
        "repo",
        "channel",
        "host",
        "url",
        "city",
        "scope",
    )

    def __init__(
        self,
        rules: Iterable[Any] = (),
        *,
        default_context: Callable[[], dict[str, str | None]] | None = None,
    ):
        self.rules = list(rules)
        self.default_context = default_context or (lambda: {})

    def evaluate(
        self,
        tool_name: str,
        params: dict[str, Any],
        *,
        read_only: bool,
    ) -> PolicyDecision:
        request = current_request_context()
        metadata = request.metadata if request is not None else {}
        defaults = self.default_context()
        mode = interaction_mode_from_metadata(
            metadata,
            fallback=str(defaults.get("mode") or "foreground"),
        )
        model = str(metadata.get("model") or defaults.get("model") or "")
        preset = str(metadata.get("model_preset") or defaults.get("preset") or "")
        mutation = "read" if read_only else "write"
        resources = self.normalize_resources(params) or [""]
        approved = {
            str(item)
            for item in metadata.get("approved_policy_rules", [])
            if isinstance(item, (str, int))
        }
        approvals = request.approvals if request is not None else None

        winner = None
        winner_resource = None
        for rule in self.rules:
            if not self._matches(self._field(rule, "mode", "*"), mode):
                continue
            if not self._matches(self._field(rule, "tool", "*"), tool_name):
                continue
            if not self._matches(self._field(rule, "mutation", "*"), mutation):
                continue
            if not self._matches(self._field(rule, "model", "*"), model):
                continue
            if not self._matches(self._field(rule, "preset", "*"), preset):
                continue
            resource_pattern = self._field(rule, "resource", "*")
            matched_resource = next(
                (resource for resource in resources if self._matches(resource_pattern, resource)),
                None,
            )
            if matched_resource is None:
                continue
            winner = rule
            winner_resource = matched_resource or None

        if winner is None:
            return PolicyDecision("allow")
        outcome = self._field(winner, "outcome", "allow")
        rule_id = self._field(winner, "id", "") or None
        reason = self._field(winner, "reason", "") or None
        if outcome == "ask":
            if rule_id is not None and rule_id in approved:
                # Legacy pre-populated approval list in request metadata.
                outcome = "allow"
            elif approvals is not None and approvals.is_approved(
                rule_id or "", tool_name, winner_resource
            ):
                # User approved this exact call earlier in the session.
                outcome = "allow"
            elif approvals is not None and approvals.is_denied(
                rule_id or "", tool_name, winner_resource
            ):
                # User declined earlier in the session: same structured block
                # as an explicit deny rule.
                outcome = "deny"
                reason = "denied by user approval decision"
            else:
                pending = (
                    approvals.record_ask(rule_id or "", tool_name, winner_resource)
                    if approvals is not None
                    else None
                )
                return PolicyDecision(
                    outcome,
                    rule_id,
                    reason,
                    winner_resource,
                    approval_token=pending.token if pending is not None else None,
                )
        return PolicyDecision(outcome, rule_id, reason, winner_resource)

    @classmethod
    def normalize_resources(cls, params: dict[str, Any]) -> list[str]:
        resources: list[str] = []
        for key in cls._RESOURCE_KEYS:
            value = params.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            raw = value.strip()
            if key == "url":
                parsed = urlparse(raw)
                if parsed.hostname:
                    resources.append(f"host:{parsed.hostname.lower()}")
                resources.append(raw)
            elif key in {"path", "working_dir", "workdir", "workspace"}:
                resources.append(str(Path(raw).expanduser().resolve(strict=False)))
            else:
                resources.append(f"{key}:{raw}")
        return resources

    @staticmethod
    def _field(rule: Any, name: str, default: str) -> str:
        if isinstance(rule, dict):
            return str(rule.get(name, default))
        return str(getattr(rule, name, default))

    @staticmethod
    def _matches(pattern: str, value: str) -> bool:
        return fnmatch.fnmatchcase(value, pattern)

