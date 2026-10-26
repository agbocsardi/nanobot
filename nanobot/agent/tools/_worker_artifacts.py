"""Run-local disk artifacts. IDs are capabilities, never paths supplied by a model."""

from __future__ import annotations

import json
import tempfile
import uuid
from collections.abc import Set
from pathlib import Path
from typing import Any

from nanobot.utils.helpers import _write_text_atomic

INLINE_BYTES = 16000  # Model-visible output, not the script's working data.
SCRIPT_BYTES = 1024 * 1024
READ_CHARS = 8000
MAX_READ_CHARS = 128000


class ArtifactStore:
    """Private, disposable storage; no user workspace or cross-run path lookup."""

    def __init__(self, max_bytes: int = 32 * 1024 * 1024):
        self._directory = tempfile.TemporaryDirectory(prefix="nanobot-worker-artifacts-")
        self._paths: dict[str, tuple[Path, int]] = {}
        self._bytes = 0
        self.max_bytes = max_bytes

    def close(self) -> None:
        self._directory.cleanup()

    def put(self, text: str) -> dict[str, Any]:
        size = len(text.encode("utf-8"))
        if self._bytes + size > self.max_bytes or len(self._paths) >= 256:
            raise ValueError("Run artifact storage budget exceeded; payload was not retained")
        artifact = uuid.uuid4().hex
        path = Path(self._directory.name) / artifact
        _write_text_atomic(path, text)
        self._paths[artifact] = (path, len(text))
        self._bytes += size
        return {"artifact": artifact, "chars": len(text), "bytes": size}

    def validate(self, artifact: str, allowed: Set[str] | None = None) -> None:
        if not isinstance(artifact, str) or artifact not in self._paths or (
            allowed is not None and artifact not in allowed
        ):
            raise ValueError("Unknown or inaccessible artifact in this run/profile")

    def read(
        self, artifact: str, offset: int = 0, limit: int | None = READ_CHARS,
        *, allowed: Set[str] | None = None,
    ) -> dict[str, Any]:
        self.validate(artifact, allowed)
        if (type(offset) is not int or offset < 0 or (limit is not None and (
                type(limit) is not int or not 1 <= limit <= MAX_READ_CHARS))):
            raise ValueError(f"offset must be nonnegative; limit must be None or 1..{MAX_READ_CHARS}")
        path, chars = self._paths[artifact]
        # UTF-8 offsets are characters, not bytes. Storage is bounded for the whole run.
        with path.open(encoding="utf-8") as handle:
            remaining = offset
            while remaining:
                chunk = handle.read(min(remaining, READ_CHARS))
                if not chunk:
                    break
                remaining -= len(chunk)
            # Full reads are convenient for computation, but must still fit the bridge.
            text = handle.read(limit if limit is not None else SCRIPT_BYTES)
        end = offset + len(text)
        more = end < chars
        if limit is None and (more or len(json.dumps(text, ensure_ascii=True).encode()) > SCRIPT_BYTES - 1024):
            raise ValueError("Artifact exceeds the 1 MiB script read limit; request offset/limit chunks or use a worker")
        return {"artifact": artifact, "text": text, "offset": offset,
                "next_offset": end if more else None, "chars": chars,
                "partial": more}

    def search(self, artifact: str, query: str, *, allowed: Set[str]) -> dict[str, Any]:
        self.validate(artifact, allowed)
        if not isinstance(query, str) or not 1 <= len(query) <= 200:
            raise ValueError("query must be a literal string of 1..200 characters")
        path, chars = self._paths[artifact]
        text = path.read_text(encoding="utf-8")
        offsets = []
        start = 0
        while len(offsets) < 100:
            found = text.find(query, start)
            if found < 0:
                break
            offsets.append(found)
            start = found + max(1, len(query))
        return {"artifact": artifact, "offsets": offsets, "chars": chars,
                "partial": text.find(query, start) >= 0 if len(offsets) == 100 else False}

    def bound(self, value: Any, *, max_bytes: int = INLINE_BYTES) -> Any:
        """Offload before truncating; script transport and model output have distinct limits."""
        serialized = json.dumps(value, ensure_ascii=True, allow_nan=False)
        if len(serialized.encode()) <= max_bytes:
            return value
        reference = self.put(serialized)
        result = {"partial": True, **reference, "preview": serialized[:1000]}
        if isinstance(value, dict):
            # Operational status remains visible even when the content/data is out of line.
            for key in ("status", "stop_reason", "retryable", "exit_code", "postcondition",
                        "content", "error", "evidence", "side_effects", "usage", "source_artifacts"):
                if key in value:
                    rendered = json.dumps(value[key], ensure_ascii=True, allow_nan=False)
                    result[key] = value[key] if len(rendered) <= 1000 else {
                        "partial": True, "preview": rendered[:500], "artifact": reference["artifact"],
                    }
        return result
