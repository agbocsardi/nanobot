"""Regression tests for the cron tool's JSON-schema / runtime contract (#3113).

The schema advertised ``required=["action"]`` while ``_add_job`` rejected empty
``message``; LLMs rationally omitted ``message`` and looped on the runtime
error. The fix keeps ``required=["action"]`` (so ``list``/``remove`` stay
callable) but states the per-action requirement in each field's description
and tightens the runtime error for ``add`` without ``message``.
"""

from __future__ import annotations

import pytest

from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.registry import ToolRegistry


class _SvcStub:
    """Minimal CronService stand-in; we only exercise schema/dispatch paths."""

    def list_jobs(self):
        return []

    def get_job(self, _job_id):
        return None

    def remove_job(self, _job_id):
        return "not-found"

    def add_job(self, **kwargs):
        class _J:
            pass

        j = _J()
        j.id = "id1"
        j.name = kwargs.get("name", "x")
        return j


@pytest.fixture
def registry() -> ToolRegistry:
    tool = CronTool(_SvcStub(), default_timezone="UTC")
    tool.set_context(
        RequestContext(channel="channel", chat_id="chat-id", session_key="channel:chat-id")
    )
    reg = ToolRegistry()
    reg.register(tool)
    return reg


class TestSchemaContract:
    def test_list_accepted_without_message(self, registry: ToolRegistry) -> None:
        # action='list' must pass schema validation with nothing but 'action'.
        _, _, err = registry.prepare_call("cron", {"action": "list"})
        assert err is None

    def test_remove_accepted_without_message(self, registry: ToolRegistry) -> None:
        # action='remove' must pass schema validation with just 'action' + 'job_id'.
        _, _, err = registry.prepare_call("cron", {"action": "remove", "job_id": "abc"})
        assert err is None

    def test_add_with_message_accepted(self, registry: ToolRegistry) -> None:
        _, _, err = registry.prepare_call(
            "cron", {"action": "add", "message": "ping", "at": "2030-01-01T00:00:00"}
        )
        assert err is None

    def test_add_without_message_surfaces_actionable_runtime_error(
        self, registry: ToolRegistry
    ) -> None:
        # Schema permits omitting message; the runtime must return a message
        # that tells the LLM exactly what's missing and how to retry, so it
        # doesn't loop like #3113 reports.
        import asyncio

        tool = registry._tools["cron"]  # type: ignore[attr-defined]
        out = asyncio.run(tool.execute(action="add", at="2030-01-01T00:00:00"))
        assert "message" in out
        assert "add" in out
        assert "Retry" in out or "retry" in out


class TestSchemaSelfDescribesRequirements:
    def test_message_description_flags_add_requirement(self) -> None:
        # LLMs rely on field descriptions to infer when something is actually
        # needed. Without this hint, #3113's loop returns.
        tool = CronTool(_SvcStub())
        desc = tool.parameters["properties"]["message"]["description"]
        assert "REQUIRED" in desc and "action='add'" in desc

    def test_job_id_description_flags_remove_requirement(self) -> None:
        tool = CronTool(_SvcStub())
        desc = tool.parameters["properties"]["job_id"]["description"]
        assert "REQUIRED" in desc and "action='remove'" in desc

    def test_top_level_required_stays_narrow(self) -> None:
        # If 'message' or 'job_id' ever creep back into top-level required,
        # list/remove start failing schema validation (the bug PR #3163 v1
        # accidentally introduced).
        tool = CronTool(_SvcStub())
        assert tool.parameters["required"] == ["action"]


class TestModelPresetValidation:
    """Per-job model_preset: validated at add time, stored on the payload."""

    @staticmethod
    def _tool(model_presets=None) -> CronTool:
        tool = CronTool(_SvcStub(), default_timezone="UTC", model_presets=model_presets)
        tool.set_context(
            RequestContext(channel="c", chat_id="d", session_key="c:d")
        )
        return tool

    def test_add_accepts_known_preset_and_stores_it(self) -> None:
        import asyncio

        captured = {}

        class _CapturingSvc(_SvcStub):
            def add_job(self, **kwargs):
                captured.update(kwargs)
                class _J:
                    pass
                j = _J()
                j.id = "id1"
                j.name = kwargs.get("name", "x")
                return j

        tool = CronTool(_CapturingSvc(), model_presets={"default": object(), "deepseek": object()})
        tool.set_context(RequestContext(channel="c", chat_id="d", session_key="c:d"))
        out = asyncio.run(tool.execute(
            action="add", message="m", every_seconds=60, model_preset="deepseek"
        ))
        assert "Created job" in out
        # Stored normalized name on the payload-bound kwarg.
        assert captured.get("model_preset") == "deepseek"

    def test_add_rejects_unknown_preset_with_clear_error(self) -> None:
        import asyncio

        tool = self._tool(model_presets={"default": object()})
        out = asyncio.run(tool.execute(
            action="add", message="m", every_seconds=60, model_preset="nope"
        ))
        assert "Error" in out
        assert "nope" in out

    def test_add_without_model_preset_is_unaffected(self) -> None:
        import asyncio

        tool = self._tool(model_presets={"default": object()})
        out = asyncio.run(tool.execute(
            action="add", message="m", every_seconds=60
        ))
        assert "Created job" in out


def test_add_forwards_misfire_policy_and_grace_seconds() -> None:
    import asyncio
    captured = {}
    class Capturing(_SvcStub):
        def add_job(self, **kwargs):
            captured.update(kwargs)
            return super().add_job(**kwargs)
    tool = CronTool(Capturing())
    tool.set_context(RequestContext(channel="c", chat_id="d", session_key="c:d"))
    out = asyncio.run(tool.execute(action="add", message="m", every_seconds=60,
                                   misfire_policy="skip", misfire_grace_seconds=7))
    assert "Created job" in out
    assert captured["misfire_policy"] == "skip"
    assert captured["misfire_grace_ms"] == 7000


def test_schema_advertises_misfire_controls() -> None:
    tool = CronTool(_SvcStub())
    props = tool.parameters["properties"]
    assert props["misfire_policy"]["enum"] == ["skip", "coalesce"]
    assert props["misfire_grace_seconds"]["minimum"] == 0


def test_add_rejects_negative_misfire_grace() -> None:
    import asyncio
    tool = CronTool(_SvcStub())
    tool.set_context(RequestContext(channel="c", chat_id="d", session_key="c:d"))
    out = asyncio.run(tool.execute(action="add", message="m", every_seconds=60, misfire_grace_seconds=-1))
    assert "nonnegative" in out
