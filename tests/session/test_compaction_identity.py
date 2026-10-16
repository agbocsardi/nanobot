"""Only owning transcripts assign identities; derived views preserve them."""

from copy import deepcopy

import pytest

from nanobot.session.compaction import CompactionEngine, CompactionError, ensure_ids, project
from tests.session.test_compaction import SummaryProvider, budget


def test_entry_ids_survive_slicing_and_reprojection():
    messages = [{"role": "user", "content": f"record {i}"} for i in range(5)]
    ensure_ids(messages)
    original_ids = [m["_entry_id"] for m in messages]
    checkpoint = {"version": 1, "summary": "goal", "first_kept_entry_id": original_ids[2],
                  "source_end_id": original_ids[-1]}
    sliced = messages[1:]
    ensure_ids(sliced)  # Existing identities are immutable, regardless of position.
    assert [m["_entry_id"] for m in messages] == original_ids
    assert project(messages, checkpoint) == project(sliced, checkpoint)
    assert [m["_entry_id"] for m in project(sliced, checkpoint)[1:]] == original_ids[2:]


def test_missing_identity_at_checkpoint_reference_fails_specifically_without_minting():
    messages = [{"role": "user", "content": f"record {i}"} for i in range(4)]
    ensure_ids(messages)
    checkpoint = {"version": 1, "summary": "goal", "first_kept_entry_id": messages[2]["_entry_id"]}
    messages[2].pop("_entry_id")
    original = deepcopy(messages)
    with pytest.raises(CompactionError, match="Missing stable entry id at transcript index 2"):
        project(messages, checkpoint)
    assert messages == original


async def test_preparation_refuses_to_assign_ids_on_a_derived_view():
    provider = SummaryProvider()
    messages = [{"role": "user", "content": "original request"},
                {"role": "assistant", "content": "progress"}]
    with pytest.raises(CompactionError, match="Missing stable entry id"):
        await CompactionEngine(provider, budget(provider)).prepare(messages)
    assert all("_entry_id" not in m for m in messages)
    assert not provider.requests


def test_identical_legacy_messages_are_distinct_entries():
    messages = [{"role": "user", "content": "continue"} for _ in range(2)]
    ensure_ids(messages)
    assert messages[0]["_entry_id"] != messages[1]["_entry_id"]
