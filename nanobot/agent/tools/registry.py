"""Tool registry for dynamic tool management."""

import json
from typing import Any

from nanobot.agent.tools.base import Tool, ToolResult, adapt_legacy_tool_result
from nanobot.agent.tools.policy import PolicyDecision, ToolPolicy


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(
        self,
        policy: ToolPolicy | None = None,
        receipt_store: Any | None = None,
    ):
        self._tools: dict[str, Tool] = {}
        self._cached_definitions: list[dict[str, Any]] | None = None
        self.policy = policy or ToolPolicy()
        # Durable action-receipt ledger for side-effectful tools (issue #31).
        self.receipt_store = receipt_store

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool
        self._cached_definitions = None

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)
        self._cached_definitions = None

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    @staticmethod
    def _lookup_key(name: str) -> str:
        """Normalize names for suggestions only; never for execution."""
        return "".join(ch.lower() for ch in name if ch.isalnum())

    def _suggest_name(self, name: str) -> str | None:
        key = self._lookup_key(str(name or ""))
        if not key:
            return None
        matches = [
            registered
            for registered in self._tools
            if self._lookup_key(registered) == key
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    @staticmethod
    def _schema_name(schema: dict[str, Any]) -> str:
        """Extract a normalized tool name from either OpenAI or flat schemas."""
        fn = schema.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str):
                return name
        name = schema.get("name")
        return name if isinstance(name, str) else ""

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get tool definitions with stable ordering for cache-friendly prompts.

        Built-in tools are sorted first as a stable prefix, then MCP tools are
        sorted and appended.  The result is cached until the next
        register/unregister call.
        """
        if self._cached_definitions is not None:
            return self._cached_definitions

        definitions = [tool.to_schema() for tool in self._tools.values()]
        builtins: list[dict[str, Any]] = []
        mcp_tools: list[dict[str, Any]] = []
        for schema in definitions:
            name = self._schema_name(schema)
            if name.startswith("mcp_"):
                mcp_tools.append(schema)
            else:
                builtins.append(schema)

        builtins.sort(key=self._schema_name)
        mcp_tools.sort(key=self._schema_name)
        self._cached_definitions = builtins + mcp_tools
        return self._cached_definitions

    def prepare_call(
        self,
        name: str,
        params: Any,
    ) -> tuple[Tool | None, Any, str | None]:
        """Resolve, cast, and validate one tool call."""
        tool = self._tools.get(name)
        if not tool:
            suggestion = self._suggest_name(str(name))
            hint = f" Did you mean '{suggestion}'? Tool names must match exactly." if suggestion else ""
            return None, params, (
                f"Error: Tool '{name}' not found.{hint} Available: {', '.join(self.tool_names)}"
            )

        params = self._coerce_params(tool, params)
        if not isinstance(params, dict):
            return tool, params, (
                f"Error: Tool '{name}' parameters must be a JSON object, got "
                f"{type(params).__name__}. Use named parameters like "
                'tool_name(param1="value1", param2="value2") matching the tool schema.'
            )

        cast_params = tool.cast_params(params)
        errors = tool.validate_params(cast_params)
        if errors:
            return tool, cast_params, (
                f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors)
            )
        return tool, cast_params, None

    @classmethod
    def _coerce_argument_value(cls, value: Any) -> Any:
        if value is None:
            return {}
        if not isinstance(value, str):
            return value

        stripped = value.strip()
        if not stripped:
            return {}

        if not stripped.startswith(("{", "[")):
            return value

        try:
            parsed = json.loads(stripped)
        except Exception:
            return value

        return parsed

    @classmethod
    def _coerce_params(cls, tool: Tool, params: Any) -> Any:
        params = cls._coerce_argument_value(params)
        return cls._unwrap_arguments_payload(tool, params)

    @classmethod
    def _unwrap_arguments_payload(cls, tool: Tool, params: Any) -> Any:
        if not isinstance(params, dict) or set(params) != {"arguments"}:
            return params
        properties = (tool.parameters or {}).get("properties", {})
        if isinstance(properties, dict) and "arguments" in properties:
            return params
        return cls._coerce_argument_value(params.get("arguments"))

    async def execute(self, name: str, params: Any, *, exec_id: str | None = None) -> Any:
        """Execute a tool by name with given parameters.

        ``exec_id`` is the stable run/tool-call identity used for durability
        receipts; side-effectful tools opt in implicitly by ``effect``.
        """
        hint = "\n\n[Analyze the error above and try a different approach.]"
        tool, params, error = self.prepare_call(name, params)
        if error:
            return ToolResult.retryable_error(error + hint)

        assert tool is not None  # guarded by prepare_call()
        decision = self.evaluate_policy(tool, params)
        if decision.outcome != "allow":
            action = "requires explicit approval" if decision.outcome == "ask" else "was denied"
            reason = f": {decision.reason}" if decision.reason else ""
            approval_hint = ""
            if decision.outcome == "ask" and decision.approval_token:
                approval_hint = (
                    f" Approve with `/policy approve {decision.approval_token}` "
                    f"or deny with `/policy deny {decision.approval_token}`."
                )
            message = (
                f"Policy {action} for tool '{name}' "
                f"(rule: {decision.rule_id or 'unnamed'}){reason}.{approval_hint}"
            )
            data: dict[str, Any] = {
                "decision": decision.outcome,
                "rule_id": decision.rule_id,
                "resource": decision.resource,
            }
            evidence: dict[str, Any] = {
                "kind": "tool_policy",
                "decision": decision.outcome,
                "rule_id": decision.rule_id,
            }
            if decision.approval_token:
                data["approval_token"] = decision.approval_token
                evidence["approval_token"] = decision.approval_token
            return ToolResult.policy_block(
                message,
                data=data,
                evidence=[evidence],
            )

        receipt = None
        if (
            exec_id is not None
            and self.receipt_store is not None
            and getattr(tool, "effect", "read") != "read"
        ):
            from nanobot.agent.tools.action_receipts import canonical_arg_hash, redact_preview
            from nanobot.agent.tools.context import current_request_context

            ctx = current_request_context()
            session_key = (ctx.session_key if ctx is not None else None) or f"{name}:{exec_id}"
            sender_id = str(ctx.sender_id) if ctx is not None and ctx.sender_id else ""
            arg_hash = canonical_arg_hash(params)
            replay_decision, receipt = self.receipt_store.begin(
                exec_id=exec_id,
                session_key=session_key,
                sender_id=sender_id,
                tool=tool.name,
                arg_hash=arg_hash,
                effect=str(getattr(tool, "effect", "read")),
                replay=str(getattr(tool, "replay", "never")),
            )
            if replay_decision == "mismatch":
                return ToolResult.retryable_error(
                    f"Error: execution id {exec_id} was reused with different "
                    "tool/arguments; receipt mismatch."
                )
            if replay_decision == "already_succeeded":
                return ToolResult(
                    "Replayed from receipt (not re-executed): "
                    + (receipt.outcome or "completed previously"),
                    status="success",
                    data={"receipt": "replayed", "exec_id": exec_id},
                    evidence=[{"kind": "action_receipt", "exec_id": exec_id, "replayed": True}],
                    postcondition="unchecked",
                )
            if replay_decision == "in_flight":
                return ToolResult.partial(
                    f"Execution {exec_id} is already in progress; not dispatched twice."
                )
            if replay_decision == "unknown":
                return ToolResult.partial(
                    f"Execution {exec_id} ended in an unknown state (crash after dispatch) "
                    "- manual review required; not auto-repeated."
                )

        try:
            outcome = adapt_legacy_tool_result(await tool.execute(**params))
            if receipt is not None:
                ok = outcome.status in ("success", "partial")
                self.receipt_store.complete(
                    exec_id,
                    status="succeeded" if ok else "failed",
                    outcome=redact_preview(str(outcome)) if ok else redact_preview(str(outcome)),
                    ok=ok,
                )
            if outcome.retryable and str(outcome).startswith("Error"):
                return self._replace_content(outcome, str(outcome) + hint)
            return outcome
        except Exception as e:
            if receipt is not None:
                self.receipt_store.complete(exec_id, status="failed", ok=False,
                                            outcome=redact_preview(f"exception: {e}"))
            return ToolResult.retryable_error(f"Error executing {name}: {str(e)}" + hint)

    @staticmethod
    def _replace_content(outcome: ToolResult, content: str) -> ToolResult:
        return ToolResult(
            content,
            status=outcome.status,
            data=outcome.data,
            evidence=outcome.evidence,
            side_effects=outcome.side_effects,
            postcondition=outcome.postcondition,
            retryable=outcome.retryable,
            exit_code=outcome.exit_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
        )

    def evaluate_policy(
        self,
        tool: Tool,
        params: dict[str, Any],
    ) -> PolicyDecision:
        """Evaluate configured policy after validation and before execution."""
        return self.policy.evaluate(tool.name, params, read_only=tool.read_only)

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
