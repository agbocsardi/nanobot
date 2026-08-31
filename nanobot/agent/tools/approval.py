"""Session-scoped approval lifecycle for interactive tool-policy decisions.

When a policy rule resolves to ``ask``, the registry blocks the tool call and
records a :class:`PendingApproval` in the session's :class:`ApprovalStore`.
The user approves or denies it (e.g. ``/policy approve <token>`` or
``/policy deny <token>``); the decision is then cached for a bounded TTL so the
same call is not re-prompted. A pending approval that is never answered
expires after ``timeout_s`` and the next attempt re-requests it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable

DEFAULT_APPROVAL_TIMEOUT_S = 300.0
DEFAULT_APPROVAL_CACHE_TTL_S = 600.0


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "rule"


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """An unanswered ``ask`` decision waiting for the user."""

    token: str
    rule_id: str
    tool: str
    resource: str | None
    requested_at: float  # wall-clock seconds
    expires_at: float  # wall-clock seconds


@dataclass(frozen=True, slots=True)
class ApprovalResolution:
    """Outcome of an approve/deny command for one pending approval."""

    token: str
    rule_id: str
    tool: str
    resource: str | None
    action: str  # "approved" | "denied"
    cache_ttl_s: float


class ApprovalStore:
    """Per-session pending/decided policy approvals with bounded TTLs.

    Not thread/loop safe: one store belongs to one session, and sessions are
    serialized by the agent loop's per-session dispatch lock.
    """

    def __init__(
        self,
        *,
        timeout_s: float = DEFAULT_APPROVAL_TIMEOUT_S,
        cache_ttl_s: float = DEFAULT_APPROVAL_CACHE_TTL_S,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.timeout_s = timeout_s
        self.cache_ttl_s = cache_ttl_s
        self._now = now
        self._pending: dict[str, PendingApproval] = {}
        self._pending_by_call: dict[tuple[str, str, str], str] = {}
        self._approved: dict[tuple[str, str, str], float] = {}
        self._denied: dict[tuple[str, str, str], float] = {}
        self._next_seq = 0

    @staticmethod
    def _cache_key(rule_id: str, tool: str, resource: str | None) -> tuple[str, str, str]:
        return (rule_id, tool, resource or "")

    def _now_value(self) -> float:
        return self._now()

    def record_ask(
        self,
        rule_id: str,
        tool: str,
        resource: str | None = None,
    ) -> PendingApproval:
        """Record (or return the existing) pending approval for an ask decision."""
        call_key = self._cache_key(rule_id, tool, resource)
        self._prune_pending()
        token = self._pending_by_call.get(call_key)
        if token is not None and token in self._pending:
            return self._pending[token]
        self._next_seq += 1
        requested_at = self._now_value()
        pending = PendingApproval(
            token=f"pa{self._next_seq}-{_slug(rule_id)}",
            rule_id=rule_id,
            tool=tool,
            resource=resource,
            requested_at=requested_at,
            expires_at=requested_at + self.timeout_s,
        )
        self._pending[pending.token] = pending
        self._pending_by_call[call_key] = pending.token
        return pending

    def is_approved(self, rule_id: str, tool: str, resource: str | None = None) -> bool:
        key = self._cache_key(rule_id, tool, resource)
        expiry = self._approved.get(key)
        if expiry is None:
            return False
        if expiry <= self._now_value():
            self._approved.pop(key, None)
            return False
        return True

    def is_denied(self, rule_id: str, tool: str, resource: str | None = None) -> bool:
        key = self._cache_key(rule_id, tool, resource)
        expiry = self._denied.get(key)
        if expiry is None:
            return False
        if expiry <= self._now_value():
            self._denied.pop(key, None)
            return False
        return True

    def approve(self, token_or_rule_id: str) -> ApprovalResolution | None:
        """Approve a pending approval by token, or by rule id when unambiguous.

        Returns the resolution, or None when the token is unknown/expired or a
        bare rule id is ambiguous (multiple pending approvals share it).
        """
        pending = self._find_pending(token_or_rule_id)
        if pending is None:
            return None
        self._remove_pending(pending.token)
        key = self._cache_key(pending.rule_id, pending.tool, pending.resource)
        self._approved[key] = self._now_value() + self.cache_ttl_s
        return ApprovalResolution(
            token=pending.token,
            rule_id=pending.rule_id,
            tool=pending.tool,
            resource=pending.resource,
            action="approved",
            cache_ttl_s=self.cache_ttl_s,
        )

    def deny(self, token_or_rule_id: str) -> ApprovalResolution | None:
        """Deny a pending approval; the same call then blocks like a deny rule."""
        pending = self._find_pending(token_or_rule_id)
        if pending is None:
            return None
        self._remove_pending(pending.token)
        key = self._cache_key(pending.rule_id, pending.tool, pending.resource)
        self._denied[key] = self._now_value() + self.cache_ttl_s
        return ApprovalResolution(
            token=pending.token,
            rule_id=pending.rule_id,
            tool=pending.tool,
            resource=pending.resource,
            action="denied",
            cache_ttl_s=self.cache_ttl_s,
        )

    def pending_list(self) -> list[PendingApproval]:
        """Return not-yet-expired pending approvals, oldest first."""
        self._prune_pending()
        return sorted(self._pending.values(), key=lambda p: p.requested_at)

    def prune(self) -> None:
        """Drop expired pending approvals."""
        self._prune_pending()

    def _find_pending(self, token_or_rule_id: str) -> PendingApproval | None:
        self._prune_pending()
        if token_or_rule_id in self._pending:
            return self._pending[token_or_rule_id]
        matches = [p for p in self._pending.values() if p.rule_id == token_or_rule_id]
        if len(matches) == 1:
            return matches[0]
        return None

    def _remove_pending(self, token: str) -> None:
        pending = self._pending.pop(token, None)
        if pending is None:
            return
        self._pending_by_call.pop(
            self._cache_key(pending.rule_id, pending.tool, pending.resource),
            None,
        )

    def _prune_pending(self) -> None:
        now = self._now_value()
        expired = [t for t, p in self._pending.items() if p.expires_at <= now]
        for token in expired:
            self._remove_pending(token)
