"""Retired config compatibility versus the still-live legacy browsing API."""

from inspect import signature
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import AgentDefaults
from nanobot.providers.base import GenerationSettings, LLMResponse
from nanobot.session.manager import Session

DEFAULT_MAX_MESSAGES = 120


def _make_loop(tmp_path: Path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=4096)
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="ok"))
    return AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")


def _populated_session(n: int) -> Session:
    session = Session(key="test:populated")
    for i in range(n):
        session.add_message("user", f"msg-{i}")
        session.add_message("assistant", f"reply-{i}")
    return session


@pytest.mark.parametrize("limit", [0, 1, 6, 120])
def test_legacy_config_count_has_no_effect_on_projected_replay(limit):
    defaults = AgentDefaults.model_validate({"maxMessages": limit})
    session = _populated_session(80)
    assert len(session.get_history(projected=True, max_messages=defaults.max_messages)) == 160


class TestGetHistoryWithMaxMessages:
    """Legacy browsing without checkpoints still has a real count consumer."""

    def test_default_uses_builtin_limit(self):
        assert len(_populated_session(80).get_history()) <= DEFAULT_MAX_MESSAGES

    def test_explicit_max_messages_limits_output(self):
        assert len(_populated_session(40).get_history(max_messages=20)) <= 20

    def test_max_messages_starts_at_user_turn(self):
        assert _populated_session(30).get_history(max_messages=25)[0]["role"] == "user"

    def test_max_messages_zero_uses_builtin_limit(self):
        assert len(_populated_session(80).get_history(max_messages=0)) <= DEFAULT_MAX_MESSAGES

    def test_small_session_unaffected(self):
        assert len(_populated_session(5).get_history(max_messages=25)) == 10


class TestProjectedReplayIntegration:
    async def test_main_path_has_no_ignored_limit_plumbing(self, tmp_path):
        loop = _make_loop(tmp_path)
        assert "max_messages" not in signature(AgentLoop.__init__).parameters
        assert not hasattr(loop, "_max_messages")
        loop.consolidator.maybe_consolidate_by_tokens = AsyncMock()
        session = loop.sessions.get_or_create("cli:test")
        with patch.object(session, "get_history", wraps=session.get_history) as history:
            assert await loop._process_message(InboundMessage(
                channel="cli", sender_id="user", chat_id="test", content="new question",
            ))
        assert history.call_args.kwargs == {
            "include_timestamps": True, "projected": True, "include_entry_ids": True,
        }
        loop.consolidator.maybe_consolidate_by_tokens.assert_awaited_once_with(session)

    async def test_projected_history_contains_the_full_older_turn(self, tmp_path):
        loop = _make_loop(tmp_path)
        loop.tools.get_definitions = MagicMock(return_value=[])
        session = loop.sessions.get_or_create("cli:test")
        for i in range(20):
            session.add_message("user", f"older task {i}")
            session.add_message("assistant", f"older answer {i}")
        assert await loop._process_message(InboundMessage(
            channel="cli", sender_id="user", chat_id="test", content="new question",
        ))
        sent = str(loop.provider.chat_with_retry.await_args.kwargs["messages"])
        assert "older task 0" in sent
        assert "new question" in sent

    async def test_checkpoint_is_the_only_way_to_hide_more_context(self, tmp_path):
        from tests.session.test_compaction_retention import checkpoint

        loop = _make_loop(tmp_path)
        session = loop.sessions.get_or_create("cli:test")
        session.add_message("user", "older hidden work")
        session.add_message("assistant", "older answer")
        session.add_message("user", "current goal")
        checkpoint(session, 2)
        assert "older hidden work" not in str(session.get_history(max_messages=1))
        assert len(session.get_history(max_messages=1)) == 2  # Summary plus retained request.


class TestSchemaConfig:
    def test_schema_default(self):
        assert AgentDefaults().max_messages == DEFAULT_MAX_MESSAGES
        assert "ignored" in AgentDefaults.model_fields["max_messages"].description

    def test_schema_accepts_zero_as_legacy_value(self):
        assert AgentDefaults(max_messages=0).max_messages == 0

    def test_schema_accepts_positive_value(self):
        defaults = AgentDefaults.model_validate({"maxMessages": 25})
        assert defaults.max_messages == 25
        assert defaults.model_dump(by_alias=True)["maxMessages"] == 25

    def test_schema_rejects_negative(self):
        with pytest.raises(ValueError):
            AgentDefaults(max_messages=-1)
