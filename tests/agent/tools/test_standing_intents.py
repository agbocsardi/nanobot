"""Tests for standing intents: matching core, store, tool, loop hook (#28)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nanobot.agent.tools.standing_intents import (
    IntentTool,
    StandingIntentStore,
    match_trigger_groups,
    normalize_text,
    source_digest,
)

# ---------------------------------------------------------------------------
# pure matching core
# ---------------------------------------------------------------------------


def test_normalize_text_case_punctuation_and_whitespace() -> None:
    assert normalize_text("  Call Me! later..  ") == "call me later"
    assert normalize_text("") == ""
    assert normalize_text("C++ and R&D") == "c and r d"


def test_match_or_of_and_groups() -> None:
    text = normalize_text("remind me about the vet appointment")
    assert match_trigger_groups([["vet"]], text) is True
    assert match_trigger_groups([["vet", "appointment"]], text) is True  # AND
    assert match_trigger_groups([["gym"], ["vet"]], text) is True  # OR
    assert match_trigger_groups([["gym"], ["dog"]], text) is False
    # contiguous phrase boundary: "vet appointment" matches, "appointment vet" does not
    assert match_trigger_groups([["appointment", "vet"]], text) is False
    assert match_trigger_groups([["vet", "other"]], text) is False


def test_match_phrase_boundaries_avoid_partial_words() -> None:
    assert match_trigger_groups([["cat"]], normalize_text("my cat is here")) is True
    assert match_trigger_groups([["cat"]], normalize_text("a category of things")) is False
    assert match_trigger_groups([["build"]], normalize_text("we built a shed")) is False
    assert match_trigger_groups([["new", "home"]], normalize_text("rented a new home today")) is True


def test_match_empty_triggers_never_fire() -> None:
    assert match_trigger_groups([[]], normalize_text("anything")) is False
    assert match_trigger_groups([["  "]], normalize_text("anything")) is False
    assert match_trigger_groups([["x"]], "") is False


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


def _owner(store, **kw) -> dict:
    base = dict(sender_id="u1", channel="telegram", chat_id="11",
                session_key="telegram:11", reminder="water the plants",
                trigger_groups=[["plants"]])
    base.update(kw)
    return base


def test_add_list_cancel_and_restart(tmp_path) -> None:
    store = StandingIntentStore(tmp_path)
    intent = store.add(**_owner(store))
    assert intent.status == "active"

    fresh = StandingIntentStore(tmp_path)
    listed = fresh.list_for_owner("telegram:11")
    assert [i.intent_id for i in listed] == [intent.intent_id]
    assert fresh.cancel(intent.intent_id, session_key="telegram:11") is True
    assert fresh.cancel(intent.intent_id, session_key="telegram:11") is False  # not active
    # Ownership guard.
    assert fresh.cancel(intent.intent_id, session_key="other") is False


def test_owner_isolation(tmp_path) -> None:
    store = StandingIntentStore(tmp_path)
    a = store.add(**_owner(store))
    b = store.add(**_owner(store, session_key="telegram:22", sender_id="u2"))
    assert [i.intent_id for i in store.list_for_owner("telegram:11", sender_id="u1")] == [a.intent_id]
    assert [i.intent_id for i in store.list_for_owner("telegram:22", sender_id="u2")] == [b.intent_id]


def test_fire_once_durable(tmp_path) -> None:
    store = StandingIntentStore(tmp_path)
    intent = store.add(**_owner(store))
    now = int(__import__("time").time() * 1000)
    fired = store.match_and_fire(session_key="telegram:11", sender_id="u1",
                                 source_key="s1", text="please water the plants", now_ms=now)
    assert [i.intent_id for i in fired] == [intent.intent_id]
    # Fired is durable store truth: even a fresh process cannot re-fire.
    fresh = StandingIntentStore(tmp_path)  # empty in-memory dedup window
    assert fresh.match_and_fire(session_key="telegram:11", sender_id="u1",
                                source_key="s2", text="water the plants", now_ms=now + 1) == []
    assert fresh.list_for_owner("telegram:11")[0].status == "fired"


def test_fire_dedup_and_fire_once(tmp_path) -> None:
    store = StandingIntentStore(tmp_path)
    store.add(**_owner(store))
    now = int(__import__("time").time() * 1000)
    key = source_digest("telegram", "11", "k", 42, "water the plants")
    assert len(store.match_and_fire(session_key="telegram:11", sender_id="u1",
                                    source_key=key, text="water the plants", now_ms=now)) == 1
    # Same inbound update replayed: deduped, no double fire.
    assert store.match_and_fire(session_key="telegram:11", sender_id="u1",
                                source_key=key, text="water the plants", now_ms=now) == []
    # Different user/session never fires someone else's intent.
    assert store.match_and_fire(session_key="telegram:11", sender_id="u9",
                                source_key="other", text="plants", now_ms=now) == []


def test_corrupt_store_degrades_to_empty(tmp_path) -> None:
    path = tmp_path / "standing_intents.json"
    path.write_text("{broken", encoding="utf-8")
    store = StandingIntentStore(tmp_path)
    assert store.list_for_owner("x") == []
    # Next write replaces the corrupt file atomically.
    intent = store.add(**_owner(store))
    fresh = StandingIntentStore(tmp_path)
    assert [i.intent_id for i in fresh.list_for_owner("telegram:11")] == [intent.intent_id]


# ---------------------------------------------------------------------------
# tool
# ---------------------------------------------------------------------------

def _tool(tmp_path: Path) -> IntentTool:
    tool = IntentTool(workspace=tmp_path)
    tool.set_context(SimpleNamespace(
        channel="telegram", chat_id="11", session_key="telegram:11",
        sender_id="u1", metadata={},
    ))
    return tool


@pytest.mark.asyncio
async def test_tool_add_list_cancel_roundtrip(tmp_path) -> None:
    tool = _tool(tmp_path)
    added = await tool.execute(
        action="add", reminder="water the plants",
        trigger_terms=["plants", "garden"],
    )
    assert added.status == "success"
    intent_id = added.data["intent_id"]

    listed = await tool.execute(action="list")
    assert intent_id in listed.data["intents"][0]["intent_id"] or intent_id in str(listed)
    assert "water the plants" in str(listed)

    cancelled = await tool.execute(action="cancel", intent_id=intent_id)
    assert "Cancelled" in str(cancelled)


@pytest.mark.asyncio
async def test_tool_validation(tmp_path) -> None:
    tool = _tool(tmp_path)
    assert (await tool.execute(action="add", reminder="r")).status == "retryable_error"  # no terms
    assert (await tool.execute(action="add", trigger_terms=["t"])).status == "retryable_error"  # no reminder


@pytest.mark.asyncio
async def test_tool_requires_sender(tmp_path) -> None:
    tool = IntentTool(workspace=tmp_path)
    tool.set_context(SimpleNamespace(channel="telegram", chat_id="11", session_key="telegram:11",
                                     sender_id="", metadata={}))
    result = await tool.execute(action="add", reminder="r", trigger_terms=["t"])
    assert result.status == "retryable_error"
    assert "known user" in str(result)


# ---------------------------------------------------------------------------
# loop hook
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_loop_fire_helpers_injects_system_reminder(tmp_path) -> None:
    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.tools.standing_intents import StandingIntentStore
    from nanobot.bus.queue import MessageBus

    store = StandingIntentStore(tmp_path)
    intent = store.add(
        sender_id="u1", channel="telegram", chat_id="11", session_key="telegram:11",
        trigger_groups=[["plants"]], reminder="water the plants",
    )
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    workspace = MagicMock()
    workspace.__truediv__ = MagicMock(return_value=MagicMock())
    with patch("nanobot.agent.loop.ContextBuilder"),          patch("nanobot.agent.loop.SessionManager"),          patch("nanobot.agent.loop.SubagentManager"):
        loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace)
    loop._standing_intents = store  # real durable store under a real tmp workspace

    messages: list[dict] = [{"role": "user", "content": "please water the plants"}]
    loop._fire_standing_intents(
        channel="telegram", chat_id="11", session_key="telegram:11", sender_id="u1",
        source_key="k1", text="please water the plants", messages=messages,
    )

    assert any(
        m.get("role") == "system" and f"[Standing reminder fired (id: {intent.intent_id})]" in m.get("content", "")
        for m in messages
    )
    assert messages[-1]["role"] == "user"  # user content untouched
    # A second processed copy of the same update cannot fire again.
    messages2: list[dict] = [{"role": "user", "content": "please water the plants"}]
    loop._fire_standing_intents(
        channel="telegram", chat_id="11", session_key="telegram:11", sender_id="u1",
        source_key="k1", text="please water the plants", messages=messages2,
    )
    assert not any(m.get("role") == "system" for m in messages2)
