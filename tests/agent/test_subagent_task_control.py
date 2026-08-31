"""Manager-level tests for the durable task-control surface (issue #27)."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from nanobot.agent.subagent import SubagentManager
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse


class _ScriptedProvider:
    """Minimal offline provider for SubagentManager tests."""

    supports_progress_deltas = False

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self._fail = False

    def get_default_model(self) -> str:
        return "test-model"

    def set_fail(self, fail: bool = True) -> None:
        self._fail = fail

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        self.requests.append(kwargs)
        if self._fail:
            raise RuntimeError("fixture provider failure")
        if not self.responses:
            raise AssertionError("unexpected provider request")
        return self.responses.pop(0)

    def __getattr__(self, item):
        if item.startswith("supports_"):
            return False
        raise AttributeError(item)


class _Preset:
    def __init__(self, provider, model="preset-model"):
        self.provider = provider
        self.model = model
        self.context_window_tokens = 8000

    def to_generation_settings(self):
        from nanobot.providers.base import GenerationSettings
        return GenerationSettings()


def _manager(tmp_path: Path, provider=None, **kw) -> SubagentManager:
    provider = provider if provider is not None else _ScriptedProvider([])
    defaults = dict(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        model="test-model",
        max_tool_result_chars=16_000,
    )
    defaults.setdefault("presets", {"fast": _Preset(provider)})
    defaults.setdefault(
        "preset_snapshot_loader",
        lambda name: __import__("types").SimpleNamespace(
            provider=provider, model="preset-model", context_window_tokens=8000,
            signature=("preset", name),
        ),
    )
    defaults.update(kw)
    return SubagentManager(**defaults)




_ID_RE = re.compile(r"id: ([0-9a-f]+)")


def _spawn_id(message: str) -> str:
    match = _ID_RE.search(message)
    assert match, message
    return match.group(1)


async def _drain(sm: SubagentManager) -> None:
    tasks = list(sm._running_tasks.values())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)
    if sm._finalizer_tasks:
        await asyncio.gather(*list(sm._finalizer_tasks), return_exceptions=True)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_durable_records_list_by_session_and_sender(tmp_path) -> None:
    provider = _ScriptedProvider([LLMResponse(content="done", tool_calls=[])])
    sm = _manager(tmp_path, provider=provider)
    a = _spawn_id(await sm.spawn(
        task="task A", label="alpha", session_key="sess-1",
        origin_channel="telegram", origin_chat_id="111", origin_sender_id="u1",
    ))
    b = _spawn_id(await sm.spawn(
        task="task B", label="beta", session_key="sess-1",
        origin_channel="telegram", origin_chat_id="111", origin_sender_id="u2",
    ))
    await sm.spawn(
        task="task C", label="gamma", session_key="sess-2",
        origin_channel="telegram", origin_chat_id="222", origin_sender_id="u1",
    )
    await _drain(sm)

    found = sm.list_session_task_records("sess-1")
    assert len(found) == 2
    assert {r["task_id"] for r in found} == {a, b}
    # sender-scoped listing hides other users' tasks in the same session
    mine = sm.list_session_task_records("sess-1", sender_id="u1")
    assert [r["task_id"] for r in mine] == [a]
    # cross-session invisible
    assert sm.list_session_task_records("sess-other") == []
    # durable across manager instances (restart)
    fresh = _manager(tmp_path, provider=_ScriptedProvider([]))
    assert {r["task_id"] for r in fresh.list_session_task_records("sess-1")} == {a, b}
    # read by public id survives restart
    assert fresh.read_run_record(a) is not None
    assert fresh.read_run_record("nope") is None


@pytest.mark.asyncio
async def test_targeted_cancel_stops_only_the_requested_task(tmp_path) -> None:
    gate = asyncio.Event()

    class _BlockingProvider(_ScriptedProvider):
        async def chat_with_retry(self, **kwargs) -> LLMResponse:
            await gate.wait()
            return LLMResponse(content="ok", tool_calls=[])

    sm = _manager(tmp_path, provider=_BlockingProvider([]))
    blocked = _spawn_id(await sm.spawn(task="blocked", session_key="sess-1"))
    other = _spawn_id(await sm.spawn(task="other", session_key="sess-1"))
    await asyncio.sleep(0)  # let both tasks start/queue

    result = await sm.cancel_task(blocked, session_key="sess-1")

    assert result == "cancelled"
    # Release the gate so remaining tasks finish, then drain.
    gate.set()
    await _drain(sm)
    blocked_rec = sm.read_run_record(blocked)
    assert blocked_rec is not None
    assert SubagentManager.task_status_vocabulary(blocked_rec) == "cancelled"
    other_rec = sm.read_run_record(other)
    assert other_rec is not None
    # the sibling was never touched: it is either still active or completed,
    # but definitely NOT cancelled.
    assert SubagentManager.task_status_vocabulary(other_rec) != "cancelled"
    assert await sm.cancel_task("missing", session_key="sess-1") == "not_found"
    # Cross-session access is rejected before terminal-state reporting.
    assert await sm.cancel_task(blocked, session_key="sess-other") == "not_owned"


@pytest.mark.asyncio
async def test_retry_creates_new_run_with_lineage_and_budgets(tmp_path) -> None:
    provider = _ScriptedProvider([])
    provider.set_fail(True)
    sm = _manager(tmp_path, provider=provider)
    failed = _spawn_id(await sm.spawn(
        task="do the thing", label="thing", session_key="sess-1",
        origin_channel="telegram", origin_chat_id="111", origin_sender_id="u1",
        model_preset="fast", max_iterations=7, max_tokens=512,
    ))
    await _drain(sm)
    failed_rec = sm.read_run_record(failed)
    assert failed_rec is not None
    assert SubagentManager.task_status_vocabulary(failed_rec) == "failed"

    # Retry with the same provider healthy again: budgets + lineage preserved.
    provider.set_fail(False)
    provider.responses = [LLMResponse(content="ok", tool_calls=[])]
    status, new_id = await sm.retry_task(failed, session_key="sess-1", sender_id="u1")
    await _drain(sm)

    assert status == "created"
    new_rec = sm.read_run_record(new_id)
    assert new_rec is not None
    assert new_rec["retry_of"] == failed
    assert new_rec["params"]["max_iterations"] == 7
    assert new_rec["params"]["max_tokens"] == 512
    assert new_rec["params"]["model_preset"] == "fast"
    assert SubagentManager.task_status_vocabulary(new_rec) == "completed"
    updated_old = sm.read_run_record(failed)
    assert updated_old["retried_by"] == new_id
    # The failed record was NOT mutated into running.
    assert SubagentManager.task_status_vocabulary(updated_old) == "failed"


@pytest.mark.asyncio
async def test_retry_cross_session_and_active_rejected(tmp_path) -> None:
    sm = _manager(tmp_path, provider=_ScriptedProvider([LLMResponse(content="ok", tool_calls=[])]))
    tid = _spawn_id(await sm.spawn(task="x", session_key="sess-1", origin_sender_id="u1"))
    await _drain(sm)

    status, _ = await sm.retry_task(tid, session_key="sess-other")
    assert status == "not_owned"
    status, _ = await sm.retry_task(tid, session_key="sess-1", sender_id="u2")
    assert status == "not_owned"
    status, _ = await sm.retry_task("missing", session_key="sess-1")
    assert status == "not_found"

    # Still-active retry refusal.
    gate = asyncio.Event()

    class _BlockingProvider(_ScriptedProvider):
        async def chat_with_retry(self, **kwargs) -> LLMResponse:
            await gate.wait()
            return LLMResponse(content="ok", tool_calls=[])

    sm2 = _manager(tmp_path, provider=_BlockingProvider([]))
    active = _spawn_id(await sm2.spawn(task="slow", session_key="sess-1"))
    await asyncio.sleep(0)
    status, _ = await sm2.retry_task(active, session_key="sess-1")
    assert status == "still_active"
    gate.set()
