from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from nanobot.agent.tools.base import ToolResult, adapt_legacy_tool_result
from nanobot.agent.tools.shell import ExecTool


def test_legacy_tool_results_have_an_explicit_compatibility_adapter() -> None:
    success = adapt_legacy_tool_result("plain legacy output")
    failure = adapt_legacy_tool_result("Error: legacy failure")

    assert success == "plain legacy output"
    assert success.status == "success"
    assert failure.status == "retryable_error"
    assert failure.retryable is True


def test_unchecked_side_effect_cannot_be_verified_success() -> None:
    result = ToolResult(
        "message accepted",
        side_effects=[{"kind": "message", "recipient": "user-1"}],
        postcondition="unchecked",
    )

    assert result.status == "partial"
    assert result.operational_success is False
    assert result.verified is False


@pytest.mark.asyncio
async def test_exec_exposes_exit_code_127_as_structured_failure() -> None:
    process = AsyncMock()
    process.communicate.return_value = (b"plausible output\n", b"not found\n")
    process.returncode = 127

    with patch.object(ExecTool, "_spawn", return_value=process):
        result = await ExecTool().execute(command="missing-command")

    assert isinstance(result, ToolResult)
    assert result.status == "retryable_error"
    assert result.exit_code == 127
    assert result.stdout == "plausible output\n"
    assert result.stderr == "not found\n"
    assert result.data == {
        "exit_code": 127,
        "stdout": "plausible output\n",
        "stderr": "not found\n",
    }
