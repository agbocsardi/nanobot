# Python codemode

- [x] Create `feat/python-codemode` from clean `main`; inspect execution and MCP paths.
- [x] Add fail-closed Bubblewrap Python worker with bounded async tool bridge.
- [x] Route nested calls through runner safeguards, context, receipts, and observability.
- [x] Defer MCP schemas from prompts when codemode is enabled; add bounded discovery.
- [x] Preserve structured MCP results and error state.
- [x] Inspect Pi's codemode tests at the user's request; add focused Python equivalents.
- [x] Document configuration, security boundary, and Python scripting API.
- [x] Run existing targeted checks plus sandbox/runtime/context-size checks; measure latency.
- [x] Commit feature, integrate current `main`, and verify newer failure/retention behavior.
- [x] Push verified `main` and fast-forward the clean deployment checkout.

## Sandbox binary follow-up

- [x] Reproduce distro/linuxbrew behavior and capability requirements before editing.
- [x] Add deterministic, configurable, validated binary selection and precise diagnostics.
- [x] Support modern Bubblewrap setup while dropping all worker capabilities before user code.
- [x] Add focused tests, run targeted checks, commit on `main`, and push (no deploy/restart).

## Log

### 2026-10-19

- Follow-up defect: bare PATH selection can choose unprofiled linuxbrew 0.11.2 instead of profiled `/usr/bin/bwrap` 0.9.0. Reproduced distro sandbox success, linuxbrew RTM_NEWADDR denial, denied loopback ioctl without CAP_NET_ADMIN, and successful ioctl with it. Confirmed actual exec labels (`nanobot-bwrap (unconfined)` vs `unconfined`) using a pipe-paused `--args` version probe. User requires direct `main` changes, tests, commit/push only; no profile/package/deployment/service changes.
- Follow-up fix: `tools.codemode.bwrapPath` defaults to `/usr/bin/bwrap`, never searches PATH, resolves symlinks, checks executability and Bubblewrap >= 0.9.0, and performs a bounded/cancellable probe of the actual exec AppArmor label via `/proc/<pid>/attr/apparmor/current`. Restricted hosts reject unconfined/uninspectable labels before namespace setup; errors identify the selected canonical binary, version, and actual policy/setup stage rather than blaming every exit on AppArmor.
- Modern setup now retains CAP_NET_ADMIN for loopback initialization and CAP_SETPCAP only for trusted bootstrap. Before generated code is compiled, the worker empties bounding/permitted/effective/inheritable/ambient capabilities and enforces no_new_privs. Real worker tests verify all sets remain empty after exec. Existing private namespaces/seccomp/resource limits are retained; startup failures never run unsandboxed.
- Real follow-up probes: with linuxbrew first in PATH, default codemode still uses profiled `/usr/bin/bwrap` and completes. Explicit unprofiled linuxbrew 0.11.2 now fails early with its exact path/version/unconfined label. The same modern binary succeeds under the existing `nanobot-bwrap` profile using a process-local `aa-exec -p nanobot-bwrap` probe; all five capability sets are zero and no_new_privs=1. Each profiled runtime probe took about 56 ms. This does NOT authorize the ordinary unprofiled linuxbrew invocation or modify the profile.
- Follow-up verification: touched-file ruff and `git diff --check` pass. Final targeted run: **1,086 passed, 1 pre-existing audioop deprecation warning, 37.30s**. Exact command: `scripts/run-tests.sh tests/tools tests/security tests/agent/tools/test_codemode.py tests/agent/tools/test_codemode_sandbox.py tests/agent/test_runner*.py tests/agent/test_mcp*.py tests/agent/tools/test_action_receipts.py tests/config tests/test_tool_contextvars.py tests/agent/test_subagent*.py tests/agent/tools/test_subagent_tools.py -q`.
- Remaining boundary: executable/runtime files remain trusted dependencies, not cryptographically authenticated or pinned across probe/exec; AppArmor/outer container policy may still deny required setup operations, now reported precisely. A chosen alternative still needs an authorized profile on restricted Ubuntu. No live configuration, deployment checkout, gateway process, system package, or AppArmor profile was changed for this fix.

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
- User requested merge and deployment-checkout update. Confirmed the clean deployment checkout is `/home/gergo/nanobot`, and the installed `nanobot-ai` tool is editable from there. `main` advanced with retention/false-success/verification/invalid-argument changes while this branch was in progress. No service restart or config change is planned.
- Committed the feature as `67609ba6f`, then integrated current `main` (`5af6fc661`). Resolved the runner conflict by retaining its four-value execution result and aligning nested targets with events. Child records preserve request order; caught/recovered errors no longer synthesize an unresolved outer failure. Nonzero command exits and ask-user control outcomes remain data/control signals, not Python exceptions.
- Integration verification: recorded a fresh baseline on clean current `main`; all 1,011 existing targeted tests passed before and after integration (`scripts/test-delta.sh`: NEW 0 / FIXED 0 / KNOWN 0). All 47 codemode tests passed, including five new integration cases for invalid arguments, cross-tool recovery/target alignment, nonzero exits, and ask-user control. Touched-file ruff passed.
- Merged integration commit `cd2201056` into `main` as `475adbcc8`, verified identical tested trees, and pushed `origin/main`. Fast-forwarded the clean `/home/gergo/nanobot` deployment checkout from `5af6fc661` to the code merge. No reset, reinstall, config change, or restart was needed/performed.
- Verified from `/home/gergo` with the installed `nanobot-ai` interpreter: imports resolve to `/home/gergo/nanobot`, a fresh sandbox returns `deployment sandbox ready` in 48 ms, and event/target alignment is intact. The existing gateway remains active with its original October 16 start time. A restart and opt-in config setting are still required to activate codemode in that running process.
