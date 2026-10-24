# Read-only workers: bounded fan-out and one-shot chunks

- [x] Read guidance and trace worker, bridge, evidence, usage and sandbox paths.
- [x] Add queued bounded concurrency, coherent global/child sandbox limits and cancellation cleanup.
- [x] Add tool-free one-shot `ask`, shared budgets/accounting/evidence, and iteration cap 8/16.
- [x] Add deterministic fake-provider and safe Bubblewrap regression coverage.
- [x] Update docs; run touched-file lint, targeted suite, and full suite; review diff.

## Log

### 2026-10-24

Started clean on assigned `feat/worker-parallel-chunk`, continuing merged `d63e05efd`.
Read AGENTS.md and all three .agent guidance files plus previous worker TODO.
Actual source defaults max_workers=2 (request described 8); will set total ceiling
8, per-run concurrency 4, configurable global cap 8 (hard maximum 8), and separate
8 reserved child sandbox slots; parent sandbox cap remains 2. Concurrent requests
need request-local usage lists, not slices of the shared aggregate. No delegation,
external provider calls, config/secrets access, service changes, merge or push.

Implemented request-local usage (also aggregated once), per-run semaphore and
process-wide queued admission respecting the strictest active/requesting cap.
Queue deadlines/cancellation release both levels. Added `ask` using the same
reservation provider with tools=None, not a runner/registry/sandbox; bridge and
runner preserve failure events. Inline input now fails against the conservative
serialized request + output reservation instead of the arbitrary 8000-byte cap.
Docs explain routing, limits, and why reserved child sandbox slots cannot be
starved by awaiting parents. All eight slots were held simultaneously in real
safe-host Bubblewrap tests; subsequent waves and active-sandbox cancellation pass.

Initial checks:

```bash
uv run ruff check nanobot/agent/runner.py nanobot/agent/tools/_readonly_worker.py nanobot/agent/tools/codemode.py nanobot/agent/tools/_codemode_worker.py tests/agent/tools/test_readonly_workers.py tests/agent/tools/test_worker_parallel_chunk.py
# Initially caught exception naming and fixture/import lint; corrected locally.
# Latest run: All checks passed!

scripts/run-tests.sh tests/agent/tools/test_worker_parallel_chunk.py tests/agent/tools/test_readonly_workers.py tests/agent/tools/test_readonly_worker_review.py -q
# 65 passed, 1 existing fitz deprecation warning in 9.22s.

scripts/run-tests.sh tests/agent/tools/test_worker_parallel_chunk.py -q
# 25 passed in 2.69s (after active-child-sandbox cancellation/global wait additions).

scripts/run-tests.sh tests/agent/tools/test_worker_parallel_chunk.py tests/agent/tools/test_readonly_workers.py tests/agent/tools/test_readonly_worker_review.py tests/agent/tools/test_codemode.py tests/agent/tools/test_codemode_sandbox.py tests/agent/test_runner*.py tests/agent/test_loop_runner_integration.py tests/agent/test_subagent*.py tests/agent/tools/test_subagent_tools.py tests/config/test_tool_config_boundaries.py tests/config/test_run_presets.py -q
# 426 passed, 1 existing fitz deprecation warning in 24.12s.
# Then added ask disabled/recursion/missing-route and fail_on_tool_error cases.

git diff --check
# Passed.
```

Final validation (after all 30 new cases):

```bash
scripts/run-tests.sh tests/agent/tools/test_worker_parallel_chunk.py tests/agent/tools/test_readonly_workers.py tests/agent/tools/test_readonly_worker_review.py tests/agent/tools/test_codemode.py tests/agent/tools/test_codemode_sandbox.py tests/agent/test_runner*.py tests/agent/test_loop_runner_integration.py tests/agent/test_subagent*.py tests/agent/tools/test_subagent_tools.py tests/config/test_tool_config_boundaries.py tests/config/test_run_presets.py -q
# 431 passed, 1 warning in 25.09s.

scripts/run-tests.sh -q
# 3716 passed, 2 skipped, 1 warning in 169.00s.
# No failures, pre-existing or new. Previous merged-pass record: 3686 passed,
# 2 skipped, 1 warning; the +30 are this pass's new cases. The fitz API
# deprecation warning is unchanged. Safe host Bubblewrap regressions ran.
```

Final touched-file lint command above was rerun after the full suite: all checks
passed. `git diff --check` passed again. Reviewed scoped code/test/docs diffs;
docs distinguish preflight `ask` budget refusal from navigation's existing explicit
partial/error outcome on a refused later model reservation.

No live provider/production checks were performed. Passing tests do not establish
production effectiveness. In-flight cancelled remote calls without usage replies
remain unmeasurable; reservations are intentionally not refunded. Remote output
caps depend on provider compliance. Workers remain opt-in. Only generated Python
is Bubblewrap-isolated; model calls and bridge mediation remain host-harness work.
No new dependencies or live settings changed; no merge/push/deployment/restart.
