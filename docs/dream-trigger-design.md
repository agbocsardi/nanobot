# Dream Trigger Design — M2: Hybrid Trigger Policy

Date: 2026-09-02
Status: design only — no runtime code changes in this branch
Scope: `fork-nanobot-dt` (branch `docs/dream-trigger-design`, base `main d203bf19`)
Source item: M2 in `2026-06-16_decouple_TODO.md` — "Change Dream trigger policy from
pure cron to hybrid turn-count + idle timer + manual, with compaction as optional extra."

---

## 1. Current trigger reality

Dream currently has exactly two triggers, and both funnel into the same guarded entry
point, `MemoryStore.run_dream` (`nanobot/agent/memory.py:986`).

### 1.1 Cron system job (the only automatic trigger)

- Registered only by the gateway runtime in `_run_gateway` — `nanobot/cli/commands.py:1024-1036`.
  `config.agents.defaults.dream.enabled` (default `True`) gates registration; the schedule
  comes from `DreamConfig.build_schedule()` (`interval_h`, default 2h, or legacy `cron`
  expression override) — `nanobot/config/schema.py:53-83`.
- When the schedule fires, `CronService._execute_job` invokes the gateway's
  `on_cron_job` callback (`nanobot/cli/commands.py:799-847`). The `dream` branch is an
  internal job: it does **not** go through the agent loop/bus. It resolves the Dream run
  preset (`_dream_snapshot()` → `preset_helpers.build_run_provider_snapshot(config, "dream", ...)`)
  and calls `store.run_dream(...)` with a `run=lambda prompt: agent.process_direct(prompt, session_key=MemoryStore.dream_session_key(), ephemeral=True, tools=store.build_dream_tools(), ...)`, then in `finally` runs `store.compact_history()` and
  `prune_dream_sessions(...)`.
- Enforced protections: the cron tool refuses to remove the protected `dream` job
  (`nanobot/agent/tools/cron.py:446-465`); `register_system_job` is idempotent on restart.

### 1.2 Manual trigger (`/dream`)

- `cmd_dream` — `nanobot/command/builtin.py:321-388`. It spawns `asyncio.create_task(_run_dream())`,
  replies "Dreaming...", and then publishes the result to the originating chat. Same guarded
  `store.run_dream(...)` call shape (camelCase guardrail limits, `commit_prefix="dream: manual run"`),
  same `finally` cleanup (`compact_history()` + `prune_dream_sessions`).
- Slash surface also includes `/dream-log` and `/dream-restore` (read/inspect only).

### 1.3 The guarded core they share

`run_dream` (M1a–M1d, shipped on `main`):

1. Builds the prompt from history newer than `memory/.dream_cursor` via
   `build_dream_prompt(max_entries=max_batch_size)`; returns immediately with
   `reason="nothing_to_process"` when there is nothing new (cheap no-op).
2. Snapshots memory surfaces byte-for-byte (`capture_dream_snapshot`).
3. Runs the provided LLM call under `timeout_s`, `max_iterations` (via the runner),
   and M1b limits `max_changed_files` / `max_diff_chars`.
4. Only a fully validated, completed run advances the cursor and auto-commits;
   timeout / exception / unexpected stop reason / limit violation roll back the
   snapshot and leave the cursor untouched.
5. Logs and records the outcome (`DreamRunResult`, `_log_dream_outcome`,
   `resp.metadata["_dream_run"]`, session metadata `_last_dream_run`).
6. Returns a result, never raising for LLM/tool failures.

### 1.4 Gaps this design closes

- **No turn-count trigger, no idle timer, no compaction hook.** Dream only fires on the
  gateway's cron schedule (or `/dream`). Interesting but quiet memory work never happens
  between 2h ticks; sessions that reset `/new` frequently or a deploy that keeps a gateway
  up for days still get periodic consolidation, but nothing follows actual usage.
  Also note the REPL (`nanobot` interactive) never registers or starts the dream cron job —
  Dream is gateway-only except for manual `/dream`.
