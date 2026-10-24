"""Real runner/bridge regressions for discarded worker failures and top-level artifacts."""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools._worker_artifacts import INLINE_BYTES
from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.filesystem import ReadFileTool
from nanobot.providers.base import LLMResponse

from .test_codemode import _Tool, sandbox_available  # noqa: F401
from .test_readonly_workers import FakeProvider, parent_registry, spec, tool_call


@pytest.mark.usefixtures("sandbox_available")
class TestDiscardedWorkerOutcomes:
    @pytest.mark.parametrize("failure", ["provider_error", "partial", "timeout", "budget_exhausted", "caught_error"])
    async def test_discarded_failure_cannot_become_parent_success(self, tmp_path, failure):
        async def child_handler(request, number):
            if failure == "timeout":
                await asyncio.Future()
            if failure == "provider_error":
                return LLMResponse(content="quota exhausted", finish_reason="error")
            if failure == "partial":
                reference = json.loads(request["messages"][1]["content"])["artifacts"][0]
                return tool_call("artifact_read", artifact=reference)
            return LLMResponse(content="seed completed")

        child = FakeProvider(child_handler)
        limits = {"max_iterations": 1} if failure == "partial" else {}
        if failure == "timeout":
            limits["timeout_s"] = 1
        if failure == "budget_exhausted":
            limits["max_workers"] = 1
        registry = parent_registry(tmp_path, child, **limits)
        code = 'ref = await artifact_put("selected")\n'
        if failure == "budget_exhausted":
            code += 'await worker("seed")\n'
        code += (
            'try:\n'
            f'    ignored = await worker({"" if failure == "caught_error" else "analyze"!r}, artifacts=[ref["artifact"]])\n'
            'except Exception: pass\n'
            'return "Analysis succeeded"'
        )
        parent = FakeProvider(lambda request, n: tool_call("codemode", code=code) if n == 1 else LLMResponse(content="All analysis completed successfully."))
        result = await AgentRunner(parent).run(spec(registry))
        failures = [event for event in result.tool_events if event["name"] == "worker" and event["status"] != "success"]
        assert len(failures) == 1
        event = failures[0]
        expected = "partial" if failure in {"provider_error", "partial"} else (
            "error" if failure == "caught_error" else failure
        )
        assert event["evidence"][0]["worker_status"] == expected
        assert event["evidence"][0]["artifact"]
        assert event["parent_call_id"] == "call" and event["call_id"].startswith("call/worker/")
        assert result.stop_reason == "partial_completion"
        assert result.final_content.startswith("Incomplete:")
        assert len(parent.requests) == 3  # default verification remains enabled
        assert "worker:" in parent.requests[-1]["messages"][-1]["content"]
        assert len(child.requests) == (0 if failure == "caught_error" else 1)
        assert len(result.worker_usage) == (0 if failure in {"caught_error", "timeout"} else 1)
        if failure == "provider_error":
            assert "out of quota" in event["detail"]

    @pytest.mark.parametrize("recovery", ["same", "different_task", "different_material", "different_artifact"])
    async def test_only_matching_worker_retry_resolves_failure(self, tmp_path, recovery):
        child = FakeProvider(lambda request, n: LLMResponse(content="quota exhausted", finish_reason="error") if n == 1 else LLMResponse(content="recovered"))
        registry = parent_registry(tmp_path, child)
        code = (
            'a = await artifact_put("selected A")\n'
            'b = await artifact_put("selected B")\n'
            'await worker("analyze", artifacts=[a["artifact"]], material="selected")\n'
            f'await worker({"another task" if recovery == "different_task" else "analyze"!r}, '
            f'artifacts=[{"b" if recovery == "different_artifact" else "a"}["artifact"]], '
            f'material={"different" if recovery == "different_material" else "selected"!r})\n'
            'return "success"'
        )
        parent = FakeProvider(lambda request, n: tool_call("codemode", code=code) if n == 1 else LLMResponse(content="All analysis completed successfully."))
        result = await AgentRunner(parent).run(spec(registry))
        assert result.stop_reason == ("completed" if recovery == "same" else "partial_completion")
        assert len(parent.requests) == (2 if recovery == "same" else 3)
        assert len(child.requests) == len(result.worker_usage) == 2
        worker_events = [event for event in result.tool_events if event["name"] == "worker"]
        assert [event["status"] for event in worker_events] == ["partial", "success"]

    async def test_discarded_failure_honors_fail_on_tool_error(self, tmp_path):
        child = FakeProvider(lambda *_: LLMResponse(content="quota exhausted", finish_reason="error"))
        registry = parent_registry(tmp_path, child)
        parent = FakeProvider(lambda *_: tool_call("codemode", code='await worker("analyze")\nreturn "success"'))
        result = await AgentRunner(parent).run(spec(registry, fail_on_tool_error=True))
        assert result.stop_reason == "tool_error"
        assert "Worker partial" in result.error
        assert len(parent.requests) == 1

    async def test_cancelled_unawaited_worker_keeps_parent_failure_evidence(self, tmp_path):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def hang(*_):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        registry = parent_registry(tmp_path, FakeProvider(hang))
        code = 'asyncio.create_task(worker("analyze"))\nawait asyncio.sleep(0.2)\nreturn "success"'
        parent = FakeProvider(lambda request, n: tool_call("codemode", code=code) if n == 1 else LLMResponse(content="success"))
        result = await AgentRunner(parent).run(spec(registry))
        assert started.is_set() and cancelled.is_set()
        assert result.stop_reason == "partial_completion"
        assert any(event.get("evidence", [{}])[0].get("worker_status") == "cancelled"
                   for event in result.tool_events if event["name"] == "worker")


