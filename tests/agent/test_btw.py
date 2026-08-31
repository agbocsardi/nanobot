"""Tests for ephemeral /btw side questions (issue #30)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse


class _Session:
    def __init__(self, key: str) -> None:
        import time

        self.key = key
        self.metadata: dict = {}
        self.messages: list = []
        self.last_consolidated = 0
        self.updated_at = time.time()
        self.channel = "telegram"
        self.created_at = time.time()

    def get_history(self, *args, **kwargs):
        return []

    def enforce_file_cap(self, **kwargs):
        return None


def _make_loop(tmp_path: Path, provider) -> object:
    from nanobot.agent.loop import AgentLoop

    fake_session = _Session("telegram:1")
    fake_ctx = MagicMock()
    fake_ctx.build_messages.return_value = [{"role": "user", "content": "btw"}]
    fake_ctx.last_context_report = {}

    with patch("nanobot.agent.loop.ContextBuilder", return_value=fake_ctx), patch(
        "nanobot.agent.loop.SessionManager"
    ), patch("nanobot.agent.loop.SubagentManager"):
        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=tmp_path,
            model="test-model",
            context_window_tokens=64 * 1024,
            max_tool_result_chars=8000,
        )
    loop.sessions = SimpleNamespace(get_or_create=lambda key: fake_session)
    loop._refresh_provider_snapshot = lambda: None
    loop._btw_wall_timeout_s = 5.0
    loop._btw_llm_timeout_s = 5
    return loop


def _request_capture():
    """Return (provider, requests) with a capturing chat_with_retry."""
    requests: list[dict] = []

    async def chat_with_retry(**kwargs):
        requests.append(kwargs)
        return LLMResponse(content="the square root of 144 is 12", tool_calls=[])

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.chat_with_retry = chat_with_retry
    provider.supports_progress_deltas = False
    return provider, requests


def _btw_msg(text: str, *, sender: str = "u1") -> InboundMessage:
    return InboundMessage(channel="telegram", sender_id=sender, chat_id="11", content=text)


@pytest.mark.asyncio
async def test_btw_answers_without_tools_and_persists_nothing(tmp_path) -> None:
    provider, requests = _request_capture()
    loop = _make_loop(tmp_path, provider)

    await loop._handle_btw(_btw_msg("/btw quick math: sqrt(144)"), "telegram:1")

    out = await asyncio.wait_for(loop.bus.consume_outbound(), timeout=2.0)
    assert "12" in out.content
    assert out.metadata.get("_btw") is True
    # The model structurally got NO tools: side-effectful tools are unreachable.
    assert requests and requests[0].get("tools") == []
    # Non-persistence: nothing written under the workspace.
    assert not (tmp_path / "memory").exists()
    assert not (tmp_path / "history.jsonl").exists()
    assert not (tmp_path / "standing_intents.json").exists()
    assert not (tmp_path / "waiting_runs.json").exists()


@pytest.mark.asyncio
async def test_btw_empty_input_usage_and_no_model_call(tmp_path) -> None:
    provider, requests = _request_capture()
    loop = _make_loop(tmp_path, provider)

    await loop._handle_btw(_btw_msg("/btw"), "telegram:1")

    out = await asyncio.wait_for(loop.bus.consume_outbound(), timeout=2.0)
    assert "Usage: `/btw" in out.content
    assert requests == []  # no inference for empty input


@pytest.mark.asyncio
async def test_btw_provider_failure_is_isolated(tmp_path) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    async def fail(**kwargs):
        raise RuntimeError("provider boom")

    provider.chat_with_retry = fail
    provider.supports_progress_deltas = False
    loop = _make_loop(tmp_path, provider)

    await loop._handle_btw(_btw_msg("/btw anything"), "telegram:1")

    out = await asyncio.wait_for(loop.bus.consume_outbound(), timeout=2.0)
    assert "your active run is unaffected" in out.content


@pytest.mark.asyncio
async def test_btw_concurrency_is_bounded(tmp_path) -> None:
    gate = asyncio.Event()

    async def slow(**kwargs):
        await gate.wait()
        return LLMResponse(content="done", tool_calls=[])

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.chat_with_retry = slow
    provider.supports_progress_deltas = False
    loop = _make_loop(tmp_path, provider)

    first = asyncio.create_task(loop._handle_btw(_btw_msg("/btw q1"), "telegram:1"))
    await asyncio.sleep(0.02)
    second = asyncio.create_task(loop._handle_btw(_btw_msg("/btw q2"), "telegram:1"))
    await asyncio.sleep(0.02)
    third = asyncio.create_task(loop._handle_btw(_btw_msg("/btw q3"), "telegram:1"))
    busy = await asyncio.wait_for(loop.bus.consume_outbound(), timeout=2.0)
    # Third side question is rejected cleanly when both slots are busy.
    assert "Busy" in busy.content
    gate.set()
    await asyncio.gather(first, second, third, return_exceptions=True)


def test_btw_request_detection() -> None:
    from nanobot.agent.loop import AgentLoop

    assert AgentLoop._is_btw_request("/btw")
    assert AgentLoop._is_btw_request("/btw add eggs to the list")
    assert AgentLoop._is_btw_request("/btw@mybot hello")
    assert not AgentLoop._is_btw_request("/task x")
    assert not AgentLoop._is_btw_request("/btwxyz")