- **No single-flight dedup.** Cron and a manual `/dream` can theoretically overlap today;
  both snapshot and run concurrently, risking cursor/commit races.
- **No bounded-budget gate.** The cron path runs `process_direct` regardless of whether a
  user turn is in flight. `process_direct` shares the per-session lock but does **not**
  acquire the loop's `_concurrency_gate` semaphore (only `_dispatch` does, loop.py:1186),
  so concurrent user turns and Dream compete for the same provider/model without a budget rule.

---

## 2. Proposed hybrid trigger design

### 2.1 Config shape — `dream.triggers.*`

New nested `DreamTriggersConfig` under the existing `DreamConfig` (camelCase aliases via the
existing `Base` machinery, same as every other config field):

```json
{
  "agents": {
    "defaults": {
      "dream": {
        "enabled": true,            // unchanged master switch: registers the cron job too
        "intervalH": 2,             // unchanged cron cadence when triggers.cron is on
        "maxBatchSize": 20,         // unchanged (M1a-M1d guardrails)
        "maxIterations": 10,
        "timeoutS": 300,
        "maxChangedFiles": 8,
        "maxDiffChars": 32000,
        "triggers": {
          "cron": true,             // keep the existing cron system job (ON by default)
          "everyNTurns": 0,         // 0 = off; suggested 8-12
          "idleIntervalH": 0,       // 0 = off; suggested 6-12
          "onCompaction": false,    // optional bonus: run after an auto-compaction archive
          "cooldownMinutes": 30     // minimum gap between any two automatic Dream runs
        }
      }
    }
  }
}
```

Field semantics:

| Field (camelCase) | Default | Meaning |
|---|---|---|
| `cron` | `true` | Register the existing Dream cron system job. When off, no cron trigger (the master `enabled` switch still gates everything). |
| `everyNTurns` | `0` | Run Dream after N completed user turns (0 = disabled). Suggested 8–12 per the M2 notes. |
| `idleIntervalH` | `0` | Run Dream at most once per N hours, only when the gateway is idle (0 = disabled). Suggested 6–12. |
| `onCompaction` | `false` | Bonus: after an auto-compaction archive completes, mark Dream due (subject to the same idle/cooldown gates). |
| `cooldownMinutes` | `30` | Minimum minutes between automatic Dream runs. Manual `/dream` bypasses it. |

Defaults preserve current behavior exactly: `cron` is the only trigger on, and `cooldownMinutes`
never interferes with a 2h cron schedule; every other trigger is off.

### 2.2 Where trigger state lives

In-process, on the `AgentLoop`, in a new small coordinator module
(`nanobot/agent/dream_triggers.py`). Pattern follows the existing in-loop components
(`AutoCompact`, `CronTurnCoordinator`): constructed in `AgentLoop.__init__`, driven from
`run()`'s existing ticks, drained via `_schedule_background`.

State (all in-memory, restart-reset — see §3.1):

- `turns_since_dream: int` — completed user turns since the last Dream cycle.
- `last_dream_finished_mono: float | None` — `time.monotonic()` at the end of the last
  Dream cycle (any outcome, including `nothing_to_process`).
- `due: dict[str, bool]` — pending reason flags (`cron`, `n_turns`, `idle`, `compaction`).
- `_running: bool` + `_run_lock: asyncio.Lock` — single-flight.
- Optional (deferred) durable mirror: `last_dream_finished_ms` in a small workspace state
  file, or on the cron job's state, to keep the idle cadence across restarts.

The Dream cursor (`memory/.dream_cursor`) stays the durable "what has been dreamed" source;
nothing about the cursor semantics changes.

### 2.3 How each trigger invokes the guarded `run_dream`

All triggers call one coordinator entry point; the coordinator owns the run args, the
`finally` cleanup, and the gating rules:

```python
await coordinator.run_once(reason: str, *, force: bool = False)
# -> resolves run preset (cron path reuse), calls store.run_dream(...) with the
#    exact guarded args from today's cron branch, then compact_history() +
#    prune_dream_sessions() in finally.
```

