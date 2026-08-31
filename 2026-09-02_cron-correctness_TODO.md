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

## Next session (same date) — #25 finish, #20 finish, #1 start

- User confirmed: deploy/restart is theirs; we merge and push.
- Created three topic branches off main 17b1f45f, each in its own git worktree:
  - `feat/cron-finalize` (/home/gergo/projects/fork-nanobot-25): separate agent/delivery timestamps, retire action.jsonl transactional mutation path, crash/interleaving tests, finish #25.
  - `feat/delegated-run-finalize` (/home/gergo/projects/fork-nanobot-20): final-synthesis terminal semantics, per-spawn token budget override, queue-full structured rejection, tests, finish #20.
  - `feat/memory-search` (/home/gergo/projects/fork-nanobot-1): built-in memory_search tool (exact search, structured filters, memory-file search, read-only), start #1.
- Spawned three Luna workers (cron-finalize, delegated-finalize, memory-search-tool); each syncs its own venv, works only in its worktree, and does not commit.
- After workers reply: review diffs, validate, merge each branch into main, push, post issue comments/closures, update TODO.

- Merged #25 final tranche as 9b06b10e (c04e8ad4) and closed issue #25.
- memory_search v1 merged earlier as 736a29bb; issue #1 commented with remaining semantic work.
- Waiting on the delegated-run (#20) worker.

- Finished #20: closed; merged as b9e1c4e5 (6f6489ea).
- #1: v1 memory_search shipped; issue stays open for semantic/ranking/indexing follow-ups.
- Cleaned up worktrees and local topic branches; CI green on all merged pushes.

### Merged to main (Gergo go-ahead)

- `1b0d7764` merge: Telegram reply diagnostics (#18)
- `e2069c47` merge: interactive policy approvals + mode wiring (#22)
- `b6ac6882` merge: incident-derived harness regressions (#23)
- `531e1f9b` merge: subagent run record path + enriched tool events (#11)
- `dd38634a` merge: memory_search v2 (index, paging, config) (#1)
- `3b2bdf87` merge: memory_read/memory_write tools (Phase A)
- `5958f20f` merge: Dream guardrails M1b-M1d
- Post-merge validation: Ruff clean; full suite 3129 passed, 1 warning.
- Topic branches deleted locally and remotely; main clean.
