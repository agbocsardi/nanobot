"""Invalid continuity checkpoints recover original history durably and loudly."""

from copy import deepcopy

import pytest
from loguru import logger

from nanobot.session.compaction import CHECKPOINTS
from nanobot.session.manager import SessionManager
from tests.session.test_compaction_retention import checkpoint


@pytest.mark.parametrize("damage", ["missing_start", "missing_end", "old_version", "collection", "entry"])
def test_invalid_checkpoint_is_removed_from_memory_and_disk(tmp_path, damage):
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("test:recovery")
    for i in range(6):
        session.add_message("user", f"original {i}")
    checkpoint(session, 4)
    if damage == "missing_start":
        session.metadata[CHECKPOINTS][-1]["first_kept_entry_id"] = "missing"
    elif damage == "missing_end":
        session.metadata[CHECKPOINTS][-1]["source_end_id"] = "missing"
    elif damage == "old_version":
        session.metadata[CHECKPOINTS][-1]["version"] = 0
    elif damage == "collection":
        session.metadata[CHECKPOINTS] = {"invalid": True}
    else:
        session.metadata[CHECKPOINTS] = ["invalid"]
    session.last_consolidated = 2
    session.metadata["_last_summary"] = {"text": "must not survive recovery"}
    sessions.save(session)
    originals = deepcopy(session.messages)
    sessions.invalidate(session.key)
    errors = []
    handle = logger.add(lambda message: errors.append(message.record), level="ERROR")
    try:
        loaded = sessions.get_or_create(session.key)
    finally:
        logger.remove(handle)
    assert loaded.messages == originals
    assert CHECKPOINTS not in loaded.metadata
    assert "_last_summary" not in loaded.metadata
    assert loaded.last_consolidated == 0
    assert [m["content"] for m in loaded.get_history(projected=True)] == [f"original {i}" for i in range(6)]
    assert any(r["level"].name == "ERROR" and "Invalid continuity checkpoint" in r["message"] for r in errors)
    sessions.invalidate(session.key)
    assert CHECKPOINTS not in sessions.get_or_create(session.key).metadata
    assert CHECKPOINTS not in sessions.read_session_file(session.key)["metadata"]


def test_valid_checkpoint_load_is_untouched(tmp_path):
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("test:valid")
    for i in range(6):
        session.add_message("user", f"original {i}")
    checkpoint(session, 4)
    sessions.save(session)
    expected = deepcopy(session.metadata)
    path = sessions._get_session_path(session.key)
    original_bytes = path.read_bytes()
    sessions.invalidate(session.key)
    loaded = sessions.get_or_create(session.key)
    assert loaded.metadata == expected
    assert path.read_bytes() == original_bytes
    assert loaded.get_history(projected=True) == session.get_history(projected=True)
