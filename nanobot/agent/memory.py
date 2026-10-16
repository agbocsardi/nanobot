"""Memory system: pure file I/O store and lightweight Consolidator."""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
import threading
import time
import weakref
from collections.abc import Awaitable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator

import tiktoken
from loguru import logger

from nanobot.session.manager import FILE_MAX_MESSAGES, Session, sanitize_message_for_persistence
from nanobot.utils.gitstore import GitStore
from nanobot.utils.helpers import (
    ensure_dir,
    estimate_prompt_tokens_chain,
    load_bundled_template,
    strip_think,
    truncate_text,
)
from nanobot.utils.prompt_templates import render_template
from nanobot.utils.usage import usage_delta, usage_snapshot

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import SessionManager


# ---------------------------------------------------------------------------
# Dream guardrail state
# ---------------------------------------------------------------------------


@dataclass
class DreamSnapshot:
    """Pre-Dream working-tree state of the memory surfaces.

    ``files`` maps workspace-relative paths to the exact bytes present before
    the run. It covers the M1b memory surfaces (SOUL.md, USER.md,
    ``memory/**/*.md``, ``skills/**/SKILL.md``) plus git-tracked files such as
    ``memory/.dream_cursor`` so an incomplete run restores the pre-run state
    byte-for-byte. Keeping bytes (rather than trusting git HEAD) preserves any
    pre-existing dirty edits a user made outside Dream.
    """

    files: dict[str, bytes]
    surface: list[str]


@dataclass
class DreamRunResult:
    """Structured outcome of one guarded Dream pass (M1b/M1c observability)."""

    completed: bool
    reason: str  # completed | nothing_to_process | timeout | exception | stop reason | max_changed_files | max_diff_chars
    stop_reason: str | None = None
    changed_files: list[str] = field(default_factory=list)
    diff_chars: int = 0
    diff: str = ""
    commit_sha: str | None = None
    elapsed_s: float = 0.0
    error: str | None = None


# ---------------------------------------------------------------------------
# MemoryStore — pure file I/O layer
# ---------------------------------------------------------------------------

