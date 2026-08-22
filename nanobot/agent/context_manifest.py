"""Deterministic three-tier context manifest assembly."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from nanobot.security.workspace_policy import is_path_within

ContextTier = Literal["constitutional", "current", "retrieved"]


@dataclass(frozen=True, slots=True)
class ContextManifestEntry:
    path: str
    owners: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContextSourceDecision:
    path: str
    tier: ContextTier
    selected: bool
    reason: str
    characters: int = 0
    estimated_tokens: int = 0
    owners: tuple[str, ...] = ()


@dataclass(slots=True)
class ContextAssembly:
    constitutional: str = ""
    current: str = ""
    retrieved: str = ""
    decisions: list[ContextSourceDecision] = field(default_factory=list)

    def report(self) -> dict[str, Any]:
        sources = [
            {
                "path": item.path,
                "tier": item.tier,
                "selected": item.selected,
                "reason": item.reason,
                "characters": item.characters,
                "estimated_tokens": item.estimated_tokens,
                "owners": list(item.owners),
            }
            for item in self.decisions
        ]
        return {
            "sources": sources,
            "totals": {
                tier: {
                    "characters": sum(
                        item.characters
                        for item in self.decisions
                        if item.tier == tier and item.selected
                    ),
                    "estimated_tokens": sum(
                        item.estimated_tokens
                        for item in self.decisions
                        if item.tier == tier and item.selected
                    ),
                }
                for tier in ("constitutional", "current", "retrieved")
            },
        }


class ContextManifestAssembler:
    """Load selected Markdown files under independent tier budgets."""

    def __init__(
        self,
        workspace: Path,
        *,
        manifest_path: str = "context-manifest.json",
        constitutional_budget_chars: int = 24_000,
        current_budget_chars: int = 8_000,
        retrieved_budget_chars: int = 24_000,
    ):
        self.workspace = workspace.expanduser().resolve()
        self.manifest_path = self.workspace / manifest_path
        self.budgets = {
            "constitutional": constitutional_budget_chars,
            "current": current_budget_chars,
            "retrieved": retrieved_budget_chars,
        }

    def assemble(
        self,
        query: str,
        *,
        owners: set[str] | None = None,
    ) -> ContextAssembly:
        manifest = self._load_manifest()
        assembly = ContextAssembly()
        normalized_query = query.casefold()
        active_owners = {owner.casefold() for owner in owners or set()}
        for tier in ("constitutional", "current", "retrieved"):
            entries = self._entries(manifest.get(tier, []))
            content, decisions = self._assemble_tier(
                tier,
                entries,
                normalized_query,
                active_owners,
            )
            setattr(assembly, tier, content)
            assembly.decisions.extend(decisions)
        return assembly

    def _assemble_tier(
        self,
        tier: ContextTier,
        entries: list[ContextManifestEntry],
        query: str,
        active_owners: set[str],
    ) -> tuple[str, list[ContextSourceDecision]]:
        remaining = self.budgets[tier]
        parts: list[str] = []
        decisions: list[ContextSourceDecision] = []
        for entry in entries:
            selected, reason = self._selected(tier, entry, query, active_owners)
            if not selected:
                decisions.append(ContextSourceDecision(
                    entry.path,
                    tier,
                    False,
                    reason,
                    owners=entry.owners,
                ))
                continue
            path = (self.workspace / entry.path).resolve(strict=False)
            if not is_path_within(path, self.workspace):
                decisions.append(ContextSourceDecision(
                    entry.path,
                    tier,
                    False,
                    "outside_workspace",
                    owners=entry.owners,
                ))
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                decisions.append(ContextSourceDecision(
                    entry.path,
                    tier,
                    False,
                    "unavailable",
                    owners=entry.owners,
                ))
                continue
            if remaining <= 0:
                decisions.append(ContextSourceDecision(
                    entry.path,
                    tier,
                    False,
                    "budget_exhausted",
                    owners=entry.owners,
                ))
                continue
            content = raw[:remaining]
            truncated = len(content) < len(raw)
            parts.append(f"## {entry.path}\n\n{content}")
            size = len(content)
            remaining -= size
            decisions.append(ContextSourceDecision(
                entry.path,
                tier,
                True,
                "selected_truncated" if truncated else reason,
                characters=size,
                estimated_tokens=(size + 3) // 4,
                owners=entry.owners,
            ))
        return "\n\n".join(parts), decisions

    @staticmethod
    def _selected(
        tier: ContextTier,
        entry: ContextManifestEntry,
        query: str,
        active_owners: set[str],
    ) -> tuple[bool, str]:
        if tier != "retrieved":
            return True, "pinned"
        owners = {owner.casefold() for owner in entry.owners}
        if owners & active_owners:
            return True, "owner_match"
        topic_terms = {
            owner.split(":", 1)[1]
            for owner in owners
            if owner.startswith(("topic:", "repo:")) and ":" in owner
        }
        terms = topic_terms | {keyword.casefold() for keyword in entry.keywords}
        if any(term and term in query for term in terms):
            return True, "keyword_match"
        return False, "not_relevant"

    def _load_manifest(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _entries(values: Any) -> list[ContextManifestEntry]:
        if not isinstance(values, list):
            return []
        entries: list[ContextManifestEntry] = []
        for value in values:
            if isinstance(value, str):
                entries.append(ContextManifestEntry(value))
            elif isinstance(value, dict) and isinstance(value.get("path"), str):
                entries.append(ContextManifestEntry(
                    path=value["path"],
                    owners=tuple(str(owner) for owner in value.get("owners", [])),
                    keywords=tuple(str(keyword) for keyword in value.get("keywords", [])),
                ))
        return entries
