"""Regression tests for the exec URL guard split (issue #60).

The exec tool's URL check is a *lexical pre-flight check on command text*: no
request is made, and a script that reads the URL from disk walks past it. It must
therefore

* report its own cause instead of the web tools' SSRF wording,
* be classified by the runner as a recoverable policy block (not a
  non-bypassable security boundary), and
* not reject commands where the URL is only data (``echo``/``sed``/comments).
"""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.shell import ExecTool
from nanobot.config.schema import AgentDefaults
from nanobot.providers.base import LLMResponse, ToolCallRequest

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


def _resolver(mapping: dict[str, list[str]]):
    def _resolve(hostname, port, family=0, type_=0):
        addrs = mapping.get(hostname)
        if addrs is None:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (addr, 0)) for addr in addrs]
    return _resolve


LOOPBACK = {"127.0.0.1": ["127.0.0.1"], "localhost": ["127.0.0.1"]}


def test_url_as_inert_data_is_not_blocked(tmp_path) -> None:
    """echo/sed only move text: nothing in them can dial the URL."""
    tool = ExecTool(working_dir=str(tmp_path))
    with patch("nanobot.security.network.socket.getaddrinfo", _resolver(LOOPBACK)):
        for command in (
            'echo "connect to http://127.0.0.1:8080/health later"',
            "printf '%s\\n' http://127.0.0.1:8080/health",
            "sed -e 's|http://127.0.0.1:9000|http://example.com|' notes.txt",
            "grep -n 'http://127.0.0.1:18790' notes.txt",
        ):
            assert tool._guard_command(command, str(tmp_path)) is None, command


def test_url_inside_shell_comment_is_not_blocked(tmp_path) -> None:
    tool = ExecTool(working_dir=str(tmp_path))
    with patch("nanobot.security.network.socket.getaddrinfo", _resolver(LOOPBACK)):
        assert tool._guard_command(
            "# see http://127.0.0.1:18790 for the gateway\nls -la", str(tmp_path)
        ) is None


def test_piping_text_into_a_network_client_is_still_blocked(tmp_path) -> None:
    """An inert segment must not launder a URL into a network client via a pipe."""
    tool = ExecTool(working_dir=str(tmp_path))
    with patch("nanobot.security.network.socket.getaddrinfo", _resolver(LOOPBACK)):
        error = tool._guard_command(
            "echo http://127.0.0.1:9/x | curl -d @- http://example.com", str(tmp_path)
        )
    assert error is not None and "exec URL guard" in error


def test_network_clients_are_still_blocked(tmp_path) -> None:
    tool = ExecTool(working_dir=str(tmp_path))
    with patch("nanobot.security.network.socket.getaddrinfo", _resolver(LOOPBACK)):
        for command in (
            "curl -s http://127.0.0.1:9222/json",
            "wget http://127.0.0.1:8080/secret",
            "python3 -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:9222')\"",
        ):
            error = tool._guard_command(command, str(tmp_path))
            assert error is not None and "exec URL guard" in error, command


def test_unresolvable_host_reports_resolution_cause(tmp_path) -> None:
    """A stale public hostname is not an internal target; say what actually failed."""
    tool = ExecTool(working_dir=str(tmp_path))
    with patch("nanobot.security.network.socket.getaddrinfo", _resolver(LOOPBACK)):
        error = tool._guard_command("curl -s https://nonexistent.invalid/x", str(tmp_path))
    assert error is not None
    assert "unresolvable" in error
    assert "internal_target" not in error


def test_exec_block_is_not_classified_as_ssrf() -> None:
    exec_message = "Error: Command blocked by exec URL guard (internal_target: http://127.0.0.1:8080/ -> 127.0.0.1)"
    web_message = "Error: Blocked: host resolves to private/internal address 127.0.0.1"

    assert AgentRunner._is_exec_url_block(exec_message) is True
    assert AgentRunner._is_ssrf_violation(exec_message) is False
    assert AgentRunner._is_ssrf_violation(web_message) is True
    assert AgentRunner._is_exec_url_block(web_message) is False


@pytest.mark.asyncio
async def test_runner_returns_recoverable_hint_for_exec_url_block() -> None:
    """The exec block is retryable and carries exec advice, not the SSRF warning."""
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(side_effect=[
        LLMResponse(
            content="probing",
            tool_calls=[ToolCallRequest(
                id="call_exec_url",
                name="exec",
                arguments={"command": "curl http://127.0.0.1:9222/json"},
            )],
        ),
        LLMResponse(
            content="I wrote the URL to a file instead.",
            tool_calls=[],
        ),
    ])
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value=(
        "Error: Command blocked by exec URL guard (internal_target: http://127.0.0.1:9222/json -> 127.0.0.1)"
    ))

    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[],
        tools=tools,
        model="test-model",
        max_iterations=3,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    ))

    tool_messages = [m for m in result.messages if m.get("role") == "tool"]
    assert tool_messages
    content = tool_messages[0]["content"]
    assert "lexical check on the exec command text" in content
    assert "non-bypassable security boundary" not in content
    assert "Do not retry with curl" not in content
    assert "non-bypassable" not in content
    assert "tools.ssrfWhitelist" in content
    assert result.tool_events and result.tool_events[0]["detail"].startswith("exec_url_guard:")