class MemoryStore:
    """Pure file I/O for memory files: MEMORY.md, history.jsonl, SOUL.md, USER.md."""

    _DEFAULT_MAX_HISTORY = 1000
    _INTERNAL_HISTORY_SESSION_PREFIXES = ("cron:", "dream:")
    _INTERNAL_HISTORY_SESSION_KEYS = {"heartbeat"}
    _LEGACY_ENTRY_START_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}[^\]]*)\]\s*")
    _LEGACY_TIMESTAMP_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s*")
    _LEGACY_RAW_MESSAGE_RE = re.compile(
        r"^\[\d{4}-\d{2}-\d{2}[^\]]*\]\s+[A-Z][A-Z0-9_]*(?:\s+\[tools:\s*[^\]]+\])?:"
    )
    _SYSTEM_FILES = [
        "memory/system/procedures.md",
        "memory/system/corrections.md",
        "memory/system/now.md",
    ]
    _MEMORY_TREE_MAX_FILES = 80
    _DESCRIPTION_MAX_CHARS = 240

    @staticmethod
    def _topic_files(workspace: Path) -> list[str]:
        """Discover topic files in memory/ dynamically (excludes MEMORY.md and system/)."""
        memory_dir = workspace / "memory"
        return [
            str(f.relative_to(workspace))
            for f in sorted(memory_dir.rglob("*.md"))
            if f.name != "MEMORY.md" and "system" not in f.relative_to(memory_dir).parts
        ]

    def __init__(self, workspace: Path, max_history_entries: int = _DEFAULT_MAX_HISTORY):
        self.workspace = workspace
        self.max_history_entries = max_history_entries
        self.memory_dir = ensure_dir(workspace / "memory")
        self.system_dir = ensure_dir(self.memory_dir / "system")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "history.jsonl"
        self.legacy_history_file = self.memory_dir / "HISTORY.md"
        self.soul_file = workspace / "SOUL.md"
        self.user_file = workspace / "USER.md"
        self._cursor_file = self.memory_dir / ".cursor"
        self._dream_cursor_file = self.memory_dir / ".dream_cursor"
        self._corruption_logged = False  # rate-limit invalid cursor warning
        self._malformed_entry_logged = False  # rate-limit bad history shape warning
        self._oversize_logged = False  # rate-limit oversized-entry warning
        self._append_lock = threading.Lock()  # serialize cursor allocation + append
        self._git = GitStore(
            workspace,
            tracked_files=[
                "SOUL.md", "USER.md", "memory/MEMORY.md", "memory/.dream_cursor",
                *self._SYSTEM_FILES,
            ],
            tracked_globs=["memory/**/*.md"],
        )
        if self._git.is_initialized():
            # Existing repos were bootstrapped without the topic-file glob;
            # refresh the whitelist so agent-created files get versioned.
            self._git.ensure_gitignore()
        self._maybe_migrate_legacy_history()

    @property
    def git(self) -> GitStore:
        return self._git

    # -- generic helpers -----------------------------------------------------

    @staticmethod
    def read_file(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    @staticmethod
    def _split_frontmatter(content: str) -> tuple[dict[str, str], str]:
        """Return ``(frontmatter, body)`` for simple YAML-ish markdown metadata.

        We only need Letta-style ``description: ...`` / ``limit: ...`` parsing,
        so keep this dependency-free and intentionally conservative.
        """
        if not content.startswith("---\n"):
            return {}, content
        end = content.find("\n---", 4)
        if end == -1:
            return {}, content
        close_end = end + len("\n---")
        if close_end < len(content) and content[close_end] == "\n":
            close_end += 1
        raw = content[4:end]
        meta: dict[str, str] = {}
        for line in raw.splitlines():
            if ":" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split(":", 1)
            key = key.strip().lower()
            value = value.strip().strip('"\'')
            if key:
                meta[key] = value
        return meta, content[close_end:]

    @classmethod
    def _body_without_frontmatter(cls, content: str) -> str:
        return cls._split_frontmatter(content)[1]

    @classmethod
    def _description_from_markdown(cls, content: str) -> str:
        meta, body = cls._split_frontmatter(content)
        description = meta.get("description", "").strip()
        if description:
            return truncate_text(description, cls._DESCRIPTION_MAX_CHARS)
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return truncate_text(stripped.lstrip("#").strip(), cls._DESCRIPTION_MAX_CHARS)
        return "(missing description)"

    def _iter_memory_markdown_files(self) -> list[Path]:
        """Return memory markdown files visible in the memory tree."""
        files = [
            f for f in sorted(self.memory_dir.rglob("*.md"))
            if f.name != "MEMORY.md"
        ]
        legacy = self.memory_file
        if legacy.exists() and self._body_without_frontmatter(self.read_file(legacy)).strip():
            files.insert(0, legacy)
        return files

    def _maybe_migrate_legacy_history(self) -> None:
        """One-time upgrade from legacy HISTORY.md to history.jsonl.

        The migration is best-effort and prioritizes preserving as much content
        as possible over perfect parsing.
        """
        if not self.legacy_history_file.exists():
            return
        if self.history_file.exists() and self.history_file.stat().st_size > 0:
            return

        try:
            legacy_text = self.legacy_history_file.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            logger.exception("Failed to read legacy HISTORY.md for migration")
            return

        entries = self._parse_legacy_history(legacy_text)
        try:
            if entries:
                self._write_entries(entries)
                last_cursor = entries[-1]["cursor"]
                self._cursor_file.write_text(str(last_cursor), encoding="utf-8")
                # Default to "already processed" so upgrades do not replay the
                # user's entire historical archive into Dream on first start.
                self._dream_cursor_file.write_text(str(last_cursor), encoding="utf-8")

            backup_path = self._next_legacy_backup_path()
            self.legacy_history_file.replace(backup_path)
            logger.info(
                "Migrated legacy HISTORY.md to history.jsonl ({} entries)",
                len(entries),
            )
        except Exception:
            logger.exception("Failed to migrate legacy HISTORY.md")

    def _parse_legacy_history(self, text: str) -> list[dict[str, Any]]:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            return []

        fallback_timestamp = self._legacy_fallback_timestamp()
        entries: list[dict[str, Any]] = []
        chunks = self._split_legacy_history_chunks(normalized)

        for cursor, chunk in enumerate(chunks, start=1):
            timestamp = fallback_timestamp
            content = chunk
            match = self._LEGACY_TIMESTAMP_RE.match(chunk)
            if match:
                timestamp = match.group(1)
                remainder = chunk[match.end():].lstrip()
                if remainder:
                    content = remainder

            entries.append({
                "cursor": cursor,
                "timestamp": timestamp,
                "content": content,
            })
        return entries

    def _split_legacy_history_chunks(self, text: str) -> list[str]:
        lines = text.split("\n")
        chunks: list[str] = []
        current: list[str] = []
        saw_blank_separator = False

        for line in lines:
            if saw_blank_separator and line.strip() and current:
                chunks.append("\n".join(current).strip())
                current = [line]
                saw_blank_separator = False
                continue
            if self._should_start_new_legacy_chunk(line, current):
                chunks.append("\n".join(current).strip())
                current = [line]
                saw_blank_separator = False
                continue
            current.append(line)
            saw_blank_separator = not line.strip()

        if current:
            chunks.append("\n".join(current).strip())
        return [chunk for chunk in chunks if chunk]

    def _should_start_new_legacy_chunk(self, line: str, current: list[str]) -> bool:
        if not current:
            return False
        if not self._LEGACY_ENTRY_START_RE.match(line):
            return False
        if self._is_raw_legacy_chunk(current) and self._LEGACY_RAW_MESSAGE_RE.match(line):
            return False
        return True

    def _is_raw_legacy_chunk(self, lines: list[str]) -> bool:
        first_nonempty = next((line for line in lines if line.strip()), "")
        match = self._LEGACY_TIMESTAMP_RE.match(first_nonempty)
        if not match:
            return False
        return first_nonempty[match.end():].lstrip().startswith("[RAW]")

    def _legacy_fallback_timestamp(self) -> str:
        try:
            return datetime.fromtimestamp(
                self.legacy_history_file.stat().st_mtime,
            ).strftime("%Y-%m-%d %H:%M")
        except OSError:
            return datetime.now().strftime("%Y-%m-%d %H:%M")

    def _next_legacy_backup_path(self) -> Path:
        candidate = self.memory_dir / "HISTORY.md.bak"
        suffix = 2
        while candidate.exists():
            candidate = self.memory_dir / f"HISTORY.md.bak.{suffix}"
            suffix += 1
        return candidate

    # -- MEMORY.md (long-term facts) -----------------------------------------

    def read_memory(self) -> str:
        return self.read_file(self.memory_file)

    def write_memory(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    # -- SOUL.md -------------------------------------------------------------

    def read_soul(self) -> str:
        return self.read_file(self.soul_file)

    def write_soul(self, content: str) -> None:
        self.soul_file.write_text(content, encoding="utf-8")

    # -- USER.md -------------------------------------------------------------

    def read_user(self) -> str:
        return self.read_file(self.user_file)

    def write_user(self, content: str) -> None:
        self.user_file.write_text(content, encoding="utf-8")

    # -- context injection (used by context.py) ------------------------------

    def get_memory_context(self) -> str:
        """Return always-loaded memory context.

        Load system files alongside any legacy MEMORY.md material not yet
        copied into them. Migration is explicit: readers never rewrite or
        delete either source, and identical paragraphs are loaded only once.
        """
        parts: list[str] = []
        system_paragraphs: set[str] = set()
        for md_file in sorted(self.system_dir.glob("*.md")):
            raw = md_file.read_text(encoding="utf-8")
            content = self._body_without_frontmatter(raw).strip()
            if content:
                system_paragraphs.update(part.strip() for part in content.split("\n\n"))
                rel_path = str(md_file.relative_to(self.workspace))
                parts.append(f"## {rel_path}\n\n{content}")
        raw_memory = self.read_memory()
        template = load_bundled_template("memory/MEMORY.md")
        long_term = (
            ""
            if template is not None and raw_memory.strip() == template.strip()
            else self._body_without_frontmatter(raw_memory).strip()
        )
        legacy = "\n\n".join(
            part for part in long_term.split("\n\n")
            if part.strip() and part.strip() not in system_paragraphs
        )
        if legacy:
            parts.append(f"## Long-term Memory\n{legacy}")
        return "# System Memory\n\n" + "\n\n".join(parts) if parts else ""

    def get_memory_tree_context(self) -> str:
        """Return a compact index of memory files and their descriptions.

        The tree makes topic files discoverable without loading their full
        contents. ``memory/system/`` files are marked as already loaded.
        """
        entries: list[str] = []
        files = self._iter_memory_markdown_files()
        for md_file in files[: self._MEMORY_TREE_MAX_FILES]:
            raw = md_file.read_text(encoding="utf-8")
            body = self._body_without_frontmatter(raw).strip()
            description = self._description_from_markdown(raw)
            rel_path = str(md_file.relative_to(self.workspace))
            loaded = (
                "loaded" if md_file.is_relative_to(self.system_dir) or md_file == self.memory_file
                else "topic"
            )
            entries.append(
                f"- `{rel_path}` ({loaded}, {len(body)} chars): {description}"
            )
        if len(files) > self._MEMORY_TREE_MAX_FILES:
            entries.append(f"- … {len(files) - self._MEMORY_TREE_MAX_FILES} more memory files")
        if not entries:
            return ""
        return "\n".join([
            "# Memory Tree",
            "Files marked loaded are already included in context. Other files are topic memories; use their descriptions to decide what may be relevant.",
            *entries,
        ])

    # -- history.jsonl — append-only, JSONL format ---------------------------

    def append_history(
        self,
        entry: str,
        *,
        max_chars: int | None = None,
        session_key: str | None = None,
    ) -> int:
        """Append *entry* to history.jsonl and return its auto-incrementing cursor.

        Entries are passed through `strip_think` to drop template-level leaks
        (e.g. unclosed `<think` prefixes, `<channel|>` markers) before being
        persisted. If the cleaned content is empty but the raw entry wasn't,
        the record is persisted with an empty string rather than falling back
        to the raw leak — otherwise `strip_think`'s guarantees would be
        undone by history replay / consolidation downstream.

        A defensive cap (*max_chars*, default ``_HISTORY_ENTRY_HARD_CAP``) is
        applied as a final safety net: individual callers should cap their own
        content more tightly; this default only exists to catch unintentional
        large writes (e.g. an LLM echoing its input back as a "summary").
        """
        limit = max_chars if max_chars is not None else _HISTORY_ENTRY_HARD_CAP
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        raw = entry.rstrip()
        if len(raw) > limit:
            if not self._oversize_logged:
                self._oversize_logged = True
                logger.warning(
                    "history entry exceeds {} chars ({}); truncating. "
                    "Usually means a caller forgot its own cap; "
                    "further occurrences suppressed.",
                    limit, len(raw),
                )
            raw = truncate_text(raw, limit)
        content = strip_think(raw)
        record = {"timestamp": ts, "content": content}
        if session_key:
            record["session_key"] = session_key
        cursor = self._append_history_record(record)
        if raw and not content:
            logger.debug(
                "history entry {} stripped to empty (likely template leak); "
                "persisting empty content to avoid re-polluting context",
                cursor,
            )
        return cursor

    def append_conversation_history(
        self,
        messages: list[dict[str, Any]],
        *,
        session_key: str | None = None,
        content: str | None = None,
        max_chars: int | None = None,
        usage: dict[str, Any] | None = None,
        summary: str | None = None,
    ) -> int:
        """Append a structured conversation archive entry to history.jsonl.

        New conversation records are schema v3: structured messages plus an
        optional LLM summary. The old ``content`` text projection is no longer
        written for conversations; readers use history_entry_text() to support
        old v1/v2 records.
        """
        _ = (content, max_chars)  # accepted for old call sites; v3 does not persist content
        now = datetime.now().astimezone().isoformat(timespec="minutes")
        cleaned_messages = self._clean_history_messages(messages)
        timestamps = [m.get("timestamp") for m in cleaned_messages if m.get("timestamp")]
        record: dict[str, Any] = {
            "schema_version": 3,
            "kind": "conversation",
            "timestamp": now,
            "start_time": min(timestamps) if timestamps else None,
            "end_time": max(timestamps) if timestamps else None,
            "session_key": session_key or "unknown",
            "message_count": len(cleaned_messages),
            "messages": cleaned_messages,
        }
        if summary and summary.strip():
            record["summary"] = truncate_text(strip_think(summary.strip()), _ARCHIVE_SUMMARY_MAX_CHARS)
        if usage:
            record["usage"] = usage
        return self._append_history_record(record)

    def _append_history_record(self, record: dict[str, Any]) -> int:
        # Cursor allocation and the append must be atomic: concurrent writers
        # could otherwise read the same current cursor and emit duplicates.
        with self._append_lock:
            cursor = self._next_cursor()
            record["cursor"] = cursor
            with open(self.history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._cursor_file.write_text(str(cursor), encoding="utf-8")
        return cursor

    @staticmethod
    def _valid_cursor(value: Any) -> int | None:
        """Non-negative int cursors only; reject bool (``isinstance(True, int)`` is True)."""
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    def _iter_valid_entries(self) -> Iterator[tuple[dict[str, Any], int]]:
        """Yield ``(entry, cursor)`` for well-formed entries; warn once on corruption."""
        poisoned: Any = None
        malformed_cursor: int | None = None
        for entry in self._read_entries():
            raw = entry.get("cursor")
            if raw is None:
                continue
            cursor = self._valid_cursor(raw)
            if cursor is None:
                poisoned = raw
                continue
            if not self._valid_history_payload(entry):
                malformed_cursor = cursor
                continue
            yield entry, cursor
        if poisoned is not None and not self._corruption_logged:
            self._corruption_logged = True
            logger.warning(
                "history.jsonl contains an invalid cursor ({!r}); dropping it. "
                "Usually caused by an external writer; further occurrences suppressed.",
                poisoned,
            )
        if malformed_cursor is not None and not self._malformed_entry_logged:
            self._malformed_entry_logged = True
            logger.warning(
                "history.jsonl contains a malformed entry at cursor {}; dropping it. "
                "Usually caused by an external writer; further occurrences suppressed.",
                malformed_cursor,
            )

    @staticmethod
    def _valid_history_payload(entry: dict[str, Any]) -> bool:
        if not isinstance(entry.get("timestamp"), str):
            return False
        has_content = isinstance(entry.get("content"), str)
        has_summary = isinstance(entry.get("summary"), str)
        has_messages = isinstance(entry.get("messages"), list)
        if not (has_content or has_summary or has_messages):
            return False
        session_key = entry.get("session_key")
        return session_key is None or isinstance(session_key, str)

    def _read_cursor_counter(self) -> int | None:
        """Return the persisted cursor counter when it is usable."""
        if not self._cursor_file.exists():
            return None
        with suppress(ValueError, OSError):
            cursor = int(self._cursor_file.read_text(encoding="utf-8").strip())
            if cursor >= 0:
                return cursor
        return None

    def _next_cursor(self) -> int:
        """Read the current cursor counter and return the next value."""
        cursor_counter = self._read_cursor_counter()
        last = self._read_last_entry() or {}
        last_cursor = self._valid_cursor(last.get("cursor"))
        if cursor_counter is not None:
            if last_cursor is not None:
                return max(cursor_counter, last_cursor) + 1
            max_history_cursor = max((c for _, c in self._iter_valid_entries()), default=0)
            return max(cursor_counter, max_history_cursor) + 1

        # Fast path: trust the tail when intact.  Otherwise scan the whole
        # file and take ``max`` — that stays correct even if the monotonic
        # invariant was broken by external writes.
        if last_cursor is not None:
            return last_cursor + 1
        return max((c for _, c in self._iter_valid_entries()), default=0) + 1

    def read_unprocessed_history(self, since_cursor: int) -> list[dict[str, Any]]:
        """Return history entries with a valid cursor > *since_cursor*."""
        return [e for e, c in self._iter_valid_entries() if c > since_cursor]

    @classmethod
    def history_entry_text(cls, entry: dict[str, Any]) -> str:
        """Text projection for prompt injection.

        Prefer LLM summaries, then structured messages, then legacy content.
        This lets new archives inject compact summaries without breaking old
        history.jsonl records that only have content.
        """
        summary = entry.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
        messages = entry.get("messages")
        if isinstance(messages, list) and messages:
            return cls._format_messages(messages)
        content = entry.get("content")
        return content if isinstance(content, str) else ""

    @classmethod
    def _is_internal_history_session(cls, session_key: str | None) -> bool:
        if not session_key:
            return False
        return (
            session_key in cls._INTERNAL_HISTORY_SESSION_KEYS
            or session_key.startswith(cls._INTERNAL_HISTORY_SESSION_PREFIXES)
        )

    def read_recent_history_for_prompt(
        self,
        since_cursor: int,
        *,
        session_key: str | None,
        unified_session: bool = False,
    ) -> list[dict[str, Any]]:
        """Return unprocessed history entries safe to inject into a turn prompt."""
        entries = self.read_unprocessed_history(since_cursor=since_cursor)
        if session_key is None:
            return entries
        if not unified_session:
            return [e for e in entries if e.get("session_key") == session_key]

        return [
            entry
            for entry in entries
            if (entry_session := entry.get("session_key")) == session_key
            or not self._is_internal_history_session(entry_session)
        ]

    def unprocessed_session_summaries(
        self,
        since_cursor: int,
        *,
        session_key: str | None,
        unified_session: bool = False,
    ) -> list[str]:
        """Return summaries or bounded fallback excerpts for unprocessed history.

        Reuses the session/dream filtering of ``read_recent_history_for_prompt``.
        A failed summary must not erase archived context from the next turn:
        structured messages provide a small, sanitized user/assistant excerpt
        instead. Legacy text-only entries remain excluded. Returned oldest
        first; callers reverse and cap the combined context for recency.
        """
        entries = self.read_recent_history_for_prompt(
            since_cursor=since_cursor,
            session_key=session_key,
            unified_session=unified_session,
        )
        summaries: list[str] = []
        for entry in entries:
            summary = entry.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                summary = self._fallback_session_summary(entry.get("messages"))
            if summary:
                summaries.append(f'[{entry.get("timestamp") or "?"}] {summary.strip()}')
        return summaries

    @staticmethod
    def _fallback_session_summary(messages: Any) -> str:
        """Keep recent dialogue, not tool payloads or an unbounded raw dump."""
        if not isinstance(messages, list):
            return ""
        excerpts: list[str] = []
        remaining = _FALLBACK_SUMMARY_MAX_CHARS
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
                continue
            cleaned = sanitize_message_for_persistence(message)
            content = strip_think(str(cleaned.get("content") or "")).strip()
            if not content:
                continue
            excerpt = f"{message['role'].upper()}: {truncate_text(content, 500)}"
            if len(excerpt) + 1 > remaining:
                break
            excerpts.append(excerpt)
            remaining -= len(excerpt) + 1
        return "\n".join(reversed(excerpts))

    def compact_history(self) -> None:
        """Drop oldest entries if the file exceeds *max_history_entries*."""
        if self.max_history_entries <= 0:
            return
        entries = self._read_entries()
        if len(entries) <= self.max_history_entries:
            return
        kept = entries[-self.max_history_entries:]
        self._write_entries(kept)

    # -- JSONL helpers -------------------------------------------------------

    def _read_entries(self) -> list[dict[str, Any]]:
        """Read all entries from history.jsonl."""
        entries: list[dict[str, Any]] = []
        with suppress(FileNotFoundError):
            with open(self.history_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue

        return entries

    def _read_last_entry(self) -> dict[str, Any] | None:
        """Read the last entry from the JSONL file efficiently."""
        try:
            with open(self.history_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return None
                read_size = min(size, 4096)
                f.seek(size - read_size)
                data = f.read().decode("utf-8")
                lines = [line for line in data.split("\n") if line.strip()]
                if not lines:
                    return None
                return json.loads(lines[-1])
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _write_entries(self, entries: list[dict[str, Any]]) -> None:
        """Overwrite history.jsonl with the given entries (atomic write)."""
        tmp_path = self.history_file.with_suffix(self.history_file.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.history_file)

            # fsync the directory so the rename is durable.
            # On Windows, opening a directory with O_RDONLY raises
            # PermissionError — skip the dir sync there (NTFS
            # journals metadata synchronously).
            with suppress(PermissionError):
                fd = os.open(str(self.history_file.parent), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    # -- dream cursor --------------------------------------------------------

    def get_last_dream_cursor(self) -> int:
        if self._dream_cursor_file.exists():
            with suppress(ValueError, OSError):
                return int(self._dream_cursor_file.read_text(encoding="utf-8").strip())
        return 0

    def set_last_dream_cursor(self, cursor: int) -> None:
        self._dream_cursor_file.write_text(str(cursor), encoding="utf-8")

    def build_dream_prompt(self, *, max_entries: int = 20) -> tuple[str, int] | None:
        """Build the Dream prompt with unprocessed history context.

        Returns ``(prompt, last_cursor)`` or ``None`` if nothing to process.
        """
        from nanobot.agent.skills import BUILTIN_SKILLS_DIR

        last_cursor = self.get_last_dream_cursor()
        entries = self.read_unprocessed_history(since_cursor=last_cursor)
        if not entries:
            return None

        batch = entries[:max_entries]
        history_text = "\n".join(
            f"[{e['timestamp']}] {truncate_text(self.history_entry_text(e), 500)}"
            for e in batch
        )
        skill_creator_path = str(BUILTIN_SKILLS_DIR / "skill-creator" / "SKILL.md")
        template = render_template(
            "agent/dream.md", strip=True, skill_creator_path=skill_creator_path,
        )
        prompt = f"{template}\n\n## Conversation History\n{history_text}"
        return (prompt, batch[-1]["cursor"])

    def build_dream_tools(self):
        """Build the restricted tool registry used by Dream runs."""
        from nanobot.agent.skills import BUILTIN_SKILLS_DIR
        from nanobot.agent.tools.apply_patch import ApplyPatchTool
        from nanobot.agent.tools.file_state import FileStates
        from nanobot.agent.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool
        from nanobot.agent.tools.registry import ToolRegistry

        tools = ToolRegistry()
        file_states = FileStates()
        workspace = self.workspace
        skills_dir = workspace / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        extra_read = [BUILTIN_SKILLS_DIR] if BUILTIN_SKILLS_DIR.exists() else None
        editable_roots = [self.soul_file, self.user_file, skills_dir]

        tools.register(ReadFileTool(
            workspace=workspace,
            allowed_dir=workspace,
            extra_allowed_dirs=extra_read,
            file_states=file_states,
        ))
        tools.register(EditFileTool(
            workspace=workspace,
            allowed_dir=self.memory_dir,
            extra_allowed_dirs=editable_roots,
            file_states=file_states,
        ))
        tools.register(ApplyPatchTool(
            workspace=workspace,
            allowed_dir=self.memory_dir,
            extra_allowed_dirs=editable_roots,
            file_states=file_states,
        ))
        tools.register(WriteFileTool(
            workspace=workspace,
            allowed_dir=skills_dir,
            file_states=file_states,
        ))
        return tools

    # -- Dream guardrails (M1b diff limits, M1c rollback, M1d observability) --

    _DREAM_SURFACE_FILES = ("SOUL.md", "USER.md")
    _DREAM_SURFACE_GLOBS = ("memory/**/*.md", "skills/**/SKILL.md")

    def _dream_surface_paths(self) -> list[str]:
        """Current memory-owned surface files under the M1b contract.

        Only SOUL.md, USER.md, ``memory/**/*.md`` and ``skills/**/SKILL.md``
        count toward Dream's changed-file/diff limits; ``history.jsonl`` and
        cursor files are deliberately excluded.
        """
        paths: set[str] = set()
        for name in self._DREAM_SURFACE_FILES:
            if (self.workspace / name).is_file():
                paths.add(name)
        for pattern in self._DREAM_SURFACE_GLOBS:
            for p in self.workspace.glob(pattern):
                if p.is_file():
                    paths.add(p.relative_to(self.workspace).as_posix())
        return sorted(paths)

    def capture_dream_snapshot(self) -> DreamSnapshot:
        """Snapshot the pre-Dream state of memory surfaces and git-tracked files."""
        surface = set(self._dream_surface_paths())
        paths = set(surface)
        for rel in self.git.tracked_files():
            if (self.workspace / rel).is_file():
                paths.add(rel)
        files: dict[str, bytes] = {}
        for rel in sorted(paths):
            p = self.workspace / rel
            try:
                files[rel] = p.read_bytes()
            except OSError:
                logger.warning("Dream snapshot: failed to read {}", p)
        return DreamSnapshot(files=files, surface=sorted(surface))

    @staticmethod
    def _diff_fragment(rel: str, old: bytes | None, new: bytes | None) -> str:
        """Deterministic unified-diff fragment for one surface file."""
        old_text = old.decode("utf-8", "replace") if old is not None else ""
        new_text = new.decode("utf-8", "replace") if new is not None else ""
        fromfile = "/dev/null" if old is None else f"a/{rel}"
        tofile = f"b/{rel}" if new is not None else "/dev/null"
        return "".join(difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
            lineterm="",
        ))

    def measure_dream_delta(
        self, snapshot: DreamSnapshot,
    ) -> tuple[list[str], int, str]:
        """Return ``(changed_files, diff_chars, diff_text)`` the run produced.

        The measure is deliberately git-independent: it compares the current
        working tree against the pre-run bytes, so new (untracked), deleted,
        and modified files inside the memory surfaces all count toward the M1b
        limits. Skipped files (cursors, history.jsonl) never appear.
        """
        current: dict[str, bytes] = {}
        for rel in self._dream_surface_paths():
            p = self.workspace / rel
            if p.is_file():
                try:
                    current[rel] = p.read_bytes()
                except OSError:
                    logger.warning("Dream delta: failed to read {}", p)
        changed: list[str] = []
        fragments: list[str] = []
        for rel in sorted(set(snapshot.surface) | set(current)):
            old = snapshot.files.get(rel)
            new = current.get(rel)
            if old == new:
                continue
            changed.append(rel)
            fragments.append(self._diff_fragment(rel, old, new))
        diff_text = "\n".join(fragments)
        return changed, len(diff_text), diff_text

    def restore_dream_state(self, snapshot: DreamSnapshot) -> list[str]:
        """Restore the pre-Dream working-tree state; return touched paths.

        Writes every snapshotted file back byte-for-byte (covering both
        git-tracked files and untracked additions, and working even when the
        workspace has no git repo) and removes newly created files under the
        memory surfaces.
        """
        touched: list[str] = []
        for rel, content in snapshot.files.items():
            p = self.workspace / rel
            try:
                if p.read_bytes() != content:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(content)
                    touched.append(rel)
            except OSError:
                logger.warning("Dream rollback: failed to restore {}", p)
        for rel in self._dream_surface_paths():
            if rel not in snapshot.files:
                try:
                    (self.workspace / rel).unlink()
                    touched.append(rel)
                except OSError:
                    logger.warning("Dream rollback: failed to remove new file {}", rel)
        return touched

    def _log_dream_outcome(
        self,
        outcome: DreamRunResult,
        *,
        model_label: str,
        max_batch_size: int,
        max_iterations: int,
        timeout_s: int,
    ) -> None:
        """M1d: log model, limits, stop reason, changed files and diff size."""
        common = (
            "model={} batch_size={} max_iterations={} timeout_s={} stop_reason={} "
            "changed_files={} diff_chars={} elapsed_s={:.1f} cursor={}"
        ).format(
            model_label,
            max_batch_size,
            max_iterations,
            timeout_s,
            outcome.stop_reason,
            outcome.changed_files,
            outcome.diff_chars,
            outcome.elapsed_s,
            self.get_last_dream_cursor(),
        )
        if outcome.completed:
            logger.info("Dream completed: {} commit={}", common, outcome.commit_sha)
        else:
            logger.warning("Dream incomplete (reason={}): {}", outcome.reason, common)

    def _record_dream_outcome(
        self,
        outcome: DreamRunResult,
        *,
        resp: Any | None,
        model_label: str,
        max_batch_size: int,
        max_iterations: int,
        timeout_s: int,
        session_key: str | None = None,
        session_manager: Any | None = None,
    ) -> None:
        """M1d: log the outcome and attach it to the response/session metadata."""
        self._log_dream_outcome(
            outcome,
            model_label=model_label,
            max_batch_size=max_batch_size,
            max_iterations=max_iterations,
            timeout_s=timeout_s,
        )
        meta = {
            "result": "completed" if outcome.completed else "incomplete",
            "reason": outcome.reason,
            "stop_reason": outcome.stop_reason,
            "model": model_label,
            "batch_size": max_batch_size,
            "max_iterations": max_iterations,
            "timeout_s": timeout_s,
            "changed_files": outcome.changed_files,
            "diff_chars": outcome.diff_chars,
            "commit_sha": outcome.commit_sha,
            "elapsed_s": round(outcome.elapsed_s, 2),
        }
        if resp is not None and isinstance(getattr(resp, "metadata", None), dict):
            resp.metadata["_dream_run"] = meta
        if session_manager is not None and session_key:
            try:
                session = session_manager.get_or_create(session_key)
                session.metadata["_last_dream_run"] = meta
                session_manager.save(session)
            except Exception:
                logger.warning("Dream: failed to record outcome in session metadata")

    async def run_dream(
        self,
        run: Callable[[str], Awaitable[Any]],
        *,
        max_batch_size: int = 20,
        max_iterations: int = 10,
        timeout_s: int = 300,
        max_changed_files: int = 8,
        max_diff_chars: int = 32_000,
        model_label: str = "unknown",
        commit_prefix: str = "dream: memory consolidation",
        session_key: str | None = None,
        session_manager: Any | None = None,
    ) -> DreamRunResult:
        """Run one guarded Dream pass: snapshot, run, validate, commit/roll back.

        ``run`` is an async callable receiving the Dream prompt; it is
        responsible for invoking ``process_direct`` with the caller's runtime
        kwargs. M1c rollback is applied for timeouts, exceptions, unexpected
        stop reasons (including max iterations), and M1b limit violations; the
        cursor only advances on a fully validated, committed run.

        Never raises for LLM/tool failures — those are recorded on the result.
        """
        start = time.monotonic()
        built = self.build_dream_prompt(max_entries=max_batch_size)
        if built is None:
            outcome = DreamRunResult(completed=False, reason="nothing_to_process")
            self._log_dream_outcome(
                outcome,
                model_label=model_label,
                max_batch_size=max_batch_size,
                max_iterations=max_iterations,
                timeout_s=timeout_s,
            )
            return outcome
        prompt, last_cursor = built

        snapshot = self.capture_dream_snapshot()
        resp: Any | None = None
        try:
            resp = await asyncio.wait_for(run(prompt), timeout=timeout_s)
        except asyncio.TimeoutError:
            outcome = DreamRunResult(
                completed=False, reason="timeout", stop_reason="timeout",
                elapsed_s=time.monotonic() - start,
            )
            return self._finish_incomplete_dream(
                snapshot, resp, outcome,
                model_label=model_label, max_batch_size=max_batch_size,
                max_iterations=max_iterations, timeout_s=timeout_s,
                session_key=session_key, session_manager=session_manager,
            )
        except Exception as exc:
            outcome = DreamRunResult(
                completed=False, reason="exception", stop_reason="exception",
                error=str(exc), elapsed_s=time.monotonic() - start,
            )
            return self._finish_incomplete_dream(
                snapshot, resp, outcome,
                model_label=model_label, max_batch_size=max_batch_size,
                max_iterations=max_iterations, timeout_s=timeout_s,
                session_key=session_key, session_manager=session_manager,
            )

        stop_reason = None
        if resp is not None:
            metadata = getattr(resp, "metadata", None)
            if isinstance(metadata, dict):
                stop_reason = metadata.get("_stop_reason")
        elapsed = time.monotonic() - start
        if not self.dream_run_completed(resp):
            outcome = DreamRunResult(
                completed=False,
                reason=stop_reason or "incomplete",
                stop_reason=stop_reason,
                elapsed_s=elapsed,
            )
            return self._finish_incomplete_dream(
                snapshot, resp, outcome,
                model_label=model_label, max_batch_size=max_batch_size,
                max_iterations=max_iterations, timeout_s=timeout_s,
                session_key=session_key, session_manager=session_manager,
            )

        changed_files, diff_chars, diff_text = self.measure_dream_delta(snapshot)
        if len(changed_files) > max_changed_files:
            outcome = DreamRunResult(
                completed=False, reason="max_changed_files", stop_reason=stop_reason,
                changed_files=changed_files, diff_chars=diff_chars,
                diff=diff_text, elapsed_s=elapsed,
            )
            return self._finish_incomplete_dream(
                snapshot, resp, outcome,
                model_label=model_label, max_batch_size=max_batch_size,
                max_iterations=max_iterations, timeout_s=timeout_s,
                session_key=session_key, session_manager=session_manager,
            )
        if diff_chars > max_diff_chars:
            outcome = DreamRunResult(
                completed=False, reason="max_diff_chars", stop_reason=stop_reason,
                changed_files=changed_files, diff_chars=diff_chars,
                diff=diff_text, elapsed_s=elapsed,
            )
            return self._finish_incomplete_dream(
                snapshot, resp, outcome,
                model_label=model_label, max_batch_size=max_batch_size,
                max_iterations=max_iterations, timeout_s=timeout_s,
                session_key=session_key, session_manager=session_manager,
            )

        # Fully validated: advance the cursor and auto-commit.
        self.set_last_dream_cursor(last_cursor)
        commit_sha = None
        if self.git.is_initialized():
            commit_sha = self.git.auto_commit(
                self.build_dream_commit_message(commit_prefix, resp),
            )
        outcome = DreamRunResult(
            completed=True, reason="completed", stop_reason=stop_reason,
            changed_files=changed_files, diff_chars=diff_chars,
            diff=diff_text, commit_sha=commit_sha, elapsed_s=elapsed,
        )
        self._record_dream_outcome(
            outcome, resp=resp,
            model_label=model_label, max_batch_size=max_batch_size,
            max_iterations=max_iterations, timeout_s=timeout_s,
            session_key=session_key, session_manager=session_manager,
        )
        return outcome

    def _finish_incomplete_dream(
        self,
        snapshot: DreamSnapshot,
        resp: Any | None,
        outcome: DreamRunResult,
        *,
        model_label: str,
        max_batch_size: int,
        max_iterations: int,
        timeout_s: int,
        session_key: str | None = None,
        session_manager: Any | None = None,
    ) -> DreamRunResult:
        """M1c: roll back the working tree and record the incomplete outcome."""
        touched = self.restore_dream_state(snapshot)
        if touched:
            logger.warning(
                "Dream rollback: restored {} path(s): {}", len(touched), touched,
            )
        self._record_dream_outcome(
            outcome, resp=resp,
            model_label=model_label, max_batch_size=max_batch_size,
            max_iterations=max_iterations, timeout_s=timeout_s,
            session_key=session_key, session_manager=session_manager,
        )
        return outcome

    @staticmethod
    def dream_run_completed(resp: object | None) -> bool:
        """Return True only when an ephemeral Dream agent turn completed cleanly."""
        metadata = getattr(resp, "metadata", None)
        return isinstance(metadata, dict) and metadata.get("_stop_reason") == "completed"

    # -- message formatting utility ------------------------------------------

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            if not message.get("content"):
                continue
            tools = f" [tools: {', '.join(message['tools_used'])}]" if message.get("tools_used") else ""
            ts = str(message.get("timestamp") or "?")[:16]
            lines.append(
                f"[{ts}] {message['role'].upper()}{tools}: {message['content']}"
            )
        return "\n".join(lines)

    @staticmethod
    def _clean_history_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep only stable, JSON-friendly fields for structured history."""
        cleaned: list[dict[str, Any]] = []
        for original in messages:
            message = sanitize_message_for_persistence(original)
            content = strip_think(str(message.get("content") or ""))
            if not content:
                continue
            record: dict[str, Any] = {
                "timestamp": message.get("timestamp"),
                "role": message.get("role"),
                "content": content,
            }
            name = message.get("name")
            if name:
                record["name"] = name
            tools_used = message.get("tools_used")
            if tools_used:
                record["tools_used"] = list(tools_used)
            cleaned.append(record)
        return cleaned

    def raw_archive(
        self,
        messages: list[dict],
        *,
        session_key: str | None = None,
        max_chars: int | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """Fallback: dump raw messages to history.jsonl without LLM summarization."""
        limit = max_chars if max_chars is not None else _RAW_ARCHIVE_MAX_CHARS
        cleaned_messages = [sanitize_message_for_persistence(m) for m in messages]
        formatted = truncate_text(self._format_messages(cleaned_messages), limit)
        self.append_conversation_history(
            [{
                "timestamp": datetime.now().astimezone().isoformat(timespec="minutes"),
                "role": "user",
                "content": f"[RAW] {len(messages)} messages\n{formatted}",
            }],
            session_key=session_key,
            usage=usage,
        )
        logger.warning(
            "Memory consolidation degraded: raw-archived {} messages", len(messages)
        )

    # ------------------------------------------------------------------
    # Dream helpers
    # ------------------------------------------------------------------

    @staticmethod
    def dream_session_key() -> str:
        """Return a unique session key for a Dream run, e.g. ``dream:20260528-100000``."""
        return f"dream:{datetime.now():%Y%m%d-%H%M%S}"

    @staticmethod
    def build_dream_commit_message(prefix: str, resp: object | None) -> str:
        """Build a Dream auto-commit message, appending the LLM summary if present."""
        msg = prefix
        if resp is not None and getattr(resp, "content", None):
            msg = f"{msg}\n\n{resp.content.strip()}"
        return msg

    @staticmethod
    def prune_dream_sessions(sessions_dir: Path, *, keep: int = 10) -> None:
        """Remove the oldest Dream session files, keeping only the N most recent.

        Only files matching ``dream_*.jsonl`` are considered. Non-dream session
        files are never touched. Persisted session timestamps are authoritative;
        filenames provide a deterministic fallback for incomplete files.
        """
        def session_order(path: Path) -> tuple[int, float, str]:
            try:
                with path.open(encoding="utf-8") as file:
                    metadata = json.loads(file.readline())
                if isinstance(metadata, dict):
                    for field in ("updated_at", "created_at"):
                        value = metadata.get(field)
                        if isinstance(value, str):
                            try:
                                return 2, datetime.fromisoformat(value).timestamp(), path.name
                            except ValueError:
                                continue
            except (OSError, json.JSONDecodeError):
                pass

            timestamp = path.stem.removeprefix("dream_")
            try:
                return 1, datetime.strptime(timestamp, "%Y%m%d-%H%M%S").timestamp(), path.name
            except ValueError:
                return 0, 0.0, path.name

        dream_files = sorted(
            sessions_dir.glob("dream_*.jsonl"), key=session_order,
        )
        if len(dream_files) <= keep:
            return

        to_remove = dream_files[: len(dream_files) - keep]
        for path in to_remove:
            try:
                path.unlink()
                logger.debug("Pruned old dream session: {}", path.stem)
            except OSError:
                logger.warning("Failed to prune dream session {}", path)


# ---------------------------------------------------------------------------
# Consolidator — lightweight token-budget triggered consolidation
# ---------------------------------------------------------------------------

# Individual history.jsonl writers cap their own payloads tightly; the
# _HISTORY_ENTRY_HARD_CAP at append_history() is a belt-and-suspenders default
# that catches any new caller that forgot to set its own cap.
_RAW_ARCHIVE_MAX_CHARS = 16_000       # fallback dump (LLM failed)
_ARCHIVE_SUMMARY_MAX_CHARS = 8_000    # LLM-produced consolidation summary
_FALLBACK_SUMMARY_MAX_CHARS = 2_000   # bounded dialogue when no LLM summary is available
_HISTORY_ENTRY_HARD_CAP = 64_000      # emergency cap in append_history


class Consolidator:
    """Coordinate continuity checkpoints and the separate explicit memory-archive policy."""

    _SAFETY_BUFFER = 1024  # extra headroom for tokenizer estimation drift

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        model: str,
        sessions: SessionManager,
        context_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        max_completion_tokens: int = 4096,
        consolidation_ratio: float = 0.5,
        unified_session: bool = False,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self.max_completion_tokens = max_completion_tokens
        self.consolidation_ratio = consolidation_ratio
        self.unified_session = unified_session
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        # Pre-compaction memory flush (issue #33): idempotent by range, guarded
        # against recursion/cross-session overlap.
        self._flush_in_flight: set[str] = set()
        self._flush_max_input_messages = 40
        self._flush_input_chars = 6000
        self._flush_max_tokens = 300
        self._flush_wall_timeout_s = 30.0

    def set_provider(
        self,
        provider: LLMProvider,
        model: str,
        context_window_tokens: int,
    ) -> None:
        self.provider = provider
        self.model = model
        self.context_window_tokens = context_window_tokens
        self.max_completion_tokens = provider.generation.max_tokens

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """Return the shared consolidation lock for one session."""
        return self._locks.setdefault(session_key, asyncio.Lock())

    def _usage_to_archive(self, session_key: str | None) -> dict[str, Any]:
        if not session_key:
            return {}
        session = self.sessions.get_or_create(session_key)
        return usage_delta(
            session.metadata.get("usage"),
            session.metadata.get("_usage_archived"),
        )

    def _mark_usage_archived(self, session_key: str | None) -> None:
        if not session_key:
            return
        session = self.sessions.get_or_create(session_key)
        session.metadata["_usage_archived"] = usage_snapshot(session.metadata.get("usage"))
        self.sessions.save(session)

    @staticmethod
    def _checkpoint_covered_messages(
        session: Session,
        checkpoint: dict[str, Any],
        previous: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """Transcript slice a checkpoint moves out of the replay window."""
        boundary = checkpoint.get("first_kept_entry_id")
        if not boundary:
            return []
        ids = [m.get("_entry_id") for m in session.messages]
        if boundary not in ids:
            logger.warning(
                "Checkpoint boundary absent from transcript for {}; nothing archived",
                session.key,
            )
            return []
        end = ids.index(boundary)
        prior_boundary = (previous or {}).get("first_kept_entry_id")
        if prior_boundary and prior_boundary in ids:
            start = ids.index(prior_boundary)
        else:
            if prior_boundary:
                logger.warning(
                    "Previous checkpoint boundary missing for {}; archiving from last_consolidated",
                    session.key,
                )
            start = min(session.last_consolidated, end)
        return session.messages[start:end]

    def archive_compacted_range(
        self,
        session: Session,
        checkpoint: dict[str, Any],
        *,
        previous: dict[str, Any] | None,
    ) -> bool:
        """Mirror the range a continuity checkpoint just hid into history.jsonl.

        Checkpoints govern what the model replays; ``memory/history.jsonl`` is
        the separate, searchable log that memory search, Dream and the
        reflection jobs read. Hiding a range behind a checkpoint must not take
        it out of that log, so the newly covered messages are appended as one
        structured conversation record.

        No summary is attached: checkpoint summaries are cumulative, so storing
        one per record would repeat earlier context in every later record.

        Never raises. The caller persists the advanced usage watermark in the
        same save as the checkpoint. Returns True when a record was appended.
        """
        try:
            messages = self._checkpoint_covered_messages(session, checkpoint, previous)
        except Exception:
            logger.exception("Could not resolve checkpoint range for {}", session.key)
            return False
        if not messages:
            return False
        usage = self._usage_to_archive(session.key)
        try:
            pruned = self._prune_messages(messages)
            if not pruned:
                raise ValueError("nothing survived pruning")
            self.store.append_conversation_history(
                pruned,
                session_key=session.key,
                usage=usage,
            )
        except Exception:
            logger.warning(
                "Structured archive of compacted range failed for {}; raw-dumping",
                session.key,
            )
            try:
                self.store.raw_archive(messages, session_key=session.key, usage=usage)
            except Exception:
                logger.exception(
                    "Could not archive compacted range for {}: those messages are no "
                    "longer reachable through memory search",
                    session.key,
                )
                return False
        session.metadata["_usage_archived"] = usage_snapshot(session.metadata.get("usage"))
        return True

    @staticmethod
    def _full_unconsolidated_history(
        session: Session,
        *,
        include_timestamps: bool = False,
    ) -> list[dict[str, Any]]:
        """Return the complete continuity projection for prompt estimation."""
        return session.get_history(projected=True, include_timestamps=include_timestamps)

    def estimate_session_prompt_tokens(
        self,
        session: Session,
    ) -> tuple[int, str]:
        """Estimate prompt size from the full unconsolidated session tail."""
        history = self._full_unconsolidated_history(session, include_timestamps=True)
        channel, chat_id = (session.key.split(":", 1) if ":" in session.key else (None, None))
        # The archived summary is read from history.jsonl inside
        # build_system_prompt via session_key, so the probe accounts for it.
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
            sender_id=None,
            session_metadata=session.metadata,
            session_key=session.key,
            unified_session=self.unified_session,
            input_token_budget=self._input_token_budget,
        )
        return estimate_prompt_tokens_chain(
            self.provider,
            self.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    @property
    def _input_token_budget(self) -> int:
        """Available input token budget for consolidation LLM."""
        return self.context_window_tokens - self.max_completion_tokens - self._SAFETY_BUFFER

    def _truncate_to_token_budget(self, text: str) -> str:
        """Truncate text so it fits within the consolidation LLM's token budget."""
        budget = self._input_token_budget
        if budget <= 0:
            return truncate_text(text, _RAW_ARCHIVE_MAX_CHARS)
        try:
            enc = tiktoken.get_encoding("cl100k_base")
            tokens = enc.encode(text)
            if len(tokens) <= budget:
                return text
            return enc.decode(tokens[:budget]) + "\n... (truncated)"
        except Exception:
            return truncate_text(text, budget * 4)

    @staticmethod
    def _prune_messages(messages: list[dict]) -> list[dict]:
        """Mechanically prune tool noise from messages before archiving.

        Replaces tool-role messages with short placeholders showing the
        tool name and output size. Removes empty assistant messages
        (tool-call-only turns). Keeps all user/assistant text intact.
        Dream gets actual conversation context instead of LLM summaries.
        """
        pruned: list[dict] = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "tool":
                name = msg.get("name", "tool")
                lines = content.count("\n") + 1 if content else 0
                chars = len(content) if content else 0
                placeholder = f"[tool: {name} — {lines} lines, {chars} chars]"
                pruned.append({**msg, "content": placeholder})
                continue
            if not str(content or "").strip():
                continue
            pruned.append(msg)
        return pruned

    async def archive(
        self,
        messages: list[dict],
        *,
        session_key: str | None = None,
        usage: dict[str, Any] | None = None,
        summary_messages: list[dict] | None = None,
    ) -> str | None:
        """Prune tool noise, LLM-summarize, append one conversation record.

        The durable record keeps structured ``messages`` plus the mechanical
        ``content`` preview. When the LLM summary succeeds, it is stored in the
        same JSONL line as ``summary``. Summary failure does not lose data: the
        pruned conversation record is still appended without ``summary``.
        """
        if not messages:
            return None
        usage_to_write = usage if usage is not None else self._usage_to_archive(session_key)
        try:
            pruned = self._prune_messages(messages)
            if not pruned:
                return None
            messages_to_summarize = summary_messages if summary_messages is not None else messages
            pruned_for_summary = self._prune_messages(messages_to_summarize)
            formatted = MemoryStore._format_messages(pruned_for_summary)
            formatted = self._truncate_to_token_budget(formatted)
            summary: str | None = None
            try:
                response = await self.provider.chat_with_retry(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": render_template(
                                "agent/consolidator_archive.md",
                                strip=True,
                            ),
                        },
                        {"role": "user", "content": formatted},
                    ],
                    tools=None,
                    tool_choice=None,
                )
                if response.finish_reason == "error":
                    raise RuntimeError(f"LLM returned error: {response.content}")
                summary = response.content or "[no summary]"
            except Exception:
                logger.warning("Consolidation summary failed; archiving transcript without summary")

            archive_formatted = MemoryStore._format_messages(pruned)
            archive_formatted = self._truncate_to_token_budget(archive_formatted)
            self.store.append_conversation_history(
                pruned,
                session_key=session_key,
                content=f"[CONV] {len(pruned)} messages\n{archive_formatted}",
                max_chars=_ARCHIVE_SUMMARY_MAX_CHARS,
                usage=usage_to_write,
                summary=summary,
            )
            if usage is None:
                self._mark_usage_archived(session_key)
            return summary
        except Exception:
            logger.warning("Mechanical pruning failed, raw-dumping to history")
            self.store.raw_archive(messages, session_key=session_key, usage=usage_to_write)
            if usage is None:
                self._mark_usage_archived(session_key)
            return None

    async def maybe_consolidate_by_tokens(
        self,
        session: Session,
    ) -> None:
        """Checkpoint session continuity; archival/Dream are separate policies."""
        from nanobot.session.compaction import RequestBudget

        if self.context_window_tokens <= 0 or not session.messages:
            return
        budget_snapshot = RequestBudget.freeze(
            self.provider, self.model, self.context_window_tokens, self.max_completion_tokens,
        )
        history = session.get_history(projected=True)
        prompt = self._build_messages(history=history, current_message="")
        if (len(session.messages) <= FILE_MAX_MESSAGES
                and budget_snapshot.estimate(self.provider, prompt, self._get_tool_definitions()) < (
                    budget_snapshot.input_limit - budget_snapshot.headroom
                )):
            return
        await self.compact_session(session, budget_snapshot=budget_snapshot)

    async def compact_session(self, session: Session, *, budget_snapshot=None) -> str | None:
        """Generate optimistically and commit only against the unchanged transcript."""
        from copy import deepcopy

        from nanobot.session.compaction import (
            CHECKPOINTS,
            CompactionEngine,
            CompactionError,
            RequestBudget,
            fingerprint,
            latest_checkpoint,
        )

        provider = self.provider
        budget = budget_snapshot or RequestBudget.freeze(
            provider, self.model, self.context_window_tokens, self.max_completion_tokens,
        )
        async with self.get_lock(session.key):
            if self.sessions.get_or_create(session.key) is not session:
                return None
            await self.flush_before_compaction(session)
            before = fingerprint(session.messages)
            prior = deepcopy(latest_checkpoint(session.metadata))
            source = session.messages
            if prior is None:
                source = session.messages[session.last_consolidated:]
                legacy = session.metadata.get("_last_summary")
                if isinstance(legacy, dict) and legacy.get("text"):
                    source = [{"role": "user", "content": legacy["text"],
                               "_compaction_summary": True}] + source
            try:
                checkpoint = await CompactionEngine(provider, budget).prepare(
                    source, previous=prior,
                    recent_tokens=max(1, int(budget.input_limit * self.consolidation_ratio)),
                )
                if (fingerprint(session.messages) != before
                        or latest_checkpoint(session.metadata) != prior
                        or self.sessions.get_or_create(session.key) is not session):
                    raise CompactionError("Session changed during compaction; checkpoint discarded")
                old_metadata = deepcopy(session.metadata)
                # Keep the checkpointed range in the searchable log too; the
                # checkpoint only governs what the model replays.
                self.archive_compacted_range(session, checkpoint, previous=prior)
                session.metadata.setdefault(CHECKPOINTS, []).append(checkpoint)
                session.metadata.pop("_last_summary", None)
                try:
                    self.sessions.save(session, fsync=True)
                except BaseException:
                    session.metadata = old_metadata
                    raise
                return checkpoint["summary"]
            except CompactionError as exc:
                logger.warning("Compaction failed for {}: {}", session.key, exc)
                return None

    # ------------------------------------------------------------------
    # Pre-compaction memory flush (issue #33)
    # ------------------------------------------------------------------

    def _flush_provenance_path(self) -> Path:
        return Path(self.store.workspace) / "memory" / "flush_provenance.jsonl"

    @staticmethod
    def _message_trust_classes(messages: list[dict[str, Any]]) -> list[str]:
        """Bounded trust classes derived from message roles + explicit markers."""
        classes: set[str] = set()
        for entry in messages:
            role = str(entry.get("role") or "")
            meta = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
            if role == "user":
                classes.add("user")
            elif role == "assistant":
                classes.add("assistant")
            else:
                classes.add("tool_untrusted")
            if meta.get("_untrusted") or meta.get("untrusted"):
                classes.add("tool_untrusted")
        return sorted(classes) or ["tool_untrusted"]

    async def flush_before_compaction(self, session) -> dict[str, Any]:
        """Silent, bounded, idempotent memory flush of the unflushed range.

        Returns a bounded provenance record; never raises and never blocks
        emergency compaction beyond the wall timeout. The same range is not
        flushed twice (session metadata checkpoint survives restart).
        """
        key = session.key
        meta = session.metadata.setdefault("_precompact_flush", {"last": 0})
        start = int(meta.get("last", 0))
        total = len(session.messages)
        if total <= start:
            return {"session": key, "range": [start, total], "result": "nothing"}
        if key in self._flush_in_flight:
            return {"session": key, "range": [start, total], "result": "skipped_in_flight"}
        self._flush_in_flight.add(key)
        try:
            result = await asyncio.wait_for(
                self._run_flush(session, start, total),
                timeout=self._flush_wall_timeout_s,
            )
            if result["result"] in ("changed", "nothing"):
                meta["last"] = total
                self.sessions.save(session)
            return result
        except asyncio.TimeoutError:
            return self._record_flush(session, start, total, "failed", "timeout")
        except Exception as exc:
            return self._record_flush(session, start, total, "failed", str(exc)[:200])
        finally:
            self._flush_in_flight.discard(key)

    async def _run_flush(self, session, start: int, total: int) -> dict[str, Any]:
        range_messages = session.messages[start:total][-self._flush_max_input_messages:]
        trust = self._message_trust_classes(range_messages)
        # Hard trust boundary: web/tool-only ranges never become durable
        # personal memory; the model is not even consulted.
        if not ({"user", "assistant"} & set(trust)):
            return self._record_flush(session, start, total, "nothing", trust=trust)

        prompt_lines = []
        for entry in range_messages:
            role = str(entry.get("role") or "?")
            content = entry.get("content") or entry.get("text") or ""
            sub = entry.get("messages")
            if isinstance(sub, list):
                content = " ".join(str(m.get("content") or "") for m in sub if isinstance(m, dict))
            if isinstance(content, str) and content.strip():
                prompt_lines.append(f"{role}: {content.strip()[:200]}")
        sample = "\n".join(prompt_lines)[: self._flush_input_chars] if prompt_lines else "(empty range)"
        contract = (
            "You are the memory-flush pass before conversation compaction. "
            "List at most 3 durable personal USER/ASSISTANT-supported facts, "
            "one per line starting with FACT:. If nothing is durable, reply exactly "
            "none. Never invent facts; never write credentials."
        )
        response = await self.provider.chat_with_retry(**{
            "messages": [{"role": "user", "content": contract + "\n\n" + sample}],
            "model": self.model,
            "max_tokens": self._flush_max_tokens,
            "temperature": 0.0,
        })
        text = (response.content or "").strip()
        facts = [
            line[len("FACT:"):].strip()
            for line in text.splitlines()
            if line.strip().upper().startswith("FACT:")
        ][:3]
        if (not facts) or text.lower() in ("none", "nothing"):
            return self._record_flush(session, start, total, "nothing", trust=trust)
        destination = self._append_facts(session, start, total, facts)
        return self._record_flush(session, start, total, "changed", trust=trust,
                                  destinations=[destination])

    def _append_facts(self, session, start: int, total: int, facts: list[str]) -> str:
        """Append bounded facts without replacing facts from earlier flushes."""
        from filelock import FileLock

        from nanobot.agent.tools._memory_common import atomic_write_text, resolve_topic_path

        rel = "flush-notes.md"
        workspace = Path(self.store.workspace)
        workspace.joinpath("memory").mkdir(parents=True, exist_ok=True)
        resolved = resolve_topic_path(workspace, rel)
        heading = f"## Pre-compaction flush {start}-{total}\n"
        body = "\n".join("- " + f[:300] for f in facts) + "\n"
        lock = FileLock(str(resolved) + ".lock", timeout=10)
        with lock:
            existing = resolved.read_text(encoding="utf-8") if resolved.exists() else "# Flush notes\n"
            separator = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
            atomic_write_text(resolved, existing + separator + heading + body)
        return f"memory/{rel}"

    def _record_flush(
        self, session, start: int, total: int, result: str,
        error: str | None = None, trust: list[str] | None = None,
        destinations: list[str] | None = None,
    ) -> dict[str, Any]:
        record = {
            "ts": int(time.time() * 1000),
            "session": session.key,
            "range": [int(start), int(total)],
            "origin": "precompact_flush",
            "result": result,
            "trust": trust or self._message_trust_classes(session.messages[start:total]),
            "destinations": destinations or [],
        }
        if error:
            record["error"] = error
        try:
            path = self._flush_provenance_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            logger.warning("Precompact flush provenance write failed", exc_info=True)
        return {k: record.get(k) for k in ("result", "range", "trust", "destinations")}


    async def compact_idle_session(
        self,
        session_key: str,
    ) -> str | None:
        """Idle and manual compaction use the same non-destructive checkpoint engine."""
        session = self.sessions.get_or_create(session_key)
        if not session.messages:
            session.updated_at = datetime.now()
            self.sessions.save(session)
            return ""
        return await self.compact_session(session)
