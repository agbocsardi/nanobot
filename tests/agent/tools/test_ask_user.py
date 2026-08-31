"""Tests for the durable ask_user tool and its pending-question store."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobot.agent.tools.ask_user import (
    ASK_USER_STATUS,
    AskUserTool,
    PendingQuestionStore,
    parse_question_callback,
    question_callback_value,
)
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import OutboundMessage
from nanobot.providers.base import LLMResponse, ToolCallRequest
from tests.harness.helpers import run_script


def _ctx(tmp_path: Path, **overrides) -> SimpleNamespace:
    sender_id = overrides.pop("sender_id", "u1")
    ctx = SimpleNamespace(
        channel="telegram",
        chat_id="123",
        session_key="telegram:123",
        sender_id=sender_id,
        metadata={},
        workspace=str(tmp_path),
        bus=SimpleNamespace(publish_outbound=lambda msg: None),
    )
    return SimpleNamespace(**{**dict(vars(ctx)), **overrides}, sender_id=sender_id)


def _tool(tmp_path: Path, *, send: list | None = None) -> AskUserTool:
    sent = [] if send is None else send
    tool = AskUserTool(workspace=tmp_path, send_callback=sent.append)
    tool.set_context(
        SimpleNamespace(
            channel="telegram",
            chat_id="123",
            session_key="telegram:123",
            sender_id="u1",
            metadata={},
        )
    )
    return tool


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


def test_create_persists_pending_question(tmp_path) -> None:
    store = PendingQuestionStore(tmp_path)
    question = store.create(
        prompt="pick one",
        option_labels=["yes", "no", "maybe"],
        channel="telegram",
        chat_id="123",
        session_key="telegram:123",
        sender_id="u1",
    )

    assert question.status == "pending"
    assert question.question_id
    assert len(question.options) == 3
    assert [o["id"] for o in question.options] == ["a", "b", "c"]
    assert question.expires_at_ms > question.created_at_ms
    assert len(question_callback_value(question.question_id, "a").encode()) <= 64

    # Durability across store instances (= gateway restart).
    reloaded = PendingQuestionStore(tmp_path)
    from_disk = reloaded.claim(
        question.question_id, "a",
        channel="telegram", chat_id="123", sender_id="u1",
    )
    assert from_disk[0] is True


def test_claim_is_single_use_and_records_answer(tmp_path) -> None:
    store = PendingQuestionStore(tmp_path)
    question = store.create(
        prompt="decide", option_labels=["go", "stop"],
        channel="telegram", chat_id="123", session_key="telegram:123", sender_id="u1",
    )

    claimed, q = store.claim(
        question.question_id, "a", channel="telegram", chat_id="123", sender_id="u1"
    )

    assert claimed is True
    assert q.status == "answered"
    assert q.selected_option_id == "a"
    assert q.answered_by == "u1"
    assert q.answered_at_ms is not None
    # Duplicate tap.
    again, _ = store.claim(
        question.question_id, "b", channel="telegram", chat_id="123", sender_id="u1"
    )
    assert again is False


def test_claim_rejects_cross_chat_and_cross_user(tmp_path) -> None:
    store = PendingQuestionStore(tmp_path)
    question = store.create(
        prompt="decide", option_labels=["go", "stop"],
        channel="telegram", chat_id="123", session_key="telegram:123", sender_id="u1",
    )

    wrong_chat, _ = store.claim(
        question.question_id, "a", channel="telegram", chat_id="999", sender_id="u1"
    )
    wrong_user, _ = store.claim(
        question.question_id, "a", channel="telegram", chat_id="123", sender_id="u2"
    )
    unknown_option, _ = store.claim(
        question.question_id, "z", channel="telegram", chat_id="123", sender_id="u1"
    )

    assert wrong_chat is False
    assert wrong_user is False
    assert unknown_option is False
    assert store.claim(
        question.question_id, "a", channel="telegram", chat_id="123", sender_id="u1"
    )[0] is True  # still claimable by the right user


def test_expired_question_rejects_and_marks_expired(tmp_path) -> None:
    store = PendingQuestionStore(tmp_path)
    question = store.create(
        prompt="decide", option_labels=["go", "stop"],
        channel="telegram", chat_id="123", session_key="telegram:123", sender_id="u1",
        expires_at_ms=1,  # long in the past
    )

    claimed, q = store.claim(
        question.question_id, "a", channel="telegram", chat_id="123", sender_id="u1",
    )

    assert claimed is False
    assert q.status == "expired"


def test_corrupt_store_degrades_to_empty(tmp_path) -> None:
    path = tmp_path / "pending_questions.json"
    path.write_text("{broken", encoding="utf-8")

    store = PendingQuestionStore(tmp_path)
    assert store.claim("missing", "a", channel="x", chat_id="y", sender_id="z") == (False, None)

    # The next write replaces the corrupt file atomically; no error loop.
    question = store.create(
        prompt="decide", option_labels=["go", "stop"],
        channel="telegram", chat_id="123", session_key="telegram:123", sender_id="u1",
    )
    assert store.claim(
        question.question_id, "a", channel="telegram", chat_id="123", sender_id="u1"
    )[0] is True


# ---------------------------------------------------------------------------
# tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_sends_question_and_returns_terminal_status(tmp_path) -> None:
    sent: list[OutboundMessage] = []
    tool = _tool(tmp_path, send=sent)

    result = await tool.execute(
        question="Which project first?",
        options=["cron", "memory", "telegram"],
    )

    assert result.status == ASK_USER_STATUS
    assert result.data["question_id"]
    assert len(result.data["options"]) == 3
    assert len(sent) == 1
    msg = sent[0]
    assert "1. cron" in msg.content
    assert "2. memory" in msg.content
    assert len(msg.buttons) == 3
    first = msg.buttons[0][0]
    assert isinstance(first, dict)
    assert first["label"] == "cron"
    assert first["callback_value"].startswith("pq:")
    qid, opt = parse_question_callback(first["callback_value"])
    assert qid == result.data["question_id"]
    assert opt == "a"
    assert PendingQuestionStore(tmp_path).claim(
        qid, opt, channel="telegram", chat_id="123", sender_id="u1"
    )[0] is True


@pytest.mark.asyncio
async def test_execute_requires_sender_and_outbound(tmp_path) -> None:
    tool = AskUserTool(workspace=tmp_path, send_callback=lambda msg: None)
    tool.set_context(
        SimpleNamespace(channel="telegram", chat_id="123", session_key="telegram:123",
                        sender_id="", metadata={})
    )

    result = await tool.execute(question="q?", options=["a", "b"])

    assert result.status == "retryable_error"
    assert "sender" in str(result)


@pytest.mark.asyncio
async def test_execute_refuses_credentials(tmp_path) -> None:
    tool = _tool(tmp_path)
    result = await tool.execute(
        question="What is your password?", options=["tell", "skip"]
    )
    assert result.status == "policy_block"


@pytest.mark.asyncio
async def test_execute_validates_option_bounds_and_uniqueness(tmp_path) -> None:
    tool = _tool(tmp_path)
    assert (await tool.execute(question="q", options=["only"])).status == "retryable_error"
    assert (await tool.execute(question="q", options=["a", "b", "c", "d", "e"])).status == "retryable_error"
    assert (await tool.execute(question="q", options=["dup", "dup"])).status == "retryable_error"


@pytest.mark.asyncio
async def test_ask_user_ends_turn_with_ask_user_stop_reason(tmp_path) -> None:
    sent: list[OutboundMessage] = []
    tools = ToolRegistry()
    tools.register(_tool(tmp_path, send=sent))

    result, _provider = await run_script(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(id="1", name="ask_user", arguments={
                        "question": "Which one?",
                        "options": ["left", "right"],
                    })
                ],
            ),
            LLMResponse(content="Awaiting your answer.", tool_calls=[]),
        ],
        tools=tools,
        max_iterations=3,
    )

    assert result.stop_reason == "ask_user"
    assert len(sent) == 1  # no further side effects after the question
    assert result.messages[-1]["content"] == "Awaiting your answer."



