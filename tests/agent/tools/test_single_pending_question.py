"""One durable pending structured question per session (issue #35)."""

import asyncio

import pytest

from nanobot.agent.tools.ask_user import PendingQuestionStore, QuestionAlreadyPendingError
from tests.agent.tools.test_ask_user import _tool


@pytest.mark.asyncio
async def test_second_ask_is_blocked_even_across_tool_instances(tmp_path):
    sent = []
    first = _tool(tmp_path, send=sent)
    second = _tool(tmp_path, send=sent)
    results = await asyncio.gather(
        first.execute(question="Choose?", options=["Yes", "No"]),
        second.execute(question="Again?", options=["Yes", "No"]),
    )
    assert sum(result.status == "ask_user" for result in results) == 1
    assert len(sent) == 1
    assert any("already pending" in result for result in results)


def test_pending_guard_allows_other_sessions_answered_and_expired(tmp_path, monkeypatch):
    now = 100_000
    monkeypatch.setattr("nanobot.agent.tools.ask_user._now_ms", lambda: now)
    store = PendingQuestionStore(tmp_path)
    scope = dict(channel="telegram", chat_id="123", sender_id="u1")

    def create(session="telegram:123"):
        return store.create(
            prompt="Choose?", option_labels=["Yes", "No"], session_key=session,
            expires_at_ms=now + 1000, **scope,
        )

    first = create()
    with pytest.raises(QuestionAlreadyPendingError):
        create()
    assert create("telegram:123:topic:2").question_id != first.question_id
    assert store.claim(first.question_id, "a", **scope)[0]
    replacement = create()
    now += 1001
    assert create().question_id != replacement.question_id
