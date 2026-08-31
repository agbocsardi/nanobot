"""Built-in memory_read tool: list, read, and literal-search curated topic memory.

Topic memory files under ``memory/`` are the curated, agent-editable surface:
``list`` shows their frontmatter title/description/updated before any
body, ``read`` returns one file's frontmatter plus full body, and ``search``
finds exact literal text in topic files and in ``memory/history.jsonl``
summaries/messages. ``history.jsonl`` is append-only evidence and is only ever
*searched* here, never modified. ``memory/system/*`` and ``MEMORY.md`` are
already loaded into context and are excluded from this tool's file surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobot.agent.memory import MemoryStore
from nanobot.agent.tools._memory_common import (
    MemoryPathError,
    parse_tags,
    resolve_topic_path,
    topic_file_exists,
    topic_rel_path,
)
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.memory_search import (
    _DEFAULT_MAX_EXCERPT_CHARS,
    _DEFAULT_MAX_RESULTS,
    _MAX_EXCERPT_CHARS,
    _MAX_MEMORY_FILE_BYTES,
    _MAX_MEMORY_FILES,
    _MAX_OUTPUT_CHARS,
    _MAX_RESULTS_LIMIT,
    MemorySearchTool,
    iter_history_entries,
)

_ACTIONS = ("list", "read", "search")


class MemoryReadTool(Tool):
    """Read/list/search the curated topic-memory surface; history is read-only."""

    _scopes = {"core", "subagent", "memory"}

    @property
    def name(self) -> str:
        return "memory_read"

    @property
    def description(self) -> str:
        return (
            "Read the curated topic-memory surface (read-only). "
            "'list' shows every topic file under memory/ with its frontmatter "
            "title/description/updated before any body. 'read' returns one topic "
            "file's frontmatter plus its full body. 'search' finds exact literal "
            "text in topic files and in memory/history.jsonl summaries and messages "
            "(summaries take precedence over raw messages). Topic memory is curated; "
            "memory/history.jsonl is append-only evidence and is only searched here, "
            "never changed. memory/system/ files are always loaded into context and "
            "are excluded from this tool. Never writes anything."
        )

    @property
    def read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_ACTIONS),
                    "description": (
                        "What to do: 'list' all topic files, 'read' one file, "
                        "or 'search' literal text"
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Topic file path relative to memory/, e.g. 'homelab.md' "
                        "or 'projects/foo.md' (the workspace-relative "
                        "'memory/homelab.md' alias also works). Required for action=read."
                    ),
                    "minLength": 1,
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Literal text to search for (case-insensitive unless "
                        "case_sensitive is true). Required for action=search."
                    ),
                    "minLength": 1,
                },
                "search_memory_files": {
                    "type": "boolean",
                    "description": "Also search topic memory files under memory/ (default true)",
                },
                "search_history": {
                    "type": "boolean",
                    "description": (
                        "Also search memory/history.jsonl summaries and messages "
                        "(default true)"
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
            },
            "required": ["action"],
        }

    def __init__(self, workspace: Path | None = None):
        self._workspace = workspace
        self._search_tool: MemorySearchTool | None = None

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(workspace=Path(ctx.workspace))

    def _workspace_path(self) -> Path:
        return self._workspace or Path(".")

    # ------------------------------------------------------------------
    # list
    # ------------------------------------------------------------------

    def _list_files(
        self, workspace: Path, rels: list[str]
    ) -> tuple[list[dict[str, Any]], int]:
        """Return ``(file_entries, skipped)`` from the discovered topic files."""
        entries: list[dict[str, Any]] = []
        skipped = 0
        for rel in rels[: _MAX_MEMORY_FILES]:
            path = workspace / rel
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
            meta, _body = MemoryStore._split_frontmatter(content)
            entries.append({
                "path": rel,
                "title": (meta.get("title") or Path(rel).stem).strip(),
                "description": MemoryStore._description_from_markdown(content),
                "updated": meta.get("updated"),
                "tags": parse_tags(meta.get("tags")),
                "bytes": len(raw),
                "lines": content.count("\n") + 1,
            })
        return entries, skipped

    @staticmethod
    def _format_list(entries: list[dict[str, Any]]) -> str:
        if not entries:
            return "no topic memory files under memory/"
        lines = [f"{len(entries)} topic memory file(s) under memory/:"]
        for entry in entries:
            head = f"- {entry['path']} — {entry['title']}"
            if entry.get("updated"):
                head += f" (updated {entry['updated']})"
            lines.append(head)
            lines.append(f"    {entry['description']}")
            if entry.get("tags"):
                lines.append("    tags: " + ", ".join(entry["tags"]))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # read
    # ------------------------------------------------------------------

    def _read_file(self, workspace: Path, path: str) -> dict[str, Any]:
        rel = topic_rel_path(path)  # raises MemoryPathError
        resolved = resolve_topic_path(workspace, path)
        if not resolved.exists():
            raise FileNotFoundError(
                f"Error: no such topic memory file memory/{rel} "
                "(run memory_read action=list to see available files)"
            )
        if not topic_file_exists(workspace, rel):
            raise MemoryPathError(
                f"Error: {rel!r} is not a topic memory file "
                "(system files, MEMORY.md, history.jsonl and cursor files are excluded)"
            )
        raw = resolved.read_bytes()
        if len(raw) > _MAX_MEMORY_FILE_BYTES:
            raise ValueError(
                f"Error: topic memory file memory/{rel} is too large to read "
                f"({len(raw)} bytes)"
            )
        content = raw.decode("utf-8")
        meta, body = MemoryStore._split_frontmatter(content)
        return {
            "action": "read",
            "path": "memory/" + str(rel),
            "title": (meta.get("title") or rel.stem).strip(),
            "description": meta.get("description", ""),
            "updated": meta.get("updated"),
            "tags": parse_tags(meta.get("tags")),
            "bytes": len(raw),
            "body": body,
        }

    @staticmethod
    def _format_read(entry: dict[str, Any]) -> str:
        head = [
            f"memory file: {entry['path']}",
            f"title: {entry['title']}",
        ]
        if entry.get("description"):
            head.append(f"description: {entry['description']}")
        if entry.get("updated"):
            head.append(f"updated: {entry['updated']}")
        if entry.get("tags"):
            head.append("tags: " + ", ".join(entry["tags"]))
        head.append("---")
        head.append("body:")
        return "\n".join(head) + "\n" + entry["body"]

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------

    def _search(
        self,
        workspace: Path,
        query: str,
        *,
        search_memory_files: bool,
        search_history: bool,
        case_sensitive: bool,
        max_results: int,
        max_excerpt_chars: int,
    ) -> dict[str, Any]:
        """Reuse memory_search's matching helpers for both sources."""
        tool = self._search_tool or MemorySearchTool(workspace=workspace)
        self._search_tool = tool

        history_matches: list[dict[str, Any]] = []
        if search_history:
            for entry, line_no in iter_history_entries(tool._history_path()):
                history_matches.extend(
                    tool._match_entry(
                        entry,
                        line_no,
                        query,
                        role=None,
                        case_sensitive=case_sensitive,
                        max_excerpt_chars=max_excerpt_chars,
                    )
                )
            history_matches.sort(key=lambda m: m.get("timestamp") or "", reverse=True)

        memory_results: list[dict[str, Any]] = []
        skipped_memory = 0
        if search_memory_files:
            memory_results, skipped_memory = tool._search_memory_files(
                query,
                case_sensitive=case_sensitive,
                max_excerpt_chars=max_excerpt_chars,
            )

        total = len(history_matches)
        return {
            "action": "search",
            "query": query,
            "case_sensitive": case_sensitive,
            "history": history_matches[:max_results],
            "history_total_matches": total,
            "history_truncated": total > len(history_matches[:max_results]),
            "memory_files": memory_results,
            "skipped_memory_files": skipped_memory,
        }

    @staticmethod
    def _format_search(data: dict[str, Any]) -> str:
        if not data["history"]:
            history_text = "no history matches"
        else:
            lines: list[str] = []
            for idx, match in enumerate(data["history"], start=1):
                bits = [match["cursor_id"], match.get("timestamp") or "?"]
                if match.get("session_key"):
                    bits.append(f"session={match['session_key']}")
                if match.get("role"):
                    bits.append(f"role={match['role']}")
                bits.append(match["kind"])
                lines.append(f"[{idx}] {' | '.join(bits)}")
                lines.append(f"    {match['excerpt']}")
            history_text = "\n".join(lines)
        if not data["memory_files"]:
            memory_text = "no memory file matches"
        else:
            lines: list[str] = []
            for file_result in data["memory_files"]:
                lines.append(file_result["path"] + ":")
                for match in file_result["matches"]:
                    lines.append(f"  {match['line']}: {match['text']}")
            memory_text = "\n".join(lines)
        return (
            f'query: "{data["query"]}" — {data["history_total_matches"]} history '
            f"match(es), {len(data['memory_files'])} memory file(s) with matches\n\n"
            f"history:\n{history_text}\n\nmemory files:\n{memory_text}"
        )

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        action: str = "list",
        path: str | None = None,
        query: str | None = None,
        search_memory_files: bool = True,
        search_history: bool = True,
        case_sensitive: bool = False,
        max_results: int | None = None,
        max_excerpt_chars: int | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        if action not in _ACTIONS:
            return ToolResult.retryable_error(
                f"Error: action must be one of {', '.join(_ACTIONS)}, got {action!r}"
            )
        workspace = self._workspace_path()

        if action == "list":
            rels = MemoryStore._topic_files(workspace)
            entries, skipped = self._list_files(workspace, rels)
            data: dict[str, Any] = {
                "action": "list",
                "files": entries,
                "truncated": len(entries) > 0 and len(rels) > len(entries),
                "skipped": skipped,
            }
            text = self._format_list(entries)
            if data["truncated"]:
                text += f"\n(showing first {len(entries)} files; {_MAX_MEMORY_FILES} max)"
            if skipped:
                text += f"\n(skipped {skipped} unreadable/oversized files)"
            return ToolResult(text, data=data)

        if action == "read":
            if not path:
                return ToolResult.retryable_error(
                    "Error: action=read requires 'path' (a topic file under memory/)"
                )
            try:
                entry = self._read_file(workspace, path)
            except MemoryPathError as e:
                return ToolResult.policy_block(
                    str(e),
                    data={"action": "read", "path": path, "allowed": "memory/"},
                    evidence=[{"kind": "memory_boundary", "allowed_root": "memory/"}],
                )
            except FileNotFoundError as e:
                return ToolResult.retryable_error(str(e))
            except ValueError as e:
                return ToolResult.retryable_error(str(e))
            text = self._format_read(entry)
            if len(text) > _MAX_OUTPUT_CHARS:
                text = text[: _MAX_OUTPUT_CHARS] + "\n(output truncated due to size)"
            return ToolResult(text, data=entry)

        # action == "search"
        if not query or not query.strip():
            return ToolResult.retryable_error("Error: action=search requires a non-empty 'query'")
        if not search_memory_files and not search_history:
            return ToolResult.retryable_error(
                "Error: nothing to search — set search_memory_files and/or search_history"
            )
        limit = _DEFAULT_MAX_RESULTS if max_results is None else max_results
        excerpt_chars = _DEFAULT_MAX_EXCERPT_CHARS if max_excerpt_chars is None else max_excerpt_chars
        try:
            data = self._search(
                workspace,
                query.strip(),
                search_memory_files=search_memory_files,
                search_history=search_history,
                case_sensitive=case_sensitive,
                max_results=limit,
                max_excerpt_chars=excerpt_chars,
            )
        except MemoryPathError as e:
            return ToolResult.policy_block(str(e), data={"action": "search"})
        text = self._format_search(data)
        notes: list[str] = []
        if data["history_truncated"]:
            notes.append(
                f"(showing {len(data['history'])} of {data['history_total_matches']} "
                "history matches; raise max_results for more)"
            )
        if data["skipped_memory_files"]:
            notes.append(
                f"(skipped {data['skipped_memory_files']} unreadable/oversized/extra memory files)"
            )
        if notes:
            text += "\n\n" + "\n".join(notes)
        if len(text) > _MAX_OUTPUT_CHARS:
            text = text[: _MAX_OUTPUT_CHARS] + "\n(output truncated due to size)"
        return ToolResult(text, data=data)
