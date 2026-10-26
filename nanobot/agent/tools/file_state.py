"""Track file freshness for read-before-edit warnings."""

from __future__ import annotations

import hashlib
import os
from collections import OrderedDict
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ReadState:
    mtime: float
    content_hash: str | None


def _hash_file(p: str) -> str | None:
    try:
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()
    except OSError:
        return None


class FileStates:
    """Per-session read/write freshness tracker; reads always return content."""

    __slots__ = ("_state",)

    def __init__(self) -> None:
        self._state: dict[str, ReadState] = {}

    def record_read(self, path: str | Path, offset: int = 1, limit: int | None = None) -> None:
        """Record that a file was read (called after successful read)."""
        p = str(Path(path).resolve())
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            return
        self._state[p] = ReadState(
            mtime=mtime,
            content_hash=_hash_file(p),
        )

    def record_write(self, path: str | Path) -> None:
        """Record that a file was written (updates mtime in state)."""
        p = str(Path(path).resolve())
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            self._state.pop(p, None)
            return
        self._state[p] = ReadState(
            mtime=mtime,
            content_hash=_hash_file(p),
        )

    def check_read(self, path: str | Path) -> str | None:
        """Check if a file has been read and is fresh.

        Returns None if OK, or a warning string.
        When mtime changed but file content is identical (e.g. touch, editor save),
        the check passes to avoid false-positive staleness warnings.
        """
        p = str(Path(path).resolve())
        entry = self._state.get(p)
        if entry is None:
            return "Warning: file has not been read yet. Read it first to verify content before editing."
        try:
            current_mtime = os.path.getmtime(p)
        except OSError:
            return None
        if current_mtime != entry.mtime:
            if entry.content_hash and _hash_file(p) == entry.content_hash:
                entry.mtime = current_mtime
                return None
            return "Warning: file has been modified since last read. Re-read to verify content before editing."
        # mtime unchanged - still check content hash to detect quick modifications
        if entry.content_hash and _hash_file(p) != entry.content_hash:
            return "Warning: file has been modified since last read. Re-read to verify content before editing."
        return None

    def get(self, path: str | Path) -> ReadState | None:
        """Return the raw ReadState entry for a path, or None."""
        return self._state.get(str(Path(path).resolve()))

    def clear(self) -> None:
        """Clear all tracked state (useful for testing)."""
        self._state.clear()


# Maximum number of logical sessions whose read/write file state is retained
# per AgentLoop.  Bounded so a long-running gateway process cannot grow this
# tracker without limit; eviction is least-recently-used (see FileStateStore).
# Our fork has no SESSION_CACHE_MAX_SIZE in nanobot/session/manager.py (its
# session cache is unbounded), so the bound is defined locally here.
MAX_FILE_STATE_SESSIONS = 128


class FileStateStore:
    """Bounded lookup table for per-session file read/write state.

    Session-scoped: one FileStates tracker per logical session key, all access
    happening on the owning AgentLoop's single event loop. It is intentionally
    not thread-safe, matching the historical plain-dict behavior; a session is
    never read/written concurrently from different OS threads. When the number
    of tracked sessions exceeds ``max_sessions``, the least-recently-used
    session's state is evicted.
    """

    __slots__ = ("_max_sessions", "_states_by_key")

    def __init__(self, *, max_sessions: int = MAX_FILE_STATE_SESSIONS) -> None:
        if max_sessions <= 0:
            raise ValueError("max_sessions must be positive")
        self._max_sessions = max_sessions
        self._states_by_key: OrderedDict[str, FileStates] = OrderedDict()

    def for_session(self, session_key: str | None) -> FileStates:
        key = session_key or "__default__"
        states = self._states_by_key.pop(key, None)
        if states is None:
            states = FileStates()
        self._states_by_key[key] = states
        while len(self._states_by_key) > self._max_sessions:
            self._states_by_key.popitem(last=False)
        return states

    def discard(self, session_key: str | None) -> None:
        """Forget file state when a session is reset or removed."""
        self._states_by_key.pop(session_key or "__default__", None)

    def clear(self) -> None:
        self._states_by_key.clear()


_current_file_states: ContextVar[FileStates | None] = ContextVar(
    "nanobot_file_states",
    default=None,
)


def current_file_states(default: FileStates) -> FileStates:
    """Return the FileStates bound to the current agent task, or a fallback."""
    return _current_file_states.get() or default


def bind_file_states(file_states: FileStates) -> Token[FileStates | None]:
    """Bind file read/write state for the current async task."""
    return _current_file_states.set(file_states)


def reset_file_states(token: Token[FileStates | None]) -> None:
    _current_file_states.reset(token)


# Module-level default instance, retained for backward compatibility with
# tests and callers that reach in directly. Per-session callers should hold
# their own FileStates instance instead of touching this one.
_default = FileStates()


def record_read(path: str | Path, offset: int = 1, limit: int | None = None) -> None:
    _default.record_read(path, offset=offset, limit=limit)


def record_write(path: str | Path) -> None:
    _default.record_write(path)


def check_read(path: str | Path) -> str | None:
    return _default.check_read(path)


def clear() -> None:
    _default.clear()


# Legacy attribute for callers that reached into the module-level dict
# directly (filesystem.py used to do this). Kept as a property-like accessor
# so existing imports keep working.
def __getattr__(name: str):
    if name == "_state":
        return _default._state
    raise AttributeError(name)
