# Python codemode

- [x] Create `feat/python-codemode` from clean `main`; inspect execution and MCP paths.
- [x] Add fail-closed Bubblewrap Python worker with bounded async tool bridge.
- [x] Route nested calls through runner safeguards, context, receipts, and observability.
- [x] Defer MCP schemas from prompts when codemode is enabled; add bounded discovery.
- [x] Preserve structured MCP results and error state.
- [x] Inspect Pi's codemode tests at the user's request; add focused Python equivalents.
- [x] Document configuration, security boundary, and Python scripting API.
- [x] Run existing targeted checks plus sandbox/runtime/context-size checks; measure latency.
- [ ] Commit feature, integrate current `main`, and verify newer failure/retention behavior.
- [ ] Push verified `main` and fast-forward the clean deployment checkout.

## Log

### 2026-10-19

- User approved Python codemode on a new branch. Primary goal: keep MCP schemas out of the model context, discovering tools on demand.
- Plan: opt-in codemode, normal built-ins stay declared, MCP tools remain registered but are omitted from model definitions. Fresh Bubblewrap worker per script; parent owns tools and policies. No unsandboxed fallback.
- User installed Bubblewrap 0.9.0. Ubuntu's AppArmor restriction initially blocked namespaces; user installed the scoped `/etc/apparmor.d/nanobot-bwrap` profile after syntax validation. Sandbox launches now succeed; global AppArmor protection remains enabled.
- Added opt-in `tools.codemode.enable`. Built-ins remain declared; all `mcp_` schemas are deferred, including late registrations. Discovery is bounded, paginated, namespace-aware, and backed by the live run registry. Unknown-tool errors no longer dump the deferred catalog.
- Fresh stdlib-only Python workers use read-only runtime mounts, private namespaces, clean environment, capped writable tmpfs, seccomp (including no forks/threads/network sockets), and hard resource limits. No VM dependency or unsafe fallback. Address-space limits plus denied process creation avoid requiring delegated cgroups.
- Nested calls reuse `_run_tool` for validation/policy/workspace/lookup safeguards and receipts. Execution contexts are registry-bound and revoked on completion. Compact child events stay out of transcript tool results and do not shift outer hook pairings. Cancelled mutations record unknown receipt outcomes.
- MCP preserves structured CallToolResult and `isError`; read-only hints allow safe concurrency, unknown operations remain serial ordering barriers. Existing MCP reconnect/retry behavior is retained, not an exactly-once guarantee.
- Inspected Pi reference tests at `e86b054753b544600693384d3db4e3281ba86b60` in `/tmp/nanobot-pi-codemode-reference`. Added 42 focused checks for discovery, constant schema size, output/errors, concurrency, cancellation/admission, receipts/approvals, context isolation, and Linux isolation. Real worker descendants disappear on cancellation even if generated code clears its own parent-death signal.
- Validation: 1,063 targeted tests passed (tools, security, codemode, runner, MCP, config, action receipts, contextvars, subagents); touched-file ruff and `git diff --check` passed; CLI help smoke passed. Real local stdio MCP round-trip passed: deferred discovery → schema lookup → structured result → filtered scalar output. An initial text-only smoke response confirmed that `structuredContent` is optional; documented explicit JSON-text parsing rather than adding heuristic parsing.
- Final local startup benchmark: 20 fresh `return 1` calls, median 51.1 ms, p95 54.0 ms (min 50.1, max 75.2). This includes runner/bridge/worker startup, not external tool latency. No `uhl` benchmark or live LLM request was run.
- Context benchmark: 500 synthetic MCP tools produce 1,842,890 direct schema characters; codemode stays at 1,564 characters, identical to its no-MCP baseline (no other built-ins in that synthetic registry).
- Documentation: `docs/codemode.md`, linked from the configuration reference and docs index. Runtime remains opt-in; user's live config was not changed. Initial implementation was left uncommitted on `feat/python-codemode`.
- User requested merge and deployment-checkout update. Confirmed the clean deployment checkout is `/home/gergo/nanobot`, and the installed `nanobot-ai` tool is editable from there. `main` advanced with retention/false-success/verification/invalid-argument changes while this branch was in progress; integration must preserve those semantics. No service restart or config change is planned.
