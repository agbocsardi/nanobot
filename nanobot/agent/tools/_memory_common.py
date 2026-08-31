"""Shared guards and helpers for the memory_read / memory_write tools.

Topic memory files under ``memory/`` are the curated, agent-editable surface.
``memory/history.jsonl`` is append-only evidence (read by search, never written
by these tools), ``memory/.dream_cursor`` / ``memory/.cursor`` are cursor state,
``memory/system/*`` is always loaded into context, and ``memory/MEMORY.md`` is
the always-loaded index. None of those are topic memory files.

Everything here is memory-scoped: paths are resolved against the workspace
``memory/`` directory and must stay inside it (realpath prefix check).
"""

from __future__ import annotations

import os
import secrets
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nanobot.agent.memory import MemoryStore
from nanobot.security.workspace_policy import is_path_within

#: Files under ``memory/`` that these tools must never expose via the ``read``
#: action or write: append-only evidence, cursor state, and the MEMORY.md index.
PROTECTED_MEMORY_FILES = frozenset({
    "history.jsonl",
    "HISTORY.md",
    ".cursor",
    ".dream_cursor",
    "MEMORY.md",
})

SYSTEM_DIR_NAME = "system"

MAX_TITLE_CHARS = 120
MAX_DESCRIPTION_CHARS = 240  # mirrors MemoryStore._DESCRIPTION_MAX_CHARS
MAX_TAGS = 20
MAX_BODY_BYTES = 1_000_000  # sanity cap on a single topic file body


class MemoryPathError(ValueError):
    """A requested topic path is outside memory/ or names a protected file."""


def memory_dir(workspace: Path) -> Path:
    """Realpath of the workspace memory directory (may not exist yet)."""
    return (workspace / "memory").resolve()


def topic_rel_path(rel: str) -> Path:
    """Normalize and validate a *memory-relative* topic path (no filesystem touch).

    Accepts ``homelab.md`` as well as the workspace-relative alias
    ``memory/homelab.md``. Rejects absolute paths, ``..`` escapes, hidden
    segments, ``memory/system/*``, and the protected files in
    :data:`PROTECTED_MEMORY_FILES`. Raises :class:`MemoryPathError` on any
    violation.
    """
    if not isinstance(rel, str) or not rel.strip():
        raise MemoryPathError("Error: topic path must be a non-empty string")
    normalized = rel.strip().replace("\\", "/")
    segments = normalized.split("/")
    # Check the raw segments first: pathlib would silently normalize "a/./b".
    if any(segment in (".", "..") for segment in segments):
        raise MemoryPathError(
            f"Error: topic path must not contain '.' or '..' segments: {rel!r}"
        )
    candidate = Path(normalized)
    if candidate.is_absolute():
        raise MemoryPathError(f"Error: topic path must be memory-relative, got absolute {rel!r}")
    parts = [segment for segment in segments if segment]
    # Workspace-relative alias: "memory/x.md" == memory-relative "x.md".
    if parts and parts[0] == "memory":
        parts = parts[1:]
    if not parts:
        raise MemoryPathError(f"Error: empty topic path: {rel!r}")
    if any(part.startswith(".") for part in parts[:-1]):
        raise MemoryPathError(f"Error: hidden directories are not topic memory: {rel!r}")
    if parts[0] == SYSTEM_DIR_NAME:
        raise MemoryPathError(
            f"Error: memory/{SYSTEM_DIR_NAME}/ is not a topic memory location: {rel!r}"
        )
    first = str(Path(*parts))
    if first in PROTECTED_MEMORY_FILES:
        raise MemoryPathError(f"Error: {first!r} is protected and not a topic memory file")
    if Path(first).suffix.lower() != ".md":
        raise MemoryPathError(f"Error: topic memory must be a .md file: {rel!r}")
    return Path(*parts)


def resolve_topic_path(workspace: Path, rel: str) -> Path:
    """Resolve ``rel`` against the real memory dir and enforce containment.

    The result is the ``strict=False`` realpath of ``memory/<rel>``; the final
    path (following symlinks) must still live inside the real memory dir.
    """
    mem_dir = memory_dir(workspace)
    resolved = (mem_dir / topic_rel_path(rel)).resolve(strict=False)
    if not is_path_within(resolved, mem_dir):
        raise MemoryPathError(
            f"Error: topic path {rel!r} resolves outside the memory directory"
        )
    return resolved


def parse_tags(value: Any) -> list[str]:
    """Parse frontmatter ``tags`` (list, ``[a, b]`` string, or comma text)."""
    if isinstance(value, list):
        return [str(tag).strip() for tag in value if str(tag).strip()]
    if not isinstance(value, str):
        return []
    raw = value.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [tag.strip().strip("\"'") for tag in raw.split(",") if tag.strip()]


def _yaml_scalar(value: str) -> str:
    """Single-line YAML-ish scalar; quotes when punctuation could confuse the parser."""
    value = " ".join(str(value).split())
    tricky = any(ch in value for ch in ':#"{}[],&*!|>\'%@`')
    if tricky or value.startswith(("-", "?", " ")):
        return '"' + value.replace('"', "'") + '"'
    return value


def format_frontmatter(meta: dict[str, Any]) -> str:
    """Serialize the topic-file frontmatter block (title/description/updated/tags)."""
    lines = ["---"]
    if meta.get("title"):
        lines.append(f"title: {_yaml_scalar(meta['title'])}")
    if meta.get("description"):
        lines.append(f"description: {_yaml_scalar(meta['description'])}")
    if meta.get("updated"):
        lines.append(f"updated: {meta['updated']}")
    tags = meta.get("tags") or []
    if tags:
        lines.append("tags: [" + ", ".join(_yaml_scalar(str(tag)) for tag in tags) + "]")
    lines.append("---")
    return "\n".join(lines) + "\n"


def now_updated() -> str:
    """UTC timestamp for the frontmatter ``updated`` field."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def atomic_write_text(path: Path, text: str) -> None:
    """Write *text* to *path* atomically: temp file + fsync + rename + dir fsync.

    The temp file lives in the target directory so ``os.replace`` stays on one
    filesystem; it is removed on any failure. Mirrors
    :meth:`MemoryStore._write_entries` (which owns history.jsonl; this helper
    is for topic files only).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        # fsync the directory so the rename is durable (skip on Windows).
        with suppress(PermissionError):
            fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def topic_file_exists(workspace: Path, rel: Path) -> bool:
    """True when ``memory/<rel>`` is one of the discovered topic files."""
    full = "memory/" + str(rel)
    return full in set(MemoryStore._topic_files(workspace))
