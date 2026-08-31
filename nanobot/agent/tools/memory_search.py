"""Built-in memory_search tool: unified history + topic-memory literal search.

v1 scope: exact (literal substring) matching only. No embeddings, no fuzzy
matching — the semantic layer is deliberately backburnered.

v2 additions (memory_search GitHub issue #1 continuation):
- Incremental append-only history index (``memory/search_index.json``).  The
  index records per history.jsonl entry: byte offset, physical line number and
  the timestamp/session_key/cursor metadata.  It lets searches (a) skip
  re-parsing the head of history.jsonl on repeated searches and (b) iterate
  newest-first straight from the tail.  Raw JSONL remains the source of truth;
  the index is disposable and rebuilt from scratch when invalid or when the
  history file is shorter (truncation) than the indexed size.  Index writes are
  atomic (tmp+rename+fsync) and strictly limited to the index file.
- Pagination: ``offset`` + ``max_results`` with ``history_offset`` and
  ``history_has_more`` in the structured data; total match count stays accurate
  across pages.
- Config knob: ``tools.memorySearch.includeSystemFiles`` (bool, default false).
  When true, memory/system/*.md and MEMORY.md are searched too, with the same
  return shape.  Default behavior (flag off) is unchanged from v1.
- Relevance ranking stays simple: newest-first, summary beats messages per
  entry (v1 behavior).  No semantic embeddings.

v3 additions (memory_search GitHub issue #1 continuation):

- Ranking modes behind ``tools.memorySearch.ranking``.
  * ``"recency"`` (default) — v1/v2 behavior, unchanged: newest-first,
    summary beats messages per entry.
  * ``"local"`` — deterministic dependency-free scoring: token overlap
    (query tokens vs match text) + a mild recency boost; sorted by score
    desc, ties by timestamp desc (see ``nanobot/agent/tools/memory_ranking.py``).
- Pluggable interface: the scorer protocol lives in ``memory_ranking`` and
  ``LocalOverlapScorer`` is the only implementation — a deterministic,
  dependency-free ``1.0 + term-frequency nudge`` scorer.  Retrieval-only —
  scoring never promotes facts into memory.
- Structured data: history results gain ``score`` (float for ``local``,
  ``null`` for ``recency``) and the data block gains ``ranking``; text output
  is unchanged for ``recency`` and shows scores for ``local``.  Memory-file
  results keep their v2 shape (line snippets, no per-line scores).
- Index semantics unchanged: ranking applies post-search, after the literal
  match + metadata filters.

Read-only with respect to user data: history.jsonl and memory files are opened
in ``r``/``rb`` mode only.  The only file this tool ever writes is the
disposable sidecar index ``memory/search_index.json`` (skipped entirely when
the index is disabled).
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Iterable

from pydantic import field_validator

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.memory_ranking import LocalOverlapScorer
from nanobot.config_base import Base

# NOTE: ``nanobot.agent.memory`` is imported lazily inside the memory-file
# search path (not at module level).  ``nanobot.config.schema`` eagerly
# forward-resolves tool config classes at import time, and a module-level
# import here would re-enter ``memory.py`` while it is still half-imported
# (memory.py -> session.manager -> config.loader -> config.schema), making the
# eager resolution fail silently and dropping the schema re-exports.

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

# --- history index -----------------------------------------------------------
_INDEX_FILENAME = "search_index.json"
_INDEX_VERSION = 1


_RANKING_MODES = ("recency", "local")


class MemorySearchToolConfig(Base):
    """Configuration for the memory_search tool (tools.memorySearch.*)."""

    enable_index: bool = True  # maintain memory/search_index.json cache
    include_system_files: bool = False  # also search memory/system/*.md + MEMORY.md
    ranking: str = "recency"  # "recency" (v1/v2) | "local" (1.0 + term-frequency nudge)

    @field_validator("ranking")
    @classmethod
    def _validate_ranking(cls, value: str) -> str:
        value = value or "recency"
        if value not in _RANKING_MODES:
            raise ValueError(
                f"tools.memorySearch.ranking must be one of {_RANKING_MODES}, got {value!r}"
            )
        return value


def _entry_meta(entry: dict[str, Any]) -> dict[str, Any]:
    """Down-projected per-entry index metadata: timestamp/session_key/cursor."""
    meta: dict[str, Any] = {}
    ts = entry.get("timestamp")
    if isinstance(ts, str) and ts:
        meta["timestamp"] = ts
    session_key = entry.get("session_key")
    if isinstance(session_key, str) and session_key:
        meta["session_key"] = session_key
    cursor = _valid_cursor(entry.get("cursor"))
    if cursor is not None:
        meta["cursor"] = cursor
    return meta


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


def _scan_history_entries(
    history: Path,
    *,
    start_offset: int = 0,
    start_line: int = 0,
) -> tuple[list[dict[str, Any]], int, int]:
    """Scan history.jsonl from ``start_offset`` and build index metadata.

    Returns ``(entries, size, next_line)`` where ``size`` is the byte position
    read up to (next resume point) and ``next_line`` the physical line count
    reached.  Physically blank/corrupt lines bump ``line`` but are not indexed;
    valid dict lines are indexed with their byte offset.
    """
    entries: list[dict[str, Any]] = []
    pos = start_offset
    line_no = start_line
    try:
        with open(history, "rb") as f:
            f.seek(start_offset)
            while True:
                raw = f.readline()
                if not raw:
                    break
                line_no += 1
                line_start = pos
                pos += len(raw)
                if not raw.strip():
                    continue
                try:
                    entry = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(entry, dict):
                    continue
                meta = {"offset": line_start, "line": line_no}
                meta.update(_entry_meta(entry))
                entries.append(meta)
    except FileNotFoundError:
        pos = 0
    return entries, pos, line_no


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
    config_key = "memory_search"

    @property
    def name(self) -> str:
        return "memory_search"

    @property
    def description(self) -> str:
        return (
            "Search the agent's archived conversation history (memory/history.jsonl) "
            "and topic memory files under memory/. Uses exact literal substring "
            "matching (no regex). Ranking defaults to recency (newest-first). "
            "tools.memorySearch.ranking='local' enables deterministic "
            "dependency-free relevance scoring. History entries "
            "prefer their LLM summary, then structured messages, then legacy "
            "content. Returns compact cited snippets — timestamps, session keys, "
            "entry ids, roles, scores — never whole sessions. Read-only."
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
                "offset": {
                    "type": "integer",
                    "description": "Number of history matches to skip (paging; default 0)",
                    "minimum": 0,
                    "default": 0,
                },
                "search_memory_files": {
                    "type": "boolean",
                    "description": (
                        "Also search topic memory files under memory/ (default true). "
                        "memory/system/ files and MEMORY.md are only searched when "
                        "tools.memorySearch.includeSystemFiles is enabled; otherwise "
                        "they are excluded (already loaded into context in full)."
                    ),
                },
            },
            "required": ["query"],
        }

    def __init__(
        self,
        workspace: Path | None = None,
        *,
        enable_index: bool = True,
        include_system_files: bool = False,
        ranking: str = "recency",
    ):
        if ranking not in _RANKING_MODES:
            raise ValueError(
                f"tools.memorySearch.ranking must be one of {_RANKING_MODES}, got {ranking!r}"
            )
        self._workspace = workspace
        self._enable_index = bool(enable_index)
        self._include_system_files = bool(include_system_files)
        self._ranking = ranking
        # Local scorer is a zero-dependency deterministic implementation.
        self._scorer: LocalOverlapScorer | None = (
            LocalOverlapScorer() if ranking == "local" else None
        )
        self._index_lock = threading.Lock()  # serialize index build/write

    @classmethod
    def config_cls(cls) -> type[MemorySearchToolConfig]:
        return MemorySearchToolConfig

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        cfg = getattr(ctx.config, "memory_search", None)
        if cfg is None:
            return cls(workspace=Path(ctx.workspace))
        return cls(
            workspace=Path(ctx.workspace),
            enable_index=cfg.enable_index,
            include_system_files=cfg.include_system_files,
            ranking=cfg.ranking,
        )

    def _history_path(self) -> Path:
        return (self._workspace or Path(".")) / "memory" / "history.jsonl"

    def _index_path(self) -> Path:
        return (self._workspace or Path(".")) / "memory" / _INDEX_FILENAME

    # ------------------------------------------------------------------
    # history index (incremental, append-only)
    # ------------------------------------------------------------------

    def _load_index(self) -> dict[str, Any] | None:
        """Read + structurally validate the sidecar index; None when unusable."""
        path = self._index_path()
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict) or data.get("version") != _INDEX_VERSION:
            return None
        size = data.get("size")
        entries = data.get("entries")
        next_line = data.get("line")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return None
        if not isinstance(next_line, int) or isinstance(next_line, bool) or next_line < 0:
            return None
        if not isinstance(entries, list):
            return None
        last_offset = -1
        for ent in entries:
            if not isinstance(ent, dict):
                return None
            offset = ent.get("offset")
            line = ent.get("line")
            if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                return None
            if not isinstance(line, int) or isinstance(line, bool) or line < 1:
                return None
            if offset <= last_offset:
                return None
            for key in ("timestamp", "session_key"):
                value = ent.get(key)
                if value is not None and not isinstance(value, str):
                    return None
            if "cursor" in ent:
                cursor = ent["cursor"]
                if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
                    return None
            last_offset = offset
        if entries and last_offset >= size:
            return None
        if entries and entries[-1]["line"] > next_line:
            return None
        return {"size": size, "line": next_line, "entries": entries}

    def _write_index(self, index: dict[str, Any]) -> None:
        """Atomically persist the index (tmp + fsync + rename). Index file only."""
        path = self._index_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        payload = json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        tmp = path.with_name(path.name + ".tmp")
        try:
            with open(tmp, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            # Best-effort directory fsync for durability (unsupported on some platforms).
            try:
                dir_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def ensure_index(self) -> tuple[int, list[dict[str, Any]]]:
        """Build/verify the history index; return ``(size, entries)``.

        Fast path: indexed size matches the current history size.  Append path:
        scan only the tail after the last indexed byte.  Rebuild path: index
        missing/invalid, or the history file is shorter than indexed (truncation)
        or the resume boundary is mid-line — rescan the whole file from scratch.
        """
        with self._index_lock:
            history = self._history_path()
            try:
                hist_size = history.stat().st_size
            except OSError:
                hist_size = 0

            index = self._load_index()
            valid = index is not None and index["size"] <= hist_size
            if valid and index["size"] == hist_size:
                return index["size"], index["entries"]

            if valid and index["size"] > 0:
                # Resume boundary must be a line start; otherwise fall back to rebuild.
                try:
                    with open(history, "rb") as f:
                        f.seek(index["size"] - 1)
                        if f.read(1) != b"\n":
                            valid = False
                except OSError:
                    valid = False

            if valid and index["size"] < hist_size:
                added, size, next_line = _scan_history_entries(
                    history, start_offset=index["size"], start_line=index["line"]
                )
                entries = [*index["entries"], *added]
                self._write_index(
                    {"version": _INDEX_VERSION, "size": size, "line": next_line, "entries": entries}
                )
                return size, entries

            # Rebuild from scratch (missing/invalid index, truncation, mid-line).
            entries, size, next_line = _scan_history_entries(history, start_offset=0, start_line=0)
            self._write_index(
                {"version": _INDEX_VERSION, "size": size, "line": next_line, "entries": entries}
            )
            return size, entries

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
        scorer: LocalOverlapScorer | None = None,
    ) -> list[dict[str, Any]]:
        """One result per matching summary / message / legacy content.

        Precedence mirrors :meth:`MemoryStore.history_entry_text`: summary
        first, then structured messages, then legacy content. A matching
        summary suppresses message-level results for that entry (compactness,
        summary wins). A role filter skips summary and legacy matches since
        those have no role.

        ``scorer`` attaches a relevance score to each result (local/provider
        ranking).  When ``None`` (recency ranking) every result carries
        ``"score": None``.
        """
        results: list[dict[str, Any]] = []
        cursor = _valid_cursor(entry.get("cursor"))
        cursor_id = f"cursor={cursor}" if cursor is not None else f"line={line_no}"
        session_key = entry.get("session_key") if isinstance(entry.get("session_key"), str) else None
        entry_ts = _normalize_ts(entry.get("timestamp"))
        display_ts = (entry.get("timestamp") if isinstance(entry.get("timestamp"), str) else None) or "?"

        def scored(full_text: str, ts: str | None) -> float | None:
            if scorer is None:
                return None
            return round(float(scorer.score(query, full_text, ts)), 6)

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
                "score": scored(summary, entry_ts),
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
                    "score": scored(msg_content, msg_ts),
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
                "score": scored(content, entry_ts),
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
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return ``(page, total_matches)``, honoring offset.

        Recency ranking (default): newest first (v1/v2 order, unchanged).
        Local/provider ranking: score desc, ties by timestamp desc.  Ranking
        applies post-search — the literal + metadata filters run unchanged
        and the scorer only reorders the matched results.
        """
        history_path = self._history_path()
        matches: list[dict[str, Any]] = []
        scorer = self._scorer

        if self._enable_index:
            _, entries = self.ensure_index()
            # Newest-first: iterate the index from the tail, reading only the
            # line bytes of filter-passing entries (metadata filters avoid the
            # disk read entirely for rejected entries).
            try:
                with open(history_path, "rb") as f:
                    for ent in reversed(entries):
                        if session_key is not None and ent.get("session_key") != session_key:
                            continue
                        ts = _normalize_ts(ent.get("timestamp"))
                        if (date_from is not None or date_to is not None) and ts is None:
                            continue
                        if date_from is not None and ts < date_from:
                            continue
                        if date_to is not None and ts > date_to:
                            continue
                        f.seek(ent["offset"])
                        raw = f.readline()
                        if not raw:
                            continue
                        try:
                            entry = json.loads(raw)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        if not isinstance(entry, dict):
                            continue
                        matches.extend(
                            self._match_entry(
                                entry,
                                ent["line"],
                                query,
                                role=role,
                                case_sensitive=case_sensitive,
                                max_excerpt_chars=max_excerpt_chars,
                                scorer=scorer,
                            )
                        )
            except FileNotFoundError:
                matches = []
        else:
            # Index disabled: full forward scan (v1 behavior), and never touch
            # the index file.
            for entry, line_no in iter_history_entries(history_path):
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
                        scorer=scorer,
                    )
                )

        if scorer is not None:
            matches.sort(
                key=lambda m: (m.get("score") or 0.0, m.get("timestamp") or ""),
                reverse=True,
            )
        else:
            matches.sort(key=lambda m: m.get("timestamp") or "", reverse=True)
        total = len(matches)
        return matches[offset : offset + max_results], total

    def _search_memory_files(
        self,
        query: str,
        *,
        case_sensitive: bool,
        max_excerpt_chars: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Search topic memory files; returns (file_results, skipped_count).

        When ``include_system_files`` is enabled, memory/system/*.md and
        MEMORY.md are searched with the same per-file (path, line, text) shape.
        """
        from nanobot.agent.memory import MemoryStore  # deferred: see module note

        workspace = self._workspace or Path(".")
        files = [
            Path(workspace) / rel
            for rel in MemoryStore._topic_files(workspace)
        ]
        if self._include_system_files:
            memory_dir = Path(workspace) / "memory"
            system_dir = memory_dir / "system"
            try:
                system_files = sorted(p for p in system_dir.rglob("*.md"))
            except OSError:
                system_files = []
            files.extend(system_files)
            mem_md = memory_dir / "MEMORY.md"
            if mem_md.exists():
                files.append(mem_md)
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

    def _format_history_results(
        self,
        results: list[dict[str, Any]],
        *,
        show_scores: bool = False,
    ) -> str:
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
            if show_scores:
                score = match.get("score")
                score_text = f"{score:.3f}" if isinstance(score, (int, float)) else "?"
                bits.append(f"score={score_text}")
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
        offset: int | None = None,
        search_memory_files: bool = True,
        **kwargs: Any,
    ) -> ToolResult:
        if not query.strip():
            return ToolResult.retryable_error("Error: query must not be empty")
        if not isinstance(offset, (int, type(None))) or isinstance(offset, bool):
            return ToolResult.retryable_error("Error: offset must be a non-negative integer")
        if offset is None:
            offset = 0
        if offset < 0:
            return ToolResult.retryable_error("Error: offset must be a non-negative integer")
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
            offset=offset,
        )
        memory_results: list[dict[str, Any]] = []
        skipped_memory = 0
        if search_memory_files:
            memory_results, skipped_memory = self._search_memory_files(
                query,
                case_sensitive=case_sensitive,
                max_excerpt_chars=excerpt_chars,
            )

        has_more = offset + len(history_results) < history_total
        data: dict[str, Any] = {
            "query": query,
            "case_sensitive": case_sensitive,
            "offset": offset,
            "ranking": self._ranking,
            "history": history_results,
            "history_total_matches": history_total,
            "history_offset": offset,
            "history_has_more": has_more,
            "history_truncated": has_more,
            "memory_files": memory_results,
            "skipped_memory_files": skipped_memory,
        }

        header = (
            f'query: "{query}" — {history_total} history match(es), '
            f"{len(memory_results)} memory file(s) with matches"
        )
        body = "\n\n".join([
            "history:\n"
            + self._format_history_results(history_results, show_scores=self._ranking == "local"),
            "memory files:\n" + self._format_memory_results(memory_results),
        ])
        notes: list[str] = []
        if has_more:
            notes.append(
                f"(showing {offset + len(history_results)} of {history_total} history matches; "
                "raise max_results or use offset for more)"
            )
        if skipped_memory:
            notes.append(f"(skipped {skipped_memory} unreadable/oversized/extra memory files)")
        result_text = header + "\n\n" + body
        if notes:
            result_text += "\n\n" + "\n".join(notes)
        if len(result_text) > _MAX_OUTPUT_CHARS:
            result_text = result_text[:_MAX_OUTPUT_CHARS] + "\n(output truncated due to size)"
        return ToolResult(result_text, data=data)