Trigger wiring:

1. **Cron (unchanged registration, delegated execution).** `cli/commands.py` keeps
   registering the `dream` system job and keeps its `on_cron_job` dream branch — but the
   branch now calls `agent.dream_triggers.run_once("cron")` instead of inlining
   `store.run_dream`. `_dream_snapshot()`/preset resolution stays where it is and is passed
   in (the coordinator reuses it verbatim). `compact_history`/`prune_dream_sessions` move
   into the coordinator's `finally` (delete the duplicated copies from both call sites).
2. **Every-N-turns.** Hook at the end of a completed **user** turn. Definition of user turn:
   a bus-inbound message processed through `_dispatch`/`_process_message` that is not a
   cron turn (`_cron_trigger`), not a heartbeat/system turn (`_interaction_mode`), and not a
   slash command dispatched inline. (CLI REPL turns count; see §5.) When
   `turns_since_dream >= everyNTurns`, mark `due["n_turns"]` and hand a background coroutine
   to `_schedule_background`; the run itself is gated by §2.4, so a busy moment defers the
   run instead of stealing budget mid-turn.
3. **Idle timer.** Piggyback the existing `consume_inbound(timeout=1.0)` timeout branch in
   `AgentLoop.run()` (loop.py:1103, same spot that already calls
   `auto_compact.check_expired(...)`). When `idleIntervalH > 0` and
   `now - last_dream_finished_mono >= idleIntervalH` and the loop is idle (see §2.4),
   mark `due["idle"]` and run. This is a cheap per-second check — no new timer/task.
4. **Manual `/dream`.** `cmd_dream` delegates to `coordinator.run_once("manual", force=True)`,
   keeping its "Dreaming..." ack, result reply, and commit prefix. Manual bypasses the
   cooldown and the no-turn-in-flight rule but still respects single-flight (replies
   "Dream already running" instead of stacking).
5. **Compaction bonus (optional).** `AutoCompact._archive` calls
   `loop.dream_triggers.note_compaction()` after a successful archive; that marks `due` and
   falls through the same gates. Deferred by default (`onCompaction=false`).

### 2.4 Dedup / cooldown / bounded-budget rules

- **Single-flight.** `run_once` first acquires `_run_lock`. If a Dream is already running,
  non-forced triggers mark their reason `due` and return (cron stays "skipped/no-op" from
  the cron service's perspective — the job record says it ran, which matches today's
  semantics); `force=True` (manual) reports "already running" instead.
- **Cooldown.** Non-forced runs skip when `now - last_dream_finished_mono < cooldownMinutes`;
  the reason stays `due` until the cooldown passes, at which point the next tick/cron fire
  consumes it.
- **No-turn-in-flight gate.** A non-forced run starts only when the loop is idle:
  `len(_active_tasks) == 0` and no `_pending_queues` keys (same signal the loop already uses
  to defer cron turns and skip auto-compaction). This is the enforcement point for the
  bounded budget, because `process_direct` deliberately bypasses `_concurrency_gate`.
- **Nothing-to-process re-arms.** Any completed cycle — including `reason="nothing_to_process"` —
  resets `turns_since_dream` and refreshes `last_dream_finished_mono`, preventing
  instant-refire loops when there is no new history for the cursor.
- **Cron deferral policy.** When cron fires while a user turn is active, the run is deferred
  to the next idle tick (preferred) — but only for a bounded window (e.g. `misfire_grace_ms`,
  already part of the cron job). Past that window the occurrence is dropped (existing
  `misfire_policy` machinery) rather than queued forever.

### 2.5 Behavior-change table

| Scenario | Today | After (defaults) |
|---|---|---|
| Gateway up, cron 2h | Dream every 2h | identical |
| `/dream` while cron running | possible overlap/race | single-flight, "already running" |
| REPL mode | only manual `/dream` | identical (new triggers default off) |
| User configures `everyNTurns: 10` | n/a | Dream after 10 user turns, cooldown-gated |
| User configures `idleIntervalH: 8` | n/a | Dream at most every 8h when idle |

