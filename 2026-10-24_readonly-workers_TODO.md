# Awaitable read-only Code Mode workers

- [x] Inspect instructions, runner, sandbox, preset, result persistence and accounting paths.
- [x] Implement opt-in scoped artifacts and minimal restricted workers.
- [x] Enforce shared budgets, deadlines, cancellation and bounded sandbox capacity.
- [x] Account child usage without mixing model attribution or double counting.
- [x] Add deterministic coverage and run targeted lint/regressions.
- [x] Document API, security boundary and limitations; review diff and commit locally.

## Review follow-up

- [x] Preserve discarded worker failures/partials/timeouts/budget outcomes in parent enforcement.
- [x] Capture and route ordinary top-level large tool/read results into scoped artifacts.
- [x] Add real-path regressions, run targeted and full-suite checks, document and commit locally.

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

### 2026-10-24 — independently reproduced review blockers

Read `/home/gergo/.nanobot/workspace/tmp/readonly-workers-review-2026-10-24.md`
and both supplied repro scripts; started from clean `e774c9ed2` on the assigned branch.
No other checkout, live config, secrets, providers, deployment or services changed.

1. Worker operations now use a runner-owned dispatch callback which records a
   nested parent `worker` event independently of script output/status handling.
   Failure/partial/timeout/capacity/budget/validation/cancellation outcomes remain
   subject to existing parent verification and fail-on-tool-error. Full outcome and
   evidence remain in scoped artifacts, with only compact evidence in events.
   Recovery matches a hash of explicit task + artifact selection + inline material;
   unrelated successful work cannot clear a worker failure. Child delegation remains
   prohibited before this dispatch, and routing/accounting are unchanged.
2. Moved capture from bridge-only dispatch into the common tool execution boundary.
   Ordinary top-level text/JSON results now receive bounded scoped references before
   model-context insertion, including original selected text/PDF/Office ranges before
   local character caps. Subsequent Code Mode can pass these handles directly to
   workers. Small results, visual blocks and disabled-mode behavior are preserved.
   Broader checks exposed mock registries accidentally activating worker state;
   activation now requires the explicit boolean `enable=True`.

Added 18 regressions in `tests/agent/tools/test_readonly_worker_review.py`, covering
real top-level read/tool-to-worker paths, discarded/caught failures, timeout, budget
exhaustion, cancellation, fail-on-tool-error, same-task recovery vs unrelated work,
error metadata, text blocks and opt-out compatibility. Adjusted one existing scope
assertion to inspect the original tool response and require parent partial completion
now that worker failure correctly triggers a verification turn. Two initial new-test
assertions were corrected for existing quota-message normalization and unknown usage
on a timed-out provider request; no provider behavior was changed.

Final validation:

```bash
uv run ruff check nanobot/agent/runner.py nanobot/agent/tools/codemode.py tests/agent/tools/test_readonly_worker_review.py tests/agent/tools/test_readonly_workers.py
# All checks passed.

scripts/run-tests.sh tests/agent/tools/test_readonly_worker_review.py tests/agent/tools/test_readonly_workers.py tests/agent/tools/test_codemode.py tests/agent/tools/test_codemode_sandbox.py tests/agent/test_runner*.py tests/agent/test_loop_runner_integration.py tests/agent/test_subagent*.py tests/agent/tools/test_subagent_tools.py -q
# 389 passed, 1 warning in 21.17s.

scripts/run-tests.sh -q
# 3686 passed, 2 skipped, 1 warning in 158.76s.
# Matches independent baseline 3668 passed/2 skipped plus 18 new tests; no new failures.
# Warning: existing fitz API deprecation surfaced by the new PDF-path fixture.

uv run --extra dev python /home/gergo/.nanobot/workspace/tmp/review_readonly_workers_probe.py
# LOST_FAILURE: partial_completion; nested (worker, partial); one child request.
# MAIN_OUTPUT: 1641 chars, scoped artifact reference present.
# Shared tool budget still allows one successful read, then rejects further attempts.

uv run --extra dev python /home/gergo/.nanobot/workspace/tmp/review_readonly_direct_probe.py
# 1968 chars into parent (formerly 128051); source_artifacts and artifact ID present.
# Tail stays out of the preview; regressions verify the full tail is accessible to a selected worker.

git diff --check
# Passed.
```

All provider calls in these checks use fakes; safe host bwrap tests ran successfully.
No live-model or production-effectiveness claim. Existing limitations remain:
run-scoped artifacts expire at run end, already-truncated upstream/extractor content
cannot be reconstructed, visual blocks intentionally retain their existing path,
and in-flight cancelled requests without usage replies cannot be measured.
No merge, push, deployment, restart, config/secrets access or provider change.
