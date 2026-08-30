# Cron Correctness and Maintenance TODO

## Checklist

- [x] Audit and close or update GitHub issues #18–#24 with commit/test evidence
- [x] Fix #13 cron list output for `silent`, `isolated`, and model preset values
- [x] Correct stale README claims without adding fork productization
- [x] Add minimal GitHub Actions CI for Ruff and pytest
- [x] Design #25 misfire, claim/lease, bounded concurrency, and diagnostics behavior
- [x] Implement and test the agreed #25 correctness tranche
- [x] Run targeted and full validation
- [x] Commit, merge to `main`, and push
- [ ] Deploy to `uhl`
- [x] #25 follow-up: separate agent-finished/delivery-finished timestamps; retire the legacy action log (feat/cron-finalize, uncommitted)

## Log

### 2026-09-02

- Recovered the accepted plan from the previous Prime Agent refinement.
- Confirmed a clean `main` at `3f1e2402` and created `feat/cron-correctness-and-maintenance`.
- Started parallel Luna audits for issue closure evidence, #13, README/CI, and #25 design.
- Closed #19, #21, and #24 with implementation evidence; posted exact remaining gaps on #18, #20, #22, and #23.
- Implemented #13 list visibility with 107 cron tests passing.
- Confirmed #25 needs staged misfire/diagnostics, bounded concurrency, and transactional claim work; stage 1 implementation is in progress.
- Replaced the stale upstream-facing README with an accurate private-fork overview and source workflow.
- Added minimal GitHub Actions CI for Ruff and pytest.

- Added durable per-occurrence claims in `23abface`, with lease renewal, expiry recovery, token-fenced completion, and fresh-state merging.
- Added nine focused claim tests; the full cron suite now passes 120 tests.
- Started stage 3 to expose misfire controls, diagnostic timestamps, delivery outcomes, and claim state through cron inspection.
- Exposed misfire controls and secret-free claim/lease diagnostics through `cron list`; added timeout and delivery-failure coverage.
- Repository validation passed: Ruff clean and 2,967 pytest tests passed (one existing aiohttp deprecation warning).
- Remaining #25 follow-up: separate agent-finished and delivery-finished timestamps, plus eventual retirement of the legacy action log.

### 2026-09-03

- Implemented the remaining #25 follow-up on `feat/cron-finalize` (worktree fork-nanobot-25, uncommitted):
  - Split delivery timing: `CronRunRecord.agent_finished_at_ms` /
    `delivery_finished_at_ms`, `CronJobState.last_delivery_at_ms`, persisted
    backward-compatibly (old stores load, serializer writes the new keys).
    Both runners stamp turn-finish and delivery-finish times; the service
    persists them from the callback's structured `CronRunResult` metadata.
  - Retired `action.jsonl`: every mutation (add/update/remove/enable/disable/
    register_system_job) is now transactional via `_mutate` — reload fresh
    `jobs.json` under the inter-process FileLock, apply, atomic save.
    `_merge_action`/`_append_action` deleted; leftover action.jsonl is ignored
    and removed on load. Claim finalization uses the same save helper.
  - Surfaced the new timestamps in `cron list` text + structured data.
  - Tests: +6 transaction (concurrent add/update/delete across instances,
    threaded concurrency, crash-window/no-resurrection, leftover-dot-tmp,
    leftover action log), +3 deterministic timestamp, +3 runner timing,
    +2 tool-list surface, +1 bound-runner timing.
  - Validation: full suite 2892 passed / 2 skipped; Ruff clean.
- Merged as `f355e16b` and pushed `origin/main`; deployment is blocked because hostname `uhl` did not resolve from this session.
