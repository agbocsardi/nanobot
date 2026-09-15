"""High-level programmatic interface to nanobot."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from contextvars import ContextVar

from nanobot.agent.hook import (
    AgentHook,
    AgentHookContext,
    AgentRunHookContext,
    SDKCaptureHook,
)
from nanobot.agent.loop import AgentLoop
from nanobot.providers.image_generation import image_gen_provider_configs


@dataclass(slots=True)
class RunResult:
    """Result of a single agent run."""

    content: str
    tools_used: list[str]
    messages: list[dict[str, Any]]


class _SDKHookFanout(AgentHook):
    """Delegate lifecycle events to the hooks of the current SDK run.

    One fan-out hook lives on the loop for the instance's lifetime; each
    ``Nanobot.run`` publishes its hooks (capture + user hooks) in a ContextVar
    so concurrent runs can never capture each other's results.
    """

    def __init__(self) -> None:
        super().__init__()
        self._current: ContextVar[tuple[AgentHook, ...]] = ContextVar(
            "nanobot_sdk_run_hooks", default=()
        )

    @contextmanager
    def scope(self, hooks: tuple[AgentHook, ...]) -> Iterator[None]:
        token = self._current.set(hooks)
        try:
            yield
        finally:
            self._current.reset(token)

    def wants_streaming(self) -> bool:
        return any(h.wants_streaming() for h in self._current.get())

    async def _fan_out(self, method: str, *args: Any, **kwargs: Any) -> None:
        for hook in self._current.get():
            await getattr(hook, method)(*args, **kwargs)

    async def before_run(self, context: AgentRunHookContext) -> None:
        await self._fan_out("before_run", context)

    async def after_run(self, context: AgentRunHookContext) -> None:
        await self._fan_out("after_run", context)

    async def on_error(self, context: AgentRunHookContext) -> None:
        await self._fan_out("on_error", context)

    async def on_finally(self, context: AgentRunHookContext) -> None:
        await self._fan_out("on_finally", context)

    async def before_iteration(self, context: AgentHookContext) -> None:
        await self._fan_out("before_iteration", context)

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        await self._fan_out("on_stream", context, delta)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        await self._fan_out("on_stream_end", context, resuming=resuming)

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        await self._fan_out("before_execute_tools", context)

    async def emit_reasoning(self, reasoning_content: str | None) -> None:
        await self._fan_out("emit_reasoning", reasoning_content)

    async def emit_reasoning_end(self) -> None:
        await self._fan_out("emit_reasoning_end")

    async def after_iteration(self, context: AgentHookContext) -> None:
        await self._fan_out("after_iteration", context)

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        for hook in self._current.get():
            content = hook.finalize_content(context, content)
        return content


class Nanobot:
    """Programmatic facade for running the nanobot agent.

    Usage::

        bot = Nanobot.from_config()
        result = await bot.run("Summarize this repo", hooks=[MyHook()])
        print(result.content)
    """

    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop
        # Instance-scoped policy, captured at construction so a run (and every
        # task it spawns) inherits this instance's paths and network rules
        # instead of whatever process-wide CLI defaults are current.
        self._config_path: Path | None = None
        self._ssrf_whitelist: list[str] | None = None
        self._hook_bus = _SDKHookFanout()
        extras = loop._extra_hooks
        if self._hook_bus not in extras:
            extras.append(self._hook_bus)

    @classmethod
    def from_config(
        cls,
        config_path: str | Path | None = None,
        *,
        workspace: str | Path | None = None,
    ) -> Nanobot:
        """Create a Nanobot instance from a config file.

        Args:
            config_path: Path to ``config.json``.  Defaults to
                ``~/.nanobot/config.json``.
            workspace: Override the workspace directory from config.
        """
        from nanobot.config.loader import (
            config_path_context,
            get_config_path,
            load_config,
            resolve_config_env_vars,
        )
        from nanobot.config.schema import Config

        resolved: Path | None = None
        if config_path is not None:
            resolved = Path(config_path).expanduser().resolve()
            if not resolved.exists():
                raise FileNotFoundError(f"Config not found: {resolved}")

        effective_path = resolved if resolved is not None else get_config_path()
        config: Config = resolve_config_env_vars(load_config(resolved))
        if workspace is not None:
            config.agents.defaults.workspace = str(
                Path(workspace).expanduser().resolve()
            )

        # Construct the loop under the scoped path so derived state (data dir,
        # config fingerprints) resolves to this instance.
        with config_path_context(effective_path):
            loop = AgentLoop.from_config(
                config,
                image_generation_provider_configs=image_gen_provider_configs(config),
            )
        bot = cls(loop)
        bot._config_path = effective_path
        bot._ssrf_whitelist = list(config.tools.ssrf_whitelist)
        return bot

    def _instance_scope(self, stack: ExitStack) -> None:
        """Enter this instance's config-path and SSRF-policy contexts.

        ContextVars propagate into tasks created inside the scope, so
        background work spawned from a run keeps the instance's policy.
        """
        from nanobot.config.loader import config_path_context
        from nanobot.security.network import configure_ssrf_whitelist, ssrf_whitelist_context

        if self._config_path is not None:
            stack.enter_context(config_path_context(self._config_path))
        if self._ssrf_whitelist is not None:
            stack.enter_context(ssrf_whitelist_context())
            # configure_ssrf_whitelist writes into the active scope, leaving
            # process-wide CLI defaults untouched.
            configure_ssrf_whitelist(self._ssrf_whitelist)

    async def run(
        self,
        message: str,
        *,
        session_key: str = "sdk:default",
        hooks: list[AgentHook] | None = None,
        mode: str | None = None,
    ) -> RunResult:
        """Run the agent once and return the result.

        Args:
            message: The user message to process.
            session_key: Session identifier for conversation isolation.
                Different keys get independent history.
            hooks: Optional lifecycle hooks for this run. They are scoped to
                this call; concurrent runs never see each other's hooks.
            mode: Interaction mode for the run (audit, exploration, cron,
                heartbeat, delegated, ...). Policy rules match it
                deterministically; audit/exploration defaults are applied when
                the corresponding tools config flags are enabled.
        """
        capture = SDKCaptureHook()
        with ExitStack() as stack:
            stack.enter_context(self._hook_bus.scope((capture, *(hooks or ()))))
            self._instance_scope(stack)
            kwargs: dict[str, Any] = {"session_key": session_key}
            if mode is not None:
                kwargs["metadata"] = {"interaction_mode": mode}
            response = await self._loop.process_direct(message, **kwargs)

        content = (response.content if response else None) or ""
        return RunResult(
            content=content,
            tools_used=capture.tools_used,
            messages=capture.messages,
        )

    async def aclose(self) -> None:
        """Release resources held by this instance (MCP connections, etc.)."""
        with ExitStack() as stack:
            self._instance_scope(stack)
            await self._loop.close_mcp()

    async def __aenter__(self) -> Nanobot:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
