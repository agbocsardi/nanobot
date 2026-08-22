"""Small declarative policy engine for tool calls."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Literal
from urllib.parse import urlparse

from nanobot.agent.tools.context import current_request_context

PolicyOutcome = Literal["allow", "deny", "ask"]


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    outcome: PolicyOutcome
    rule_id: str | None = None
    reason: str | None = None
    resource: str | None = None


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
        mode = str(
            metadata.get("interaction_mode")
            or metadata.get("run_type")
            or ("heartbeat" if metadata.get("heartbeat") else None)
            or ("cron" if metadata.get("cron_job_id") else None)
            or defaults.get("mode")
            or "foreground"
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
        if outcome == "ask" and rule_id in approved:
            outcome = "allow"
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
