"""Built-in memory_write tool: create or update curated topic memory files.

Topic memory files under ``memory/`` (``memory/*.md``) are the curated,
agent-editable surface. ``memory/history.jsonl`` is append-only evidence and is
never touched here; neither are ``.dream_cursor`` / ``.cursor``, session files,
or ``memory/system/*``. Writes are atomic (temp file + fsync + rename) and the
topic path is strictly contained inside the real ``memory/`` directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobot.agent.memory import MemoryStore
from nanobot.agent.tools._memory_common import (
    MAX_BODY_BYTES,
    MAX_DESCRIPTION_CHARS,
    MAX_TAGS,
    MAX_TITLE_CHARS,
    MemoryPathError,
    atomic_write_text,
    format_frontmatter,
    now_updated,
    parse_tags,
    resolve_topic_path,
    topic_rel_path,
)
from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.schema import ArraySchema, ObjectSchema, StringSchema


@tool_parameters(
    ObjectSchema(
        properties={
            "path": StringSchema(
                "Topic file path relative to memory/, e.g. 'homelab.md' or "
                "'projects/foo.md' (the workspace-relative 'memory/homelab.md' alias "
                "works too). Must end in .md and stay inside memory/.",
                min_length=1,
            ),
            "title": StringSchema(
                "Frontmatter title. When omitted on update the existing title is kept; "
                "on create it derives from the file name.",
                max_length=MAX_TITLE_CHARS,
            ),
            "description": StringSchema(
                "One-line frontmatter description (shown by memory_read action=list). "
                "When omitted on update the existing description is kept; on create it "
                "derives from the first body line.",
                max_length=MAX_DESCRIPTION_CHARS,
            ),
            "tags": ArraySchema(
                items=StringSchema("tag"),
                description=(
                    "Optional frontmatter tags. When omitted on update the existing "
                    "tags are kept."
                ),
                max_items=MAX_TAGS,
            ),
            "body": StringSchema(
                "Full markdown body to store. Required and must be non-empty.",
                min_length=1,
                max_length=MAX_BODY_BYTES,
            ),
        },
        required=["path", "body"],
        description=(
            "Create or update one curated topic memory file under memory/. "
            "Topic memory is curated, editable knowledge; memory/history.jsonl "
            "is append-only evidence and is never written by this tool. "
            "memory/system/*, MEMORY.md, cursor files and session files are off-limits."
        ),
        additional_properties=False,
    ).to_json_schema()
)
class MemoryWriteTool(Tool):
    """Curate topic memory files under memory/ (atomic writes only)."""

    _scopes = {"core", "subagent", "memory"}

    @property
    def name(self) -> str:
        return "memory_write"

    @property
    def description(self) -> str:
        return (
            "Create or update a curated topic memory file under memory/ "
            "(memory/*.md) with YAML-ish frontmatter (title, description, updated "
            "timestamp, optional tags). Topic memory is curated, editable knowledge "
            "that persists across sessions. memory/history.jsonl is append-only "
            "evidence and is NEVER touched by this tool; neither are .dream_cursor, "
            ".cursor, MEMORY.md, memory/system/*, or session files. The topic path "
            "must stay inside the real memory/ directory. Writes are atomic "
            "(temp file + fsync + rename), so concurrent reads never see a partial file."
        )

    @property
    def read_only(self) -> bool:
        return False

    def __init__(self, workspace: Path | None = None):
        self._workspace = workspace

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(workspace=Path(ctx.workspace))

    def _workspace_path(self) -> Path:
        return self._workspace or Path(".")

    @staticmethod
    def _derive_description(body: str) -> str:
        """First non-empty, non-heading body line, truncated like memory_search list."""
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if len(stripped) > MAX_DESCRIPTION_CHARS:
                stripped = stripped[: MAX_DESCRIPTION_CHARS - 1] + "\u2026"
            return stripped
        return ""

    async def execute(
        self,
        path: str,
        body: str,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        if body is None or not body.strip():
            return ToolResult.retryable_error("Error: body must be a non-empty string")
        if tags is not None and not isinstance(tags, list):
            return ToolResult.retryable_error("Error: tags must be a list of strings")

        try:
            rel = topic_rel_path(path)
            resolved = resolve_topic_path(self._workspace_path(), path)
        except MemoryPathError as e:
            return ToolResult.policy_block(
                str(e),
                data={"path": path, "allowed": "memory/"},
                evidence=[{"kind": "memory_boundary", "allowed_root": "memory/"}],
            )

        created = not resolved.exists()
        if resolved.exists() and resolved.is_dir():
            return ToolResult.retryable_error(
                f"Error: memory/{rel} is a directory, not a topic memory file"
            )

        existing_meta: dict[str, str] = {}
        if resolved.exists():
            try:
                existing_content = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                return ToolResult.retryable_error(
                    f"Error: cannot read existing memory/{rel}: {e}"
                )
            existing_meta, _ = MemoryStore._split_frontmatter(existing_content)

        new_title = (title or existing_meta.get("title") or rel.stem).strip()
        if len(new_title) > MAX_TITLE_CHARS:
            new_title = new_title[: MAX_TITLE_CHARS - 1] + "\u2026"
        new_description = (
            description.strip()
            if description is not None
            else (existing_meta.get("description") or self._derive_description(body)).strip()
        )
        if len(new_description) > MAX_DESCRIPTION_CHARS:
            new_description = new_description[: MAX_DESCRIPTION_CHARS - 1] + "\u2026"
        new_tags = parse_tags(tags) if tags is not None else parse_tags(existing_meta.get("tags"))

        meta = {
            "title": new_title,
            "description": new_description,
            "updated": now_updated(),
            "tags": new_tags[:MAX_TAGS],
        }
        normalized_body = body.rstrip() + "\n"
        content = format_frontmatter(meta) + "\n" + normalized_body

        try:
            atomic_write_text(resolved, content)
        except OSError as e:
            return ToolResult.retryable_error(f"Error writing memory/{rel}: {e}")

        # Verify the write by reading back (postcondition check).
        try:
            written_back = resolved.read_text(encoding="utf-8")
            postcondition = "checked" if written_back == content else "failed"
        except OSError:
            postcondition = "failed"

        data: dict[str, Any] = {
            "action": "write",
            "path": "memory/" + str(rel),
            "created": created,
            "title": new_title,
            "description": new_description,
            "updated": meta["updated"],
            "tags": new_tags,
            "body_bytes": len(content.encode("utf-8")),
        }
        evidence = [
            {"kind": "memory_file_write", "path": "memory/" + str(rel), "created": created}
        ]
        side_effects = [
            {"kind": "topic_memory_write", "path": "memory/" + str(rel), "created": created}
        ]
        verb = "created" if created else "updated"
        text = (
            f"{verb} topic memory file memory/{rel} — title: {new_title}"
            + (f" — description: {new_description}" if new_description else "")
        )
        return ToolResult(
            text,
            data=data,
            evidence=evidence,
            side_effects=side_effects,
            postcondition=postcondition,
        )
