"""Tests for durable wait-and-resume run state (issue #29)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nanobot.agent.loop import _extract_ask_user_question_id, _extract_tool_names
from nanobot.agent.tools.ask_user import AskUserTool
from nanobot.agent.tools.waiting_runs import WaitingRunStore, resume_source_digest


def _owner(**kw) -> dict:
    base = dict(
        question_id="q1", sender_id="u1", channel="telegram", chat_id="11",
        session_key="telegram:11", expires_at_ms=int(__import__("time").time() * 1000) + 60_000,
    )
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# store state machine
# ---------------------------------------------------------------------------


def test_create_augment_and_restart(tmp_path) -> None:
    store = WaitingRunStore(tmp_path)
    run = store.create(**_owner())
    assert run.status == "waiting"
    assert run.run_id == "q1"

    assert store.augment("q1", note="done step 1", budgets={"max_iterations": 4},
                         completed_tool_names=["exec", "search"]) is True
    fresh = WaitingRunStore(tmp_path)
    loaded = fresh.get("q1")
    assert loaded is not None
    assert loaded.note == "done step 1"
    assert loaded.budgets == {"max_iterations": 4}
    assert loaded.completed_tool_names == ["exec", "search"]


def test_claim_resume_exactly_once_and_dedup(tmp_path) -> None:
    store = WaitingRunStore(tmp_path)
    store.create(**_owner())
    key = resume_source_digest("telegram", "11", "k", 5, "answer")

    claimed, run = store.claim_resume("q1", session_key="telegram:11", sender_id="u1", source_key=key)
    assert claimed is True and run.status == "resuming"
    # Same inbound delivery replayed.
    again, _ = store.claim_resume("q1", session_key="telegram:11", sender_id="u1", source_key=key)
    assert again is False
    # Different delivery while already resuming.
    again2, _ = store.claim_resume("q1", session_key="telegram:11", sender_id="u1", source_key="other")
    assert again2 is False


def test_claim_owner_and_expiry(tmp_path) -> None:
    store = WaitingRunStore(tmp_path)
    store.create(**_owner())
    wrong, run = store.claim_resume("q1", session_key="telegram:22", sender_id="u2", source_key="a")
    assert wrong is False
    assert run.status == "waiting"  # untouched

    store2 = WaitingRunStore(tmp_path)
    store2.create(**_owner(expires_at_ms=1, question_id="qx"))
    claimed, run = store2.claim_resume("qx", session_key="telegram:11", sender_id="u1", source_key="b")
    assert claimed is False
    assert run.status == "expired"


def test_complete_cancel_and_corrupt(tmp_path) -> None:
    store = WaitingRunStore(tmp_path)
    store.create(**_owner())
    store.claim_resume("q1", session_key="telegram:11", sender_id="u1", source_key="c")
    assert store.mark_complete("q1", ok=True) is True
    assert store.get("q1").status == "completed"
    assert store.mark_complete("q1") is False  # not resuming anymore

    store.create(**_owner(question_id="q2"))
    assert store.cancel("q2", session_key="telegram:11") is True
    assert store.get("q2").status == "cancelled"
    assert store.cancel("q2", session_key="telegram:11") is False
    assert store.cancel("q2", session_key="other") is False

    path = tmp_path / "waiting_runs.json"
    path.write_text("{broken", encoding="utf-8")
    broken_store = WaitingRunStore(tmp_path)
    assert broken_store.list_for_owner("x") == []
    assert list(tmp_path.glob("waiting_runs.json.corrupt-*"))


# ---------------------------------------------------------------------------
# ask_user creates the envelope before rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_user_persists_waiting_envelope_before_send(tmp_path) -> None:
    from nanobot.agent.tools.waiting_runs import WaitingRunStore

    sent: list = []
    tool = AskUserTool(workspace=tmp_path, send_callback=sent.append)
    tool.set_context(SimpleNamespace(
        channel="telegram", chat_id="11", session_key="telegram:11", sender_id="u1", metadata={},
    ))

    result = await tool.execute(question="Which one?", options=["a", "b"])

    assert result.status == "ask_user"
    run = WaitingRunStore(tmp_path).get(result.data["question_id"])
    assert run is not None
    assert run.status == "waiting"
    assert len(sent) == 1  # envelope persisted before the outbound render


# ---------------------------------------------------------------------------
# pure helpers for continuation note extraction
# ---------------------------------------------------------------------------


def test_extract_question_id_and_tool_names() -> None:
    messages = [
        {"role": "assistant", "tool_calls": [{"name": "exec", "id": "1"}]},
        {"role": "tool", "name": "exec", "content": "Question abc123def sent - awaiting your answer."},
        {"role": "assistant", "tool_calls": [{"name": "search", "id": "2"}]},
    ]
    assert _extract_ask_user_question_id(messages) == "abc123def"
    assert _extract_tool_names(messages) == ["exec", "search"]


# ---------------------------------------------------------------------------
# loop resume claim + injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_resume_claims_once_and_injects_envelope(tmp_path) -> None:
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    store = WaitingRunStore(tmp_path)
    store.create(**_owner())
    store.augment("q1", note="did the scan", budgets={"max_iterations": 5},
                  completed_tool_names=["exec"])

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    workspace = MagicMock()
    workspace.__truediv__ = MagicMock(return_value=MagicMock())
    with patch("nanobot.agent.loop.ContextBuilder"), \
         patch("nanobot.agent.loop.SessionManager"), \
         patch("nanobot.agent.loop.SubagentManager"):
        loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace)
    loop._waiting_runs = store

    messages: list[dict] = [{"role": "user", "content": "q answer"}]
    key = resume_source_digest("telegram", "11", "k", 9, "answer")
    run_id, budgets = loop._fire_waiting_resume(
        session_key="telegram:11", sender_id="u1", question_id="q1",
        answer_label="a", source_key=key, messages=messages,
    )

    assert run_id == "q1"
    assert budgets == {"max_iterations": 5}
    assert any("Resuming run q1" in m.get("content", "") for m in messages)
    assert any("did the scan" in m.get("content", "") for m in messages)
    # Exactly-once: same source replayed does not inject again.
    messages2: list[dict] = [{"role": "user", "content": "q answer"}]
    again_id, _ = loop._fire_waiting_resume(
        session_key="telegram:11", sender_id="u1", question_id="q1",
        answer_label="a", source_key=key, messages=messages2,
    )
    assert again_id is None
    assert not any("Resuming run q1" in m.get("content", "") for m in messages2)