@pytest.mark.usefixtures("sandbox_available")
class TestTopLevelArtifacts:
    @pytest.mark.parametrize("source_kind", ["text", "office", "pdf", "tool", "tool_error", "text_blocks"])
    async def test_top_level_source_is_bounded_and_full_tail_reaches_selected_worker(self, tmp_path, monkeypatch, source_kind):
        bulk = "source-data\n" * 20000 + "TAIL_SENTINEL\n"
        observed = {}

        def child_handler(request, number):
            if number == 1:
                reference = json.loads(request["messages"][1]["content"])["artifacts"][0]
                return tool_call("codemode", code=(
                    f'found = await tools.artifact_search(artifact={reference!r}, query="TAIL_SENTINEL")\n'
                    'offset = found["data"]["offsets"][0]\n'
                    f'tail = await artifact_read({reference!r}, offset=offset, limit=40)\n'
                    'assert "TAIL_SENTINEL" in tail["text"]\n'
                    'return "full tail found"'
                ))
            assert "full tail found" in request["messages"][-1]["content"]
            return LLMResponse(content="tail verified")

        child = FakeProvider(child_handler)
        registry = parent_registry(tmp_path, child)
        if source_kind in {"text", "office", "pdf"}:
            suffix = {"text": ".txt", "office": ".docx", "pdf": ".pdf"}[source_kind]
            path = tmp_path / ("large" + suffix)
            path.write_text(bulk)
            registry.register(ReadFileTool(workspace=tmp_path, allowed_dir=tmp_path))
            if source_kind == "office":
                monkeypatch.setattr("nanobot.utils.document.extract_text", lambda path: bulk)
            if source_kind == "pdf":
                import fitz

                document = MagicMock()
                document.__len__.return_value = 1
                document.__getitem__.return_value.get_text.return_value = bulk
                monkeypatch.setattr(fitz, "open", lambda path: document)
            first = tool_call("read_file", path=str(path), limit=100000, force=True)
        else:
            async def source():
                if source_kind == "text_blocks":
                    return [{"type": "text", "text": bulk}]
                return ToolResult(bulk, status="retryable_error" if source_kind == "tool_error" else "success",
                                  data={"full": bulk}, exit_code=23 if source_kind == "tool_error" else None,
                                  evidence=[{"kind": "original_operation"}])

            registry.register(_Tool("source", handler=source))
            first = tool_call("source")

        def parent_handler(request, number):
            if number == 1:
                return first
            if number == 2:
                content = request["messages"][-1]["content"]
                assert len(content.encode()) < INLINE_BYTES
                assert "TAIL_SENTINEL" not in content
                reference = json.loads(content)
                assert reference["partial"] is True
                if source_kind == "tool_error":
                    assert reference["status"] == "retryable_error" and reference["exit_code"] == 23
                    assert reference["evidence"] == [{"kind": "original_operation"}]
                sources = reference.get("source_artifacts")
                observed["reference"] = sources[0] if sources else reference
                selected = observed["reference"]["artifact"]
                return tool_call("codemode", code=f'return await worker("find the tail", artifacts=[{selected!r}])')
            assert "tail verified" in request["messages"][-1]["content"]
            return LLMResponse(content="analysis finished")

        parent = FakeProvider(parent_handler)
        result = await AgentRunner(parent).run(spec(registry, workspace=tmp_path, verify_tool_failures=False))
        assert result.stop_reason == ("partial_completion" if source_kind == "tool_error" else "completed")
        assert observed["reference"]["chars"] >= len(bulk)
        assert len(child.requests) == len(result.worker_usage) == 2
        assert all(bulk not in json.dumps(request) for request in parent.requests)

    async def test_top_level_read_opt_out_keeps_existing_output(self, tmp_path):
        path = tmp_path / "large.txt"
        path.write_text("source-data\n" * 20000 + "TAIL_SENTINEL\n")
        child = FakeProvider(lambda *_: LLMResponse(content="unused"))
        registry = parent_registry(tmp_path, child)
        registry.get("codemode").config.workers.enable = False
        registry.register(ReadFileTool(workspace=tmp_path, allowed_dir=tmp_path))
        parent = FakeProvider(lambda request, n: tool_call("read_file", path=str(path), limit=100000, force=True) if n == 1 else LLMResponse(content="done"))
        result = await AgentRunner(parent).run(spec(registry, workspace=tmp_path))
        content = parent.requests[-1]["messages"][-1]["content"]
        assert len(content) > 128000 and content.startswith("1| source-data")
        assert "source_artifacts" not in content and "TAIL_SENTINEL" not in content
        assert result.worker_usage == [] and child.requests == []
