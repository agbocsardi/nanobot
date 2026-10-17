"""False-success regressions derived from operational incidents."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.providers.base import LLMResponse, ToolCallRequest
from tests.harness.helpers import StaticTool, run_script


class _PathTool(Tool):
    """Scripted tool that declares the path argument used for resolution."""

    def __init__(self, name: str, *results: object):
        self._name = name
        self._results = list(results)

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Fixture tool: {self._name}"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "edits": {"type": "array", "items": {"type": "object"}},
            },
            "required": [],
        }

    async def execute(self, **kwargs) -> object:
        if len(self._results) == 1:
            return self._results[0]
        return self._results.pop(0)


def _edit_turn(call_id: str, name: str, path: str) -> LLMResponse:
    return LLMResponse(
        content="editing",
        tool_calls=[ToolCallRequest(id=call_id, name=name, arguments={"path": path})],
    )


def _tool_turn(name: str, arguments: dict | None = None) -> list[LLMResponse]:
    return [
        LLMResponse(
            content="working",
            tool_calls=[ToolCallRequest(id="call_1", name=name, arguments=arguments or {})],
        ),
        LLMResponse(content="done"),
    ]


@pytest.mark.asyncio
async def test_exit_127_cannot_be_recorded_as_success() -> None:
    process = AsyncMock()
    process.communicate.return_value = (b"plausible output\n", b"not found\n")
    process.returncode = 127
    tools = ToolRegistry()
    tools.register(ExecTool())

    with patch.object(ExecTool, "_spawn", return_value=process):
        result, _ = await run_script(
            _tool_turn("exec", {"command": "missing-command"}),
            tools=tools,
        )

    event = result.tool_events[0]
    # A command that ran and returned is never success, but its exit code is
    # payload data: it must not be announced as an unresolved tool failure.
    assert event["status"] == "nonzero_exit"
    assert event["execution_succeeded"] is True
    assert event["operational_success"] is False
    assert event["verified"] is False
    assert event["retryable"] is True
    assert event["exit_code"] == 127
    assert event["stdout"] == "plausible output\n"
    assert event["stderr"] == "not found\n"
    assert result.tools_used == []
    assert result.stop_reason == "completed"
    assert result.final_content == "done"


@pytest.mark.asyncio
async def test_nonzero_exit_alone_does_not_announce_an_incomplete_turn() -> None:
    """Probe commands exit non-zero all the time (grep with no match)."""
    process = AsyncMock()
    process.communicate.return_value = (b"", b"")
    process.returncode = 1
    tools = ToolRegistry()
    tools.register(ExecTool())

    with patch.object(ExecTool, "_spawn", return_value=process):
        result, _ = await run_script(
            _tool_turn("exec", {"command": "grep missing pattern.txt"}),
            tools=tools,
        )

    assert result.tool_events[0]["status"] == "nonzero_exit"
    assert result.stop_reason == "completed"
    assert result.final_content == "done"


@pytest.mark.asyncio
async def test_unchecked_side_effect_is_recorded_as_partial() -> None:
    tools = ToolRegistry()
    tools.register(StaticTool(
        "deliver",
        ToolResult(
            "accepted",
            side_effects=[{"kind": "message", "recipient": "user-1"}],
            postcondition="unchecked",
        ),
    ))

    result, provider = await run_script(_tool_turn("deliver"), tools=tools)

    event = result.tool_events[0]
    assert event["status"] == "partial"
    assert event["postcondition"] == "unchecked"
    assert event["verified"] is False
    assert event["side_effects"] == [{"kind": "message", "recipient": "user-1"}]
    assert result.tools_used == []
    # An unverifiable side effect earns no verification turn: no tool can
    # confirm it and a retry is churn. The notice guards the claim instead.
    assert len(provider.requests) == 2
    assert result.stop_reason == "partial_completion"
    assert result.final_content.startswith("Incomplete:")
    assert "- deliver" in result.final_content


@pytest.mark.asyncio
async def test_policy_block_stays_distinct_from_operational_failure() -> None:
    tools = ToolRegistry()
    tools.register(StaticTool(
        "dangerous_action",
        ToolResult.policy_block(
            "blocked",
            evidence=[{"kind": "policy", "rule": "deny"}],
        ),
    ))

    result, _ = await run_script(_tool_turn("dangerous_action"), tools=tools)

    event = result.tool_events[0]
    assert event["status"] == "policy_block"
    assert event["retryable"] is False
    assert event["evidence"] == [{"kind": "policy", "rule": "deny"}]
    assert result.stop_reason == "policy_block"
    assert result.final_content.startswith("Incomplete:")


@pytest.mark.asyncio
async def test_retryable_error_can_recover_with_later_success() -> None:
    class RecoveringTool(StaticTool):
        def __init__(self):
            super().__init__("inspect", None)
            self.calls = 0

        async def execute(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ToolResult.retryable_error("missing file")
            return ToolResult("found alternate file")

    tools = ToolRegistry()
    tools.register(RecoveringTool())
    responses = [
        LLMResponse(
            content="trying",
            tool_calls=[ToolCallRequest(id="call_1", name="inspect", arguments={})],
        ),
        LLMResponse(
            content="recovering",
            tool_calls=[ToolCallRequest(id="call_2", name="inspect", arguments={})],
        ),
        LLMResponse(content="done with evidence"),
    ]

    result, _ = await run_script(responses, tools=tools)

    assert [event["status"] for event in result.tool_events] == [
        "retryable_error",
        "success",
    ]
    assert result.stop_reason == "completed"
    assert result.final_content == "done with evidence"


def _failing_write(name: str = "write_file") -> _PathTool:
    return _PathTool(
        name,
        ToolResult.retryable_error("Error: permission denied"),
        ToolResult("Successfully wrote the file"),
    )


@pytest.mark.asyncio
async def test_verification_turn_lets_the_model_retry_before_replying() -> None:
    """A false-success reply earns one turn to fix the operation, not a prefix."""
    tools = ToolRegistry()
    tools.register(_failing_write())

    result, provider = await run_script(
        [
            _edit_turn("call_1", "write_file", "/notes/grocery-list.md"),
            LLMResponse(content="Saved the list."),
            _edit_turn("call_2", "write_file", "/notes/grocery-list.md"),
            LLMResponse(content="Saved the list, after a retry."),
        ],
        tools=tools,
        max_iterations=6,
    )

    # Four provider calls: the original reply, the nudge, the retry, the reply.
    assert len(provider.requests) == 4
    nudge = provider.requests[2]["messages"][-1]
    assert nudge["role"] == "user"
    assert "did not complete" in nudge["content"]
    assert "- write_file: Error: permission denied" in nudge["content"]
    assert result.stop_reason == "completed"
    assert result.final_content == "Saved the list, after a retry."


@pytest.mark.asyncio
async def test_verification_turn_runs_once_then_reports_the_failure() -> None:
    """The nudge must not become a loop: one check, then the honest notice."""
    tools = ToolRegistry()
    tools.register(_failing_write())

    result, provider = await run_script(
        [
            _edit_turn("call_1", "write_file", "/notes/grocery-list.md"),
            LLMResponse(content="Saved the list."),
            LLMResponse(content="Saved the list."),
        ],
        tools=tools,
        max_iterations=6,
    )

    assert len(provider.requests) == 3
    assert result.stop_reason == "partial_completion"
    assert result.final_content.startswith("Incomplete:")
    assert "- write_file: Error: permission denied" in result.final_content


@pytest.mark.asyncio
async def test_no_verification_turn_without_iteration_budget() -> None:
    """With no slot left, fall back to the prefix rather than the nudge."""
    tools = ToolRegistry()
    tools.register(_failing_write())

    result, provider = await run_script(
        [
            _edit_turn("call_1", "write_file", "/notes/grocery-list.md"),
            LLMResponse(content="Saved the list."),
        ],
        tools=tools,
        max_iterations=2,
    )

    assert len(provider.requests) == 2
    assert result.stop_reason == "partial_completion"
    assert result.final_content.startswith("Incomplete:")


@pytest.mark.asyncio
async def test_verification_turn_can_be_disabled() -> None:
    """verify_tool_failures=False keeps the prefix-only behaviour."""
    tools = ToolRegistry()
    tools.register(_failing_write())

    result, provider = await run_script(
        [
            _edit_turn("call_1", "write_file", "/notes/grocery-list.md"),
            LLMResponse(content="Saved the list."),
        ],
        tools=tools,
        max_iterations=6,
        verify_tool_failures=False,
    )

    assert len(provider.requests) == 2
    assert result.stop_reason == "partial_completion"
    assert result.final_content.startswith("Incomplete:")


@pytest.mark.asyncio
async def test_resolved_failures_never_earn_a_verification_turn() -> None:
    """Nothing unresolved means no extra call at all."""
    tools = ToolRegistry()
    tools.register(_failing_write("apply_patch"))
    tools.register(_PathTool("edit_file", ToolResult("Successfully edited the file")))

    result, provider = await run_script(
        [
            LLMResponse(
                content="patching",
                tool_calls=[ToolCallRequest(
                    id="call_1",
                    name="apply_patch",
                    arguments={"edits": [{"path": "/notes/grocery-list.md"}]},
                )],
            ),
            _edit_turn("call_2", "edit_file", "/notes/grocery-list.md"),
            LLMResponse(content="List updated."),
        ],
        tools=tools,
        max_iterations=6,
    )

    assert len(provider.requests) == 3
    assert result.stop_reason == "completed"
    assert result.final_content == "List updated."


@pytest.mark.asyncio
async def test_failed_edit_recovered_by_another_tool_on_the_same_path() -> None:
    """apply_patch missing its anchor, then edit_file writing that same file."""
    tools = ToolRegistry()
    tools.register(_PathTool(
        "apply_patch",
        ToolResult.retryable_error("Error: old_text not found in /notes/grocery-list.md"),
    ))
    tools.register(_PathTool("edit_file", ToolResult("Successfully edited the file")))

    result, _ = await run_script(
        [
            LLMResponse(
                content="patching",
                tool_calls=[ToolCallRequest(
                    id="call_1",
                    name="apply_patch",
                    arguments={"edits": [{"path": "/notes/grocery-list.md"}]},
                )],
            ),
            _edit_turn("call_2", "edit_file", "/notes/grocery-list.md"),
            LLMResponse(content="list updated"),
        ],
        tools=tools,
    )

    assert [event["status"] for event in result.tool_events] == [
        "retryable_error",
        "success",
    ]
    assert result.stop_reason == "completed"
    assert result.final_content == "list updated"


@pytest.mark.asyncio
async def test_recovery_on_another_path_still_reports_the_failure() -> None:
    """A write that never landed must not be laundered by a different write."""
    tools = ToolRegistry()
    tools.register(_PathTool(
        "apply_patch",
        ToolResult.retryable_error("Error: old_text not found in /notes/grocery-list.md"),
    ))
    tools.register(_PathTool("edit_file", ToolResult("Successfully edited the file")))

    result, _ = await run_script(
        [
            LLMResponse(
                content="patching",
                tool_calls=[ToolCallRequest(
                    id="call_1",
                    name="apply_patch",
                    arguments={"edits": [{"path": "/notes/grocery-list.md"}]},
                )],
            ),
            _edit_turn("call_2", "edit_file", "/notes/shopping-list.md"),
            LLMResponse(content="both lists updated"),
        ],
        tools=tools,
    )

    assert result.stop_reason == "partial_completion"
    assert result.final_content.startswith("Incomplete:")
    # The notice names what did not land, so the claim is checkable.
    assert "apply_patch" in result.final_content
    assert "old_text not found" in result.final_content


@pytest.mark.asyncio
async def test_resolved_failure_is_not_named_in_a_later_notice() -> None:
    """Only genuinely unresolved failures appear in the notice."""
    tools = ToolRegistry()
    tools.register(_PathTool(
        "edit_file",
        ToolResult.retryable_error("Error: old_text not found"),
        ToolResult("Successfully edited the file"),
    ))
    tools.register(StaticTool("deliver", ToolResult.retryable_error("Error: send failed")))

    result, _ = await run_script(
        [
            _edit_turn("call_1", "edit_file", "/notes/grocery-list.md"),
            _edit_turn("call_2", "edit_file", "/notes/grocery-list.md"),
            LLMResponse(
                content="sending",
                tool_calls=[ToolCallRequest(id="call_3", name="deliver", arguments={})],
            ),
            LLMResponse(content="all done"),
        ],
        tools=tools,
        max_iterations=4,
    )

    assert result.stop_reason == "partial_completion"
    assert "- deliver: Error: send failed" in result.final_content
    assert result.final_content.count("- ") == 1


@pytest.mark.asyncio
async def test_unrelated_success_does_not_recover_retryable_error() -> None:
    tools = ToolRegistry()
    tools.register(StaticTool("inspect", ToolResult.retryable_error("missing file")))
    tools.register(StaticTool("unrelated", ToolResult("listed directory")))
    responses = [
        LLMResponse(
            content="trying",
            tool_calls=[ToolCallRequest(id="call_1", name="inspect", arguments={})],
        ),
        LLMResponse(
            content="doing something else",
            tool_calls=[ToolCallRequest(id="call_2", name="unrelated", arguments={})],
        ),
        LLMResponse(content="done"),
    ]

    result, _ = await run_script(responses, tools=tools)

    assert result.stop_reason == "partial_completion"
    assert result.final_content.startswith("Incomplete:")
