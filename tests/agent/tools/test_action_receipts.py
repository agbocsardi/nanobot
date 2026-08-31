"""Tests for durable action receipts and idempotent replay (issue #31)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.agent.tools.action_receipts import (
    ActionReceiptStore,
    canonical_arg_hash,
    redact_preview,
)
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import RequestContext, bind_request_context, reset_request_context
from nanobot.agent.tools.registry import ToolRegistry


class _SideEffectTool(Tool):
    """Representative side-effectful tool with an invocation counter."""

    effect = "external_write"
    replay = "never"

    def __init__(self, calls: list[int], *, replay: str = "never"):
        self._calls = calls
        self.replay = replay

    @property
    def name(self) -> str:
        return "side_effect"

    @property
    def description(self) -> str:
        return "fixture side-effect tool"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"message": {"type": "string"}}}

    async def execute(self, message: str, **kwargs) -> ToolResult:
        self._calls.append(1)
        return ToolResult(f"sent: {message}", side_effects=[{"kind": "fixture"}])


class _ReadOnlyTool(Tool):
    """Unannotated (read-only) control tool."""

    @property
    def name(self) -> str:
        return "look"

    @property
    def description(self) -> str:
        return "read only"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult("looked")


def _registry(tmp_path: Path) -> ToolRegistry:
    return ToolRegistry(receipt_store=ActionReceiptStore(tmp_path))


def _bind_ctx():
    return bind_request_context(
        RequestContext(
            channel="telegram", chat_id="11", session_key="telegram:11",
            sender_id="u1", metadata={},
        )
    )


def _succeeded_receipt_json(tmp_path, exec_id="e1", updated_past=0):
    store = ActionReceiptStore(tmp_path)
    decision, receipt = store.begin(
        exec_id=exec_id, session_key="telegram:11", sender_id="u1",
        tool="side_effect", arg_hash=canonical_arg_hash({"message": "hi"}),
        effect="external_write", replay="never", now_ms=1,
    )
    assert decision == "new"
    store.complete(exec_id, status="succeeded", ok=True, outcome="sent: hi")
    return store


# ---------------------------------------------------------------------------
# store semantics
# ---------------------------------------------------------------------------


def test_duplicate_success_never_dispatches_twice(tmp_path) -> None:
    calls: list[int] = []
    store = ActionReceiptStore(tmp_path)
    tool = _SideEffectTool(calls)
    registry = ToolRegistry(receipt_store=store)
    registry.register(tool)
    token = _bind_ctx()

    async def run() -> tuple[str, str]:
        first = await registry.execute("side_effect", {"message": "hi"}, exec_id="e1")
        second = await registry.execute("side_effect", {"message": "hi"}, exec_id="e1")
        return str(first), str(second)

    a, b = __import__("asyncio").run(run())
    reset_request_context(token)

    assert len(calls) == 1  # underlying tool called exactly once
    assert "Replayed from receipt" in b


def test_mismatched_id_is_rejected_and_audited(tmp_path) -> None:
    calls: list[int] = []
    store = ActionReceiptStore(tmp_path)
    registry = ToolRegistry(receipt_store=store)
    registry.register(_SideEffectTool(calls))
    token = _bind_ctx()

    async def run() -> tuple[str, str]:
        await registry.execute("side_effect", {"message": "hi"}, exec_id="e1")
        out = await registry.execute("side_effect", {"message": "different"}, exec_id="e1")
        return str(out)

    out, = (__import__("asyncio").run(run()),)
    reset_request_context(token)
    assert "mismatch" in out.lower()
    assert store.get("e1").status == "unknown" or store.get("e1").arg_hash != canonical_arg_hash({"message": "different"})


def test_crash_after_dispatch_leaves_unknown_and_never_auto_repeats(tmp_path) -> None:
    calls: list[int] = []
    store = ActionReceiptStore(tmp_path)
    registry = ToolRegistry(receipt_store=store)
    registry.register(_SideEffectTool(calls))
    token = _bind_ctx()

    async def run() -> str:
        # Step 1: first dispatch (crash simulated by not completing).
        decision, _ = store.begin(
            exec_id="e2", session_key="telegram:11", sender_id="u1",
            tool="side_effect", arg_hash=canonical_arg_hash({"message": "hi"}),
            effect="external_write", replay="never", now_ms=1,
        )
        assert decision == "new"
        # Step 2: same id after restart, but long after dispatch (stale).
        out = await registry.execute("side_effect", {"message": "hi"}, exec_id="e2")
        return str(out)

    out = __import__("asyncio").run(run())
    reset_request_context(token)
    assert "unknown" in out.lower()
    assert "not auto-repeated" in out.lower()
    assert len(calls) == 0  # never dispatched again


def test_idempotency_key_tool_may_resume_after_stale_started(tmp_path) -> None:
    calls: list[int] = []
    store = ActionReceiptStore(tmp_path)
    tool = _SideEffectTool(calls, replay="idempotency_key")
    registry = ToolRegistry(receipt_store=store)
    registry.register(tool)
    token = _bind_ctx()

    async def run() -> str:
        decision, _ = store.begin(
            exec_id="e3", session_key="telegram:11", sender_id="u1",
            tool="side_effect", arg_hash=canonical_arg_hash({"message": "hi"}),
            effect="external_write", replay="idempotency_key",
            now_ms=1,
        )
        assert decision == "new"
        # Stale started -> the idempotency_key replay policy may dispatch again.
        out = await registry.execute("side_effect", {"message": "hi"}, exec_id="e3")
        return str(out)

    out = __import__("asyncio").run(run())
    reset_request_context(token)
    assert "sent: hi" in out
    assert len(calls) == 1


def test_receipts_persist_across_restart_and_redact_secrets(tmp_path) -> None:
    _succeeded_receipt_json(tmp_path, "e1")
    fresh_store = ActionReceiptStore(tmp_path)
    receipt = fresh_store.get("e1")
    assert receipt is not None and receipt.status == "succeeded"
    preview = redact_preview({"message": "hi", "Authorization": "Bearer secret123"})
    assert "secret123" not in preview
    assert len(preview) < 500


def test_readonly_tool_is_not_receipt_gated(tmp_path) -> None:
    registry = ToolRegistry(receipt_store=ActionReceiptStore(tmp_path))
    token = _bind_ctx()
    read_only = _ReadOnlyTool()
    registry.register(read_only)

    async def run() -> tuple[str, str]:
        a = await registry.execute("look", {}, exec_id="e9")
        b = await registry.execute("look", {}, exec_id="e9")
        return str(a), str(b)

    a, b = __import__("asyncio").run(run())
    reset_request_context(token)
    assert a == "looked" and b == "looked"


# ---------------------------------------------------------------------------
# /receipt command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_receipt_command_reads_owned_receipt(tmp_path) -> None:
    from types import SimpleNamespace

    from nanobot.bus.events import InboundMessage
    from nanobot.command.builtin import (
        BUILTIN_COMMAND_SPECS,
        cmd_receipt,
    )
    from nanobot.command.router import CommandContext

    store = ActionReceiptStore(tmp_path)
    store.begin(exec_id="r1", session_key="telegram:11", sender_id="u1", tool="message",
                arg_hash="h", effect="external_write", replay="never")
    store.complete("r1", status="succeeded", ok=True, outcome="sent: hello")
    loop = SimpleNamespace(_receipt_store=store)
    msg = InboundMessage(channel="telegram", sender_id="u1", chat_id="11", content="/receipt r1")
    ctx = CommandContext(msg=msg, session=None, key=msg.session_key, raw="/receipt r1", args="r1", loop=loop)

    out = await cmd_receipt(ctx)
    assert "status: succeeded" in out.content
    assert "tool: message" in out.content

    msg2 = InboundMessage(channel="telegram", sender_id="u2", chat_id="11", content="/receipt r1")
    cross = CommandContext(msg=msg2, session=None, key=msg2.session_key, raw="/receipt r1", args="r1", loop=loop)
    out2 = await cmd_receipt(cross)
    assert "not found" in out2.content

    specs = {s.command: s for s in BUILTIN_COMMAND_SPECS}
    assert "/receipt" in specs
