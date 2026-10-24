# Awaitable read-only Code Mode workers

- [x] Inspect instructions, runner, sandbox, preset, result persistence and accounting paths.
- [x] Implement opt-in scoped artifacts and minimal restricted workers.
- [x] Enforce shared budgets, deadlines, cancellation and bounded sandbox capacity.
- [x] Account child usage without mixing model attribution or double counting.
- [x] Add deterministic coverage and run targeted lint/regressions.
- [x] Document API, security boundary and limitations; review diff and commit locally.

## Log

### 2026-10-24

Clean assigned branch `feat/readonly-codemode-workers`. Minimal profile will expose only
selected artifacts (read/search) plus child-local Code Mode, not arbitrary host files/web.
Use explicitly configured subagent preset; fail closed if unavailable. No deployment.

Implemented `worker`, `artifact_put`, `artifact_read` bridge operations, a fresh
artifact-only registry, child-local Code Mode, and private run-scoped storage
(32 MiB/256 IDs, 16,000-byte inline JSON budget). Preserved original selected
text/PDF/Office ranges before existing local truncation. Original payloads over
the bridge frame size and large child computation results remain explicitly readable.
One active child/run, two process-wide, with two reserved child sandbox slots;
existing parent deadline unchanged. No retries/provider failover/foreground fallback.
Child usage is separately attributed and recorded incrementally in parent sessions.
No new dependencies declared; uv created the worktree-local environment.

Initial broader regressions exposed four old SimpleNamespace result doubles lacking
`worker_usage`; updated these consumers. Two new assertions were corrected to
respect the existing runner's failure-evidence/verification behavior (catching a
failed tool must not turn it into successful analysis). All targeted checks below
now pass; no remaining failures in these selections. Full repository suite and live
provider/production checks were not run. Safe existing host bwrap fixture ran, not
skipped; no host packages/security settings changed.

Exact validation commands and final outcomes:

```bash
uv run ruff check nanobot/agent/loop.py nanobot/agent/runner.py nanobot/agent/tools/codemode.py nanobot/agent/tools/_codemode_worker.py nanobot/agent/tools/_readonly_worker.py nanobot/agent/tools/_worker_artifacts.py nanobot/agent/tools/filesystem.py nanobot/utils/helpers.py tests/agent/tools/test_readonly_workers.py tests/agent/tools/test_subagent_tools.py
# All checks passed.

scripts/run-tests.sh tests/agent/tools/test_readonly_workers.py tests/agent/tools/test_codemode.py tests/agent/tools/test_codemode_sandbox.py tests/agent/test_runner*.py tests/agent/test_loop_runner_integration.py tests/agent/test_subagent*.py tests/agent/tools/test_subagent_tools.py -q
# 371 passed in 17.93s (final run).

scripts/run-tests.sh tests/utils/test_helpers.py tests/utils/test_usage.py tests/tools/test_tool_registry.py tests/tools/test_tool_loader.py tests/tools/test_filesystem_tools.py tests/tools/test_mcp_tool.py tests/config/test_tool_config_boundaries.py tests/config/test_run_presets.py tests/agent/test_tool_loader_scopes.py tests/cron/test_cron_run_usage.py -q
# 162 passed in 0.92s (after filesystem capture change).

scripts/run-tests.sh tests/config tests/harness/test_config_drift_regressions.py tests/agent/test_tool_loader_entrypoints.py -q
# 41 passed in 0.76s.

git diff --check
# Passed.
```

Limitations: no worker host-file/web access, recursive workers, persistent REPL,
or cross-run artifact recovery. Material truncated by upstream services/extractors
cannot be reconstructed. Unreported usage of cancelled remote requests is unknown;
requested output caps depend on provider compliance. Passing local tests does not
establish production effectiveness. No merge/push/deployment/restart performed;
future activation needs explicit opt-in plus a configured cheap subagent preset and
process restart. See `docs/codemode.md` for API and default limits.