---

## 3. Edge cases

### 3.1 Gateway restarts

- All coordinator counters reset (`turns_since_dream=0`, `last_dream_finished_mono=None`,
  `due` cleared). The durable surfaces are untouched: `.dream_cursor` is on disk, the cron
  job persists in `workspace/cron/jobs.json`, and `register_system_job` re-registers
  idempotently. Worst case: Dream is delayed until the next cron fire or the next N-turns
  threshold — never lost permanently, never double-run (cursor won't regress).
- Consequence accepted: the idle cadence restarts from process start, so a restart right
  after an idle-triggered Dream means the next idle Dream is up to `idleIntervalH` away.
  Optional v1.5: persist `last_dream_finished_ms` so idle cadence survives restarts.

### 3.2 Overlap with active user turns

- The no-turn-in-flight gate (§2.4) is checked at run-start time, and all automatic reasons
  are deferred (run at the next idle tick) rather than dropped, within the cron grace
  window. The N-turns trigger only *schedules* a background run after the triggering turn
  completes — it never competes with that turn's own budget.
- Mid-turn injection (`_pending_queues`) and `_session_locks` stay untouched; Dream runs in
  its own `dream:` session key (as today), so it never corrupts a user session.

### 3.3 Bounded budget

- Only run when no turn is in flight or after an idle gap; the idle tick is the natural
  quiet window. This also protects `NANOBOT_MAX_CONCURRENT_REQUESTS`: bus turns acquire
  `_concurrency_gate`, so the coordinator's idle check plus single-flight means at most one
  extra LLM consumer (Dream) exists when the gate is uncontented.
- Manual `/dream` can run while the loop is busy — this matches today's semantics and is
  explicit user intent; M1a–M1d limits still bound it.

### 3.4 Interplay with M1b–M1d rollback

- Unchanged: every trigger path funnels through the same `run_dream`, so timeout,
  exception, `max_iterations`, `max_changed_files`, and `max_diff_chars` outcomes roll back
  identically regardless of which trigger started the run. A hybrid-triggered Dream that is
  interrupted (e.g. gateway shutdown mid-run) still rolls back through the same snapshot
  machinery.
- One hardening gap surfaced by this design: `run_dream` does not currently catch
  `asyncio.CancelledError` (it is a `BaseException`, so the `except Exception` branch at
  memory.py:1039 misses it). A cancelled Dream leaves partial surface edits with no rollback
  and no cursor advance. Recommend folding a CancelledError → rollback-and-re-raise branch
  into M2 (one line in `run_dream`), so shutdown-time cancellation during a hybrid trigger
  is safe too.

### 3.5 Duplicate reasons / multiple channels

- `due` is a set of reason flags, so cron + N-turns + idle stacked together collapse into
  one run; after the run all flags clear and counters reset.
- Multi-channel traffic counts toward `turns_since_dream` across all channels (the flag
  asked for "every N user turns across all channels"), consistent with the cursor being a
  global history offset.

---

## 4. Implementation plan

### Files to touch

1. `nanobot/config/schema.py` — add `DreamTriggersConfig` and
   `DreamConfig.triggers: DreamTriggersConfig = Field(default_factory=DreamTriggersConfig)`
   (camelCase aliases automatic via existing `Base` machinery).
2. `nanobot/agent/dream_triggers.py` (new) — `DreamTriggerCoordinator`: state, `run_once`,
   gating rules (`_run_lock`, cooldown, idle check, no-turn-in-flight), reason flags,
   `note_user_turn_completed`, `note_compaction`, `manual`; owns the guarded
   `store.run_dream` call (args lifted verbatim from the current cron branch in
   `cli/commands.py`), the `finally` cleanup (`compact_history` + `prune_dream_sessions`),
   and preset resolution (`_dream_snapshot` moved here or passed in).
3. `nanobot/agent/loop.py` — construct coordinator in `__init__` (expose
   `loop.dream_triggers`), hook the idle tick in `run()`'s consume-timeout branch, hook
   user-turn completion at the end of `_dispatch`, re-export config wiring in `from_config`.
4. `nanobot/cli/commands.py` — `on_cron_job` dream branch delegates to
   `agent.dream_triggers.run_once("cron", ...)`; keep cron job registration as-is; drop the
   duplicated inline `store.run_dream` + cleanup there.
5. `nanobot/command/builtin.py` — `cmd_dream` delegates to
   `coordinator.run_once("manual", force=True)`; keep the "Dreaming..." ack, result reply,
   "already running" case, and `commit_prefix="dream: manual run"`.
6. `nanobot/agent/autocompact.py` — optional `on_archive_done` hook → `note_compaction()`
   (only when `triggers.onCompaction` is on).
7. `nanobot/agent/memory.py` — optional hardening: CancelledError rollback branch in
   `run_dream` (§3.4).

### Test list

- `tests/config/test_dream_config.py` — triggers defaults (cron on, rest off), camelCase
  aliases, back-compat with configs that predate `triggers`, cooldown field bounds.
- `tests/agent/test_dream_triggers.py` (new) — pure unit tests on the coordinator with a
  fake `run` callable:
  - turn counter increments only for user turns (cron/heartbeat/command turns excluded);
  - threshold → `due["n_turns"]` and background schedule;
  - single-flight: second trigger while running defers / manual reports "already running";
  - cooldown blocks automatic triggers, manual bypasses;
  - idle tick fires only after `idleIntervalH` and only when idle; restarts reset state;
  - `nothing_to_process` re-arms counters without refire;
  - all paths call `run_dream` exactly once and always run `compact_history`/`prune` cleanup.
- `tests/agent/test_dream_guardrails.py` — regression: unchanged, must stay green (M1b–M1d).
- `tests/agent/test_dream.py`, `tests/agent/test_dream_session.py` — regression.
- `tests/command/test_builtin_dream.py` — `/dream` through coordinator: ack, result reply,
  "already running", manual force bypasses cooldown.
- `tests/cron/test_cron_service.py` (or a small new case) — cron dream job delegation still
  executes exactly one guarded run; deferred-when-busy and misfire-grace behavior.
- Gateway smoke (manual, no CI): start gateway with `everyNTurns: 3`, send 3 messages,
  observe one Dream run record; no overlap with an active turn.

---

## 5. Decisions needed from Gergő

1. **Trigger defaults.** Confirm: keep `cron=true` as the only default-on trigger
   (pure-cron behavior preserved) and ship `everyNTurns` / `idleIntervalH` default 0
   (off). Recommended when enabled: `everyNTurns: 10`, `idleIntervalH: 8`,
   `cooldownMinutes: 30`.
2. **"User turn" definition.** Bus-inbound, non-cron, non-heartbeat, non-command turns —
   including CLI REPL turns. OK? (Alternative: exclude `cli` channel.)
3. **Idle semantics.** Strict: automatic Dream runs only when zero turns are in flight
   (defer otherwise), vs. allow Dream to interleave at low priority. Recommend strict defer.
4. **Cron deferral.** When the cron Dream fires during an active user turn: run at the next
   idle tick within the misfire grace window (recommended) vs. drop the occurrence.
5. **Manual overlap.** `/dream` while a user turn is active: allow (today's behavior,
   recommended) vs. defer.
6. **State persistence.** In-memory counters v1 (recommended) vs. persist
   `last_dream_finished_ms` across restarts for idle-cadence continuity.
7. **Compaction bonus.** Include `onCompaction` in v1 (recommended: it is ~10 lines) or
   defer.
8. **CancelledError hardening.** Fold the `run_dream` CancelledError rollback into M2
   (recommended) or leave for M5 (worktree-based Dream).
9. **REPL parity.** Hybrid triggers live in `AgentLoop`, so they will activate in REPL mode
   too once wired. Confirm that is desired (today Dream is gateway-only apart from `/dream`).
