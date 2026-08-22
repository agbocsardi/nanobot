"""Offline provider and tool fixtures for control-plane regressions."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import AgentDefaults
from nanobot.providers.base import LLMResponse


class ScriptedProvider:
    """Return a fixed response sequence without network access."""

    def __init__(self, responses: Iterable[LLMResponse]):
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        self.requests.append(kwargs)
        if not self.responses:
            raise AssertionError("Scripted provider received an unexpected request")
        return self.responses.pop(0)


class StaticTool(Tool):
    """Return one fixed value while satisfying the real registry contract."""

    def __init__(self, name: str, result: Any):
        self._name = name
        self.result = result

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Fixture tool: {self._name}"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:
        return self.result


async def run_script(
    responses: Iterable[LLMResponse],
    *,
    tools: ToolRegistry | None = None,
    max_iterations: int = 3,
    finalize_on_max_iterations: bool = True,
):
    provider = ScriptedProvider(responses)
    # AgentRunner only requires the provider protocol at runtime. MagicMock supplies
    # optional capability attributes without coupling this harness to a live backend.
    provider.supports_progress_deltas = False
    runner = AgentRunner(provider)  # type: ignore[arg-type]
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "run fixture"}],
        tools=tools or ToolRegistry(),
        model="fixture-model",
        max_iterations=max_iterations,
        max_tool_result_chars=AgentDefaults().max_tool_result_chars,
        finalize_on_max_iterations=finalize_on_max_iterations,
    ))
    return result, provider


def unused_tools() -> MagicMock:
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock()
    return tools
