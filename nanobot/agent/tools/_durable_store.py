"""Tiny durable JSON store shared by ask_user and standing_intents.

One file under the workspace, one process lock, atomic writes through the
canonical ``nanobot.utils.run_records._atomic_write``, and a plain empty
list on corrupt input. Domain logic (scopes, statuses, TTLs) stays in the
owning tools.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from nanobot.utils.run_records import _atomic_write


class DurableJsonStore:
    """One-file JSON list store: ``{"version": 1, "<key>": [...]}``.

    All mutations happen through the owning tool under ``lock``; the store
    itself only serializes. Loads are tolerant: missing or corrupt input
    degrades to an empty list.
    """

    def __init__(self, path: Path, key: str) -> None:
        self._path = path
        self._key = key
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    def load(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        items = data.get(self._key, []) if isinstance(data, dict) else []
        return [item for item in items if isinstance(item, dict)]

    def write(self, items: list[dict[str, Any]]) -> None:
        _atomic_write(
            self._path,
            json.dumps({"version": 1, self._key: items}, indent=2, ensure_ascii=False),
        )
