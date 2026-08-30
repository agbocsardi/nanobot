"""Built-in memory_search tool: unified history + topic-memory literal search.

v1 scope: exact (literal substring) matching only. No embeddings, no fuzzy
matching — the semantic layer is deliberately backburnered.

Read-only: opens ``memory/history.jsonl`` and topic memory files with ``r``
mode only and never writes anything.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from nanobot.agent.memory import MemoryStore
from nanobot.agent.tools.base import Tool, ToolResult

_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}))?$")
_TS_HEAD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_DEFAULT_MAX_RESULTS = 10
_MAX_RESULTS_LIMIT = 100
_DEFAULT_MAX_EXCERPT_CHARS = 240
_MAX_EXCERPT_CHARS = 4000
_MAX_LINES_PER_MEMORY_FILE = 25
_MAX_MEMORY_FILES = 50
_MAX_OUTPUT_CHARS = 64_000
_MAX_MEMORY_FILE_BYTES = 2_000_000


def iter_history_entries(path: Path) -> Iterable[tuple[dict[str, Any], int]]:
    """Yield ``(entry, line_no)`` for each well-formed JSONL line.

    Blank lines and lines that do not parse as JSON objects are skipped
    silently (compact reader; no shelling out). ``line_no`` is the 1-based
    physical line, used as a citation fallback when a record has no cursor.
    """
    try:
        handle = open(path, "r", encoding="utf-8")
    except FileNotFoundError:
        return
    with handle:
        for line_no, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            yield entry, line_no


def _valid_cursor(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _normalize_ts(value: Any) -> str | None:
    """Normalize an entry/message timestamp to ``YYYY-MM-DD HH:MM``.

    v3 records use ISO timestamps (e.g. ``2025-06-16T10:30:00+00:00``),
    legacy records use ``YYYY-MM-DD HH:MM``. Returns None when the value does
    not carry a parsable date (such entries are excluded from date filters).
    """
    if not isinstance(value, str):
        return None
    head = value[:16].replace("T", " ")
    if not _TS_HEAD_RE.match(head):
        return None
    return head


def _parse_date_filter(value: str, label: str) -> str:
    match = _DATE_RE.match(value.strip())
    if not match:
        raise ValueError(
            f"Error: {label} must be 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM', got {value!r}"
        )
    date, time_ = match.group(1), match.group(2)
    if time_:
        return f"{date} {time_}"
    return f"{date} 00:00" if label == "date_from" else f"{date} 23:59"


def _contains(haystack: str, needle: str, case_sensitive: bool) -> bool:
    if case_sensitive:
        return needle in haystack
    return needle.lower() in haystack.lower()


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)] + "\u2026"


def _frontmatter_line_span(content: str) -> int:
    """Return the number of leading lines consumed by YAML frontmatter (0 if none)."""
    if not content.startswith("---\n"):
        return 0
    lines = content.split("\n")
    for idx in range(1, len(lines)):
        if lines[idx].startswith("---"):
            return idx + 1
    return 0


class MemorySearchTool(Tool):
    """Search conversation history and topic memory files with literal matching."""
    _scopes = {"core", "subagent"}

    @property
    def name(self) -> str:
        return "memory_search"

    @property
    def description(self) -> str:
        return (
            "Search the agent's archived conversation history (memory/history.jsonl) "
            "and topic memory files under memory/. Uses exact literal substring "
            "matching (no regex, no semantic search). History entries prefer their "
            "LLM summary, then structured messages, then legacy content. Returns "
            "compact cited snippets — timestamps, session keys, entry ids, roles — "
            "never whole sessions. Read-only."
        )

    @property
    def read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Literal text to search for (case-insensitive unless case_sensitive is true)",
                    "minLength": 1,
                },
                "date_from": {
                    "type": "string",
                    "description": "Only entries at/after this time: 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'",
                },
                "date_to": {
                    "type": "string",
                    "description": "Only entries at/before this time: 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'",
                },
                "session_key": {
                    "type": "string",
                    "description": "Only entries with this exact session key",
                },
                "role": {
                    "type": "string",
                    "description": (
                        "Only match structured messages with this exact role "
                        "(e.g. user, assistant). Summary and legacy-content matches "
                        "are excluded when role is set."
                    ),
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Match case-sensitively (default false)",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of history result snippets (default 10, max 100)",
                    "minimum": 1,
                    "maximum": _MAX_RESULTS_LIMIT,
                },
                "max_excerpt_chars": {
                    "type": "integer",
                    "description": "Per-snippet excerpt length cap in characters (default 240, max 4000)",
                    "minimum": 20,
                    "maximum": _MAX_EXCERPT_CHARS,
                },
                "search_memory_files": {
                    "type": "boolean",
                    "description": (
                        "Also search topic memory files under memory/ (default true). "
                        "memory/system/ and MEMORY.md are always excluded; memory/system/ "
                        "files are already loaded into context in full."
                    ),
                },
            },
            "required": ["query"],
        }

    def __init__(self, workspace: Path | None = None):
        self._workspace = workspace

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(workspace=Path(ctx.workspace))

    def _history_path(self) -> Path:
        return (self._workspace or Path(".")) / "memory" / "history.jsonl"

    # ------------------------------------------------------------------
    # matching helpers
    # ------------------------------------------------------------------

    def _match_entry(
        self,
        entry: dict[str, Any],
        line_no: int,
        query: str,
        *,
        role: str | None,
        case_sensitive: bool,
        max_excerpt_chars: int,
    ) -> list[dict[str, Any]]:
        """One result per matching summary / message / legacy content.

        Precedence mirrors :meth:`MemoryStore.history_entry_text`: summary
        first, then structured messages, then legacy content. A matching
        summary suppresses message-level results for that entry (compactness,
        summary wins). A role filter skips summary and legacy matches since
        those have no role.
        """
        results: list[dict[str, Any]] = []
        cursor = _valid_cursor(entry.get("cursor"))
        cursor_id = f"cursor={cursor}" if cursor is not None else f"line={line_no}"
        session_key = entry.get("session_key") if isinstance(entry.get("session_key"), str) else None
        entry_ts = _normalize_ts(entry.get("timestamp"))
        display_ts = (entry.get("timestamp") if isinstance(entry.get("timestamp"), str) else None) or "?"

        summary = entry.get("summary")
        has_summary = isinstance(summary, str) and bool(summary.strip())
        if has_summary and role is None and _contains(summary, query, case_sensitive):
            results.append({
                "kind": "summary",
                "cursor_id": cursor_id,
                "cursor": cursor,
                "line": line_no if cursor is None else None,
                "timestamp": display_ts,
                "session_key": session_key,
                "role": None,
                "excerpt": _truncate(summary.strip(), max_excerpt_chars),
            })
            return results

        messages = entry.get("messages")
        if isinstance(messages, list) and (not has_summary or role is not None):
            for idx, message in enumerate(messages, start=1):
                if not isinstance(message, dict):
                    continue
                msg_role = message.get("role")
                msg_content = message.get("content")
                if not isinstance(msg_content, str) or not msg_content:
                    continue
                if role is not None and msg_role != role:
                    continue
                if not _contains(msg_content, query, case_sensitive):
                    continue
                msg_ts = _normalize_ts(message.get("timestamp")) or entry_ts
                results.append({
                    "kind": "message",
                    "cursor_id": f"{cursor_id} msg={idx}",
                    "cursor": cursor,
                    "line": line_no if cursor is None else None,
                    "timestamp": msg_ts or "?",
                    "session_key": session_key,
                    "role": msg_role if isinstance(msg_role, str) else None,
                    "excerpt": _truncate(msg_content.strip(), max_excerpt_chars),
                })
            if results:
                return results

        content = entry.get("content")
        if role is None and isinstance(content, str) and _contains(content, query, case_sensitive):
            results.append({
                "kind": "content",
                "cursor_id": cursor_id,
                "cursor": cursor,
                "line": line_no if cursor is None else None,
                "timestamp": display_ts,
                "session_key": session_key,
                "role": None,
                "excerpt": _truncate(content.strip(), max_excerpt_chars),
            })
        return results

    def _search_history(
        self,
        query: str,
        *,
        date_from: str | None,
        date_to: str | None,
        session_key: str | None,
        role: str | None,
        case_sensitive: bool,
        max_results: int,
        max_excerpt_chars: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return ``(capped_results, total_matches)``, newest first."""
        matches: list[dict[str, Any]] = []
        for entry, line_no in iter_history_entries(self._history_path()):
            if session_key is not None and entry.get("session_key") != session_key:
                continue
            ts = _normalize_ts(entry.get("timestamp"))
            if (date_from is not None or date_to is not None) and ts is None:
                continue
            if date_from is not None and ts < date_from:
                continue
            if date_to is not None and ts > date_to:
                continue
            matches.extend(
                self._match_entry(
                    entry,
                    line_no,
                    query,
                    role=role,
                    case_sensitive=case_sensitive,
                    max_excerpt_chars=max_excerpt_chars,
                )
            )
        matches.sort(key=lambda m: m.get("timestamp") or "", reverse=True)
        total = len(matches)
        return matches[:max_results], total

    def _search_memory_files(
        self,
        query: str,
        *,
        case_sensitive: bool,
        max_excerpt_chars: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Search topic memory files; returns (file_results, skipped_count)."""
        workspace = self._workspace or Path(".")
        files = [
            Path(workspace) / rel
            for rel in MemoryStore._topic_files(workspace)
        ]
        file_results: list[dict[str, Any]] = []
        skipped = 0
        for path in files[: _MAX_MEMORY_FILES]:
            try:
                raw = path.read_bytes()
            except OSError:
                skipped += 1
                continue
            if len(raw) > _MAX_MEMORY_FILE_BYTES:
                skipped += 1
                continue
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                skipped += 1
                continue
            span = _frontmatter_line_span(content)
            matches: list[dict[str, Any]] = []
            for idx, line in enumerate(content.split("\n"), start=1):
                if idx <= span:
                    continue
                if not _contains(line, query, case_sensitive):
                    continue
                matches.append({
                    "line": idx,
                    "text": _truncate(line.strip(), max_excerpt_chars),
                })
                if len(matches) >= _MAX_LINES_PER_MEMORY_FILE:
                    break
            if matches:
                file_results.append({
                    "path": str(path.relative_to(workspace)),
                    "matches": matches,
                })
        return file_results, skipped

    # ------------------------------------------------------------------
    # execution + formatting
    # ------------------------------------------------------------------

    def _format_history_results(self, results: list[dict[str, Any]]) -> str:
        if not results:
            return "no history matches"
        lines: list[str] = []
        for idx, match in enumerate(results, start=1):
            bits = [match["cursor_id"], match.get("timestamp") or "?"]
            if match.get("session_key"):
                bits.append(f"session={match['session_key']}")
            if match.get("role"):
                bits.append(f"role={match['role']}")
            bits.append(match["kind"])
            lines.append(f"[{idx}] {' | '.join(bits)}")
            lines.append(f"    {match['excerpt']}")
        return "\n".join(lines)

    @staticmethod
    def _format_memory_results(results: list[dict[str, Any]]) -> str:
        if not results:
            return "no memory file matches"
        lines: list[str] = []
        for file_result in results:
            lines.append(file_result["path"] + ":")
            for match in file_result["matches"]:
                lines.append(f"  {match['line']}: {match['text']}")
        return "\n".join(lines)

    async def execute(
        self,
        query: str,
        date_from: str | None = None,
        date_to: str | None = None,
        session_key: str | None = None,
        role: str | None = None,
        case_sensitive: bool = False,
        max_results: int | None = None,
        max_excerpt_chars: int | None = None,
        search_memory_files: bool = True,
        **kwargs: Any,
    ) -> ToolResult:
        if not query.strip():
            return ToolResult.retryable_error("Error: query must not be empty")
        try:
            from_ts = _parse_date_filter(date_from, "date_from") if date_from is not None else None
            to_ts = _parse_date_filter(date_to, "date_to") if date_to is not None else None
        except ValueError as e:
            return ToolResult.retryable_error(str(e))
        if from_ts is not None and to_ts is not None and from_ts > to_ts:
            return ToolResult.retryable_error(
                f"Error: date_from {from_ts!r} is after date_to {to_ts!r}"
            )

        limit = _DEFAULT_MAX_RESULTS if max_results is None else max_results
        excerpt_chars = _DEFAULT_MAX_EXCERPT_CHARS if max_excerpt_chars is None else max_excerpt_chars

        history_results, history_total = self._search_history(
            query,
            date_from=from_ts,
            date_to=to_ts,
            session_key=session_key,
            role=role,
            case_sensitive=case_sensitive,
            max_results=limit,
            max_excerpt_chars=excerpt_chars,
        )
        memory_results: list[dict[str, Any]] = []
        skipped_memory = 0
        if search_memory_files:
            memory_results, skipped_memory = self._search_memory_files(
                query,
                case_sensitive=case_sensitive,
                max_excerpt_chars=excerpt_chars,
            )

        data: dict[str, Any] = {
            "query": query,
            "case_sensitive": case_sensitive,
            "history": history_results,
            "history_total_matches": history_total,
            "history_truncated": history_total > len(history_results),
            "memory_files": memory_results,
            "skipped_memory_files": skipped_memory,
        }

        header = (
            f'query: "{query}" — {history_total} history match(es), '
            f"{len(memory_results)} memory file(s) with matches"
        )
        body = "\n\n".join([
            "history:\n" + self._format_history_results(history_results),
            "memory files:\n" + self._format_memory_results(memory_results),
        ])
        notes: list[str] = []
        if history_total > len(history_results):
            notes.append(
                f"(showing {len(history_results)} of {history_total} history matches; "
                "raise max_results for more)"
            )
        if skipped_memory:
            notes.append(f"(skipped {skipped_memory} unreadable/oversized/extra memory files)")
        result_text = header + "\n\n" + body
        if notes:
            result_text += "\n\n" + "\n".join(notes)
        if len(result_text) > _MAX_OUTPUT_CHARS:
            result_text = result_text[:_MAX_OUTPUT_CHARS] + "\n(output truncated due to size)"
        return ToolResult(result_text, data=data)
