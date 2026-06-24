# Nanobot fork improvement — TODO

Source: `projects/stack/nanobot-fork-improvement.md` (personal vault).

## Checklist

### Phase 1 — Stop noise
- [x] Fix silent cron turns: suppress agent-generated "done"/fallback output for scheduled turns
  - Added `CronPayload.silent` (new field, defaults False — no migration, no regression)
  - Exposed `silent` in cron tool schema; threaded through `add_job` / `update_job`
  - `bound_runner` tags cron turns with `CRON_SILENT_META`; `loop._dispatch` honors it
  - `[SILENT]` exact-trimmed marker also suppresses (cron turns only)
  - Helper `cron_suppress_success_delivery()` + tests in `tests/cron/test_session_turns_silent.py`
- [x] Audit cron jobs for silent-intent leaks / suppression hacks
  - No `[SILENT]` hacks in repo code (was prompt-level; now framework-honored)
  - Heartbeat `set_suppress_delivery` + `evaluate_response` is a deliberate separate gate — left as-is
  - Marked legacy `deliver`/`channel`/`to` fields DEAD in `types.py` (don't reuse for silent)

### Phase 2 — Isolate background models
- [ ] Extract `run_with_preset(model, max_tokens, max_iterations)` primitive from Dream
- [ ] Wire cron, subagent, heartbeat through it (never inherit ambient chat model)
- [ ] Default background preset: cheap model, low iteration cap (~50)

### Phase 3 — Make burn observable
- [x] Re-score upstream "real usage forwarding" (commit 9814a3b9)
  - Moot in this fork: `api/` / OpenAI-compatible server was removed in the decouple pass
- [x] Persist background run records for cron + subagents
  - Shared JSON writer in `nanobot/utils/run_records.py`
  - Cron `runs/{run_id}.json`: kind, prompt/job metadata, model/provider, usage, silent flag, status/response
  - Subagent `subagents/{task_id}.json`: kind, prompt/task, params, model/provider, usage, iterations, tool events, status/result
- [ ] Add run-type / preset attribution once Phase 2 `run_with_preset` exists
- [ ] Backfill a few recent sessions to validate schema

### Phase 4 — Slim startup context
- [ ] Compact unprocessed sessions with existing consolidation before startup injection
- [ ] Preserve message content + tool results; collapse adjacent short turns where safe
- [ ] Measure context token savings on a realistic startup batch

### Phase 5 — Indexed history lookup
- [ ] Add `memory_search` tool: literal/exact search over `history.jsonl` with citations
- [ ] Return session key + excerpt + timestamp; agent decides whether to load full session
- [ ] Defer semantic search to a later iteration

---

## Log

### 2026-06-24
- Phase 1 complete. Modeled silent cron as a new `CronPayload.silent` field rather than
  repurposing the legacy `deliver` field. Reason: `_normalize_agent_turn_job` force-sets
  `deliver=False` on every bound job, so reusing it would have silently muted every existing
  reminder. Marked `deliver`/`channel`/`to`/`channel_meta` as DEAD in `types.py` for later
  removal. Suppression is success-only (error path still publishes), cron turns only (normal
  chat never suppressed), exact `[SILENT]` match only (no fuzzy). 117 cron/loop tests pass.
- Note: decided `silent` over `deliver` despite planning to decouple from upstream — `deliver`
  carries inverted historical meaning ("push to WhatsApp") that would need active un-teaching;
  `silent` reads as intent. Dead fields kept (not ripped) because fork hasn't fully diverged and
  ripping upstream-owned fields on heavily-edited files costs more than it saves.
- Next: Phase 2 (background model isolation) or Phase 3 (usage observability).

### 2026-06-24 (continued)
- Added shared background run-record logging for cron + subagents. Cron records now include
  `kind="cron"`, silent flag, provider/model, and usage for every run, including silent runs
  (delivery suppression only skips chat publish, not usage capture). Subagents now write
  `subagents/{task_id}.json` with prompt/task, params, provider/model, usage, iterations,
  tool events, status, and result. Cron and subagent records share `nanobot/utils/run_records.py`.
- Decision: no usage rollup command. Data is persisted for debugging and future preset attribution;
  rollups stay YAGNI until explicitly wanted.
- Checks: targeted lint + `147 passed in 2.05s` across new tests, cron suite, subagent suite,
  and loop cron timezone test.
