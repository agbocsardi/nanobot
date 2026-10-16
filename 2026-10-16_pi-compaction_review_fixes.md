# Compaction adversarial-review fixes

Scope: F1–F9 in order; no commit/merge/push. Existing uncommitted implementation preserved. Each bounded work turn records its evidence and checks here. Full-suite comparison baseline: 3448 passed / 4 failed / 2 collection skips.

## Progress

- [x] F1: checkpoint-safe active-file bound, lossless archive, projection invariant, measurements
- [x] F2: remove dead code/tests; correct stale comments
- [x] F3: provider/session dependency inversion
- [x] F4: invalid-checkpoint recovery
- [x] F5: stable identity invariant
- [x] F6: unified governed-payload composition
- [ ] F7: explicit private-field boundary
- [ ] F8: retire or implement the legacy message-count replay knob
- [ ] F9: pin memory-extraction versus audit archival and its retention contract
- [ ] Final focused/full suites, baseline delta, lint and diff checks

## F1 — completed (first bounded work turn)

Confirmed: normal saves no longer enforce a file cap. Correction to the review: `AutoCompact.prepare_session()` returns no extra summary when a checkpoint exists; `AutoCompact._archive()` still invokes `Consolidator.compact_idle_session()`. That branch prevents duplicate injection, not idle compaction, so it will not be changed for F1.

Implementation choice: enforce the cap centrally on session save. Only checkpoint-hidden messages may leave the active file; system messages and the entire retained suffix must survive. Archive callbacks must succeed before mutation. Use a lossless, fsynced archive of the existing redacted JSON records rather than `MemoryStore.raw_archive()`, which deliberately truncates/formats text and cannot preserve the original audit representation. Explicit heartbeat retention remains a separate production caller of `retain_recent_legal_suffix()` (`nanobot/cli/commands.py`).

### Changes and proof

- `nanobot/session/manager.py:563` — `Session.enforce_file_cap()` now validates the checkpoint, selects only non-system records strictly before `first_kept_entry_id`, calls the archive callback **before mutation**, and never drops the retained entry or any later message. It adjusts index-based memory-flush/legacy cursors and removes superseded checkpoints whose references moved out of the active transcript. Missing callback/checkpoint means no destructive fallback.
- `nanobot/session/manager.py:802,840` — all `SessionManager.save()` callers now share the cap under the canonical-file lock. `_archive_session_prefix()` uses the existing `atomic_write_lines()` facility with fsync to persist full redacted records in content-addressed batches under `sessions/archive/<safe-key>/`. Repeating the same archive is idempotent. The callback preserves IDs and call/result structure, unlike the deliberately bounded/flattened `MemoryStore.raw_archive()`; it does not feed a new topic-memory extraction path.
- `nanobot/agent/memory.py:1666` — exceeding the message limit also requests the existing checkpoint operation when token estimates are low. This avoids indefinite growth of short-message sessions that have never crossed a token threshold. Failed summarization still cannot authorize dropping unsummarized context.
- `nanobot/agent/loop.py:2559` — final-turn persistence also respects the checkpoint's committed `source_end_id` watermark. Deduplicating only against IDs remaining in the active file could otherwise re-append already committed, archived tool batches during a long turn.
- Updated configuration documentation with audit location and soft-cap semantics. Reworked five existing retention assertions/names to test the new safe contract; no existing test cases were deleted.

Seven new tests in `tests/session/test_compaction_retention.py`:

1. `:22` — 5,000-message save trims to 2,000, preserves the retained ID and exact projected history, and reconstructs every original from archive plus active file; reload parity checked.
2. `:45` — system messages and the complete retained suffix survive even when they exceed the cap.
3. `:60` — missing/failing archive callback leaves original state intact.
4. `:71` — repeating an archive does not create duplicate batches.
5. `:79` — final save cannot re-append an archived committed run prefix.
6. `:99` — archived tool calls/results retain existing redaction, IDs and call/result linkage; arguments/results/hidden reasoning secrets do not leak into the archive.
7. `:122` — message-count pressure creates a checkpoint below the token threshold before trimming becomes legal.

Deliberately retained: `retain_recent_legal_suffix()` is still called by the explicit heartbeat cleanup policy (`nanobot/cli/commands.py:977`), not the normal session cap. F2 will assess its remaining legacy tests separately. The cap is necessarily soft when indispensable projected context/system instructions exceed it, or summarization fails: the review explicitly forbids dropping those records. Audit storage intentionally retains history; this bounds the active working set, not total historical disk consumption. A failed active-file write after archival can leave an extra safe copy in the archive; records are never removed before archival succeeds.

### Measurements (synthetic, not a private/live session)

Benchmark: 5,000 alternating user/assistant records, each with `f'{i}: ' + 'synthetic audit evidence ' * 24`; checkpoint first retained index 4,000. Fifteen warm-filesystem samples per state; medians. Before = same code with cap disabled for the controlled benchmark; after = default cap enabled. Load explicitly invalidates the manager cache each sample; save is the normal per-turn `SessionManager.save(fsync=False)` API, not full model/turn latency. Both states assert identical projected context. Executed with `uv run python` in an automatically removed synthetic temporary workspace. Machine-readable results: `/tmp/pi-compaction-retention-benchmark.json`.

| Metric | Before | After |
| --- | ---: | ---: |
| Active records | 5,000 | 2,000 |
| Active JSONL bytes | 3,661,803 | 1,465,413 |
| Median load | 30.281 ms | 12.129 ms |
| Median per-turn save | 40.650 ms | 16.263 ms |

One-time trim plus durable archive: **65.877 ms**; archive size **2,196,452 bytes**. Steady-state active file/load/save cost decreased about 60%; retained projection was unchanged.

### Verification and baseline delta

Targeted F1 checks: **138 passed**.

Focused compaction suite:

```bash
scripts/run-tests.sh tests/session tests/agent/test_runner*.py tests/agent/test_loop_runner_integration.py tests/agent/test_loop_consolidation_tokens.py tests/agent/test_consolidator.py tests/agent/test_consolidation_ratio.py tests/agent/test_auto_compact.py tests/agent/test_autocompact_unit.py tests/command/test_compact_command.py tests/agent/test_vision_handoff.py -q --disable-warnings --tb=short
```

**401 passed, 0 failed, 0 skipped** (11.30 s).

Memory-capped full suite (same temporary timeout overlay and 10-second guard as the untouched baseline):

```bash
TIMEOUT_SITE=$(uv run --with pytest-timeout python -c 'from pathlib import Path; import pytest_timeout; print(Path(pytest_timeout.__file__).parent)')
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$TIMEOUT_SITE" scripts/run-tests.sh tests --import-mode=importlib --timeout=10 --timeout-method=signal -q --disable-warnings --tb=short
```

**3482 passed / 3 failed / 2 collection skips** (87.29 s), versus **3448 / 4 / 2** baseline: **+34 passed, −1 failed, skips unchanged; no new failing node IDs**. The three failures remain the baseline email polling timeouts; baseline's unrelated subagent lineage failure did not recur. Against the pre-review implementation, F1 adds seven passing test cases (3485 collected versus 3478); renaming existing cases does not remove coverage. Logs: `/tmp/pi-compaction-review-focused.txt`, `/tmp/pi-compaction-review-full.txt`.

Lint of all changed Python plus new compaction fixtures and `git diff --check` both pass. No commit, merge or push. F2–F8 remain pending; this is not a claim that the entire adversarial review is resolved.

## F2 — completed (bounded F2/F3 work turn)

Confirmed the three runner helpers and their constants have no production callers. Also identified the dead `Consolidator._prompt_diagnostics()` helper. Removing these plus `pick_consolidation_boundary()`, and only tests that invoke the retired behavior; live orphan repair, normalization, payload-fit, role repair, and image-budget coverage stay. The unused replay-boundary helper/parameter are reserved for F8, to resolve that user-visible config decision together.

AutoCompact correction retained explicitly: `prepare_session()` only suppresses duplicate summary injection for a checkpointed session; `_archive()` still invokes idle compaction. No idle-compaction bypass was found. F1's bound symbols now have production callers; `retain_recent_legal_suffix()` remains the explicit heartbeat policy's caller, not a dead cap helper.

### Changes and proof

Deleted production symbols: `AgentRunner._microcompact`, `_apply_tool_result_budget`, `_snip_history`; `_SNIP_SAFETY_BUFFER`, `_MICROCOMPACT_KEEP_RECENT`, `_MICROCOMPACT_KEEP_RECENT_IMAGES`, `_MICROCOMPACT_MIN_CHARS`, `_COMPACTABLE_TOOLS`; and `Consolidator.pick_consolidation_boundary`, `_prompt_diagnostics`. Their orphaned imports were removed. `nanobot/agent/runner.py:1941` now proceeds directly from live image-observation tracking to tool batching; `nanobot/agent/memory.py:1393` retains the still-called history estimator rather than the obsolete boundary picker. `nanobot/providers/base.py:815` now explains role repair for imported/assistant-first histories without referring to deleted truncation.

Deleted exactly **10 obsolete cases**: four `test_snip_history_*` cases and three `test_microcompact_*` cases in `test_runner_governance.py`, plus all three cases in `test_runner_image_microcompact.py` (the file and its `_image` fixture were removed). Also removed unused test-only `_make_loop`. Kept/renamed the imported-orphan test because it exercises the live `_drop_orphan_tool_results` path. No live image-budget tests were removed.

The two governance-failure tests had patched an unreachable method and were vacuously green. They now inject one failure into the live orphan-repair path and assert that the fallback actually calls it again: `tests/agent/test_runner_governance.py:15` and `tests/agent/test_runner_image_budget.py:53`. Live budget preflight, tool normalization, role repair, retained-call/result validity, and nine-image handoff remain covered.

Targeted run: **101 passed** (`test_runner_governance.py`, `test_runner_image_budget.py`, `test_runner_umans_image_limit.py`, `test_runner_tool_execution.py`, `test_provider_retry.py`, and `tests/session/test_compaction*.py`). Lint and whitespace checks passed. Repository search returns no remaining definitions/references for the deleted production symbols. Expected collection delta from F2: **−10 cases**, not skips/xfails. Full/focused verification is recorded after F3 below.

## F3 — completed (bounded F2/F3 work turn)

Moving overflow classification to a dependency-free provider utility; all actual callers will import it there. Inspection found **no provider caller of `RequestBudget`**: its validation is used by the runner/session coordinators, not provider execution. The review's additional budget-exception inversion claim therefore does not justify moving that session-owned validation merely to manufacture a fix.

### Changes and proof

- `nanobot/providers/errors.py:6` owns `is_context_overflow()` with no session or runner imports.
- `nanobot/providers/base.py:19,1141` imports the provider utility normally at module scope and calls it before transport retries/image stripping. The lazy session import is deleted.
- `nanobot/agent/runner.py:22,524` imports the same provider utility. All classifier callers/tests were redirected, so no unused session re-export/import is retained; the compaction engine itself never called the classifier.
- `tests/providers/test_error_layering.py:11` verifies every provider module's import statements (including nested imports) and imports `nanobot.providers.base` in a bounded standalone subprocess, asserting no session modules are loaded. Existing explicit-overflow/transport-error and single-call retry cases still pass.
- Repository search confirms `from nanobot.session`, `RequestBudget`, and `CompactionError` are absent throughout `nanobot/providers/`. No budget-validation relocation was warranted: that code remains in the session/runner orchestration layer where it is consumed.

Targeted F3 run: **76 passed**. The new architecture check adds **one** case. Together F2/F3 have a net **−9** cases: ten tests for unreachable behavior deleted, one live layering regression added. No skips or xfails were introduced.

## Verification after F2/F3

Focused suite (same F1 command plus `tests/providers/test_error_layering.py`):

```bash
scripts/run-tests.sh tests/session tests/agent/test_runner*.py tests/agent/test_loop_runner_integration.py tests/agent/test_loop_consolidation_tokens.py tests/agent/test_consolidator.py tests/agent/test_consolidation_ratio.py tests/agent/test_auto_compact.py tests/agent/test_autocompact_unit.py tests/command/test_compact_command.py tests/agent/test_vision_handoff.py tests/providers/test_error_layering.py -q --disable-warnings --tb=short
```

**392 passed / 0 failed / 0 skipped** (11.49 s).

Full suite: the same memory-capped, importlib, temporary `pytest-timeout` overlay command recorded under F1, with the same 10-second guard: **3473 passed / 3 failed / 2 collection skips** (88.02 s). Delta versus untouched **3448 / 4 / 2** baseline: **+25 passed / −1 failed / unchanged skips; no new failing node IDs**. The failures are the same three baseline email polling timeouts. Collection size is **3476** (3473 + 3), down nine from F1's 3485 exactly as accounted for above. Logs: `/tmp/pi-compaction-review-f2-f3-focused.txt`, `/tmp/pi-compaction-review-f2-f3-full.txt`.

Full changed-Python lint, including new provider/compaction fixtures, and `git diff --check` pass. Pi attribution and third-party notices are untouched. No commit/merge/push. Next bounded pass starts with **F4**; **F4–F9 remain pending**.

## F4 — implementation and targeted verification

`Session.discard_invalid_checkpoint()` validates checkpoint shape/version/references against the canonical transcript. `SessionManager.get_or_create()` applies this to loaded and cached sessions under the canonical-file lock, logs an ERROR for invalid continuity, clears checkpoint/stale-summary metadata and legacy replay cursor, then fsyncs the repaired state before returning raw history. A failed repair write propagates and invalidates the cache rather than pretending the disk was fixed. Valid checkpoints are not rewritten. Six cases in `tests/session/test_compaction_recovery.py` cover missing start/end IDs, old versions, malformed collections/entries, durable removal with ERROR logging, and a byte-for-byte untouched valid load. F4 targeted verification: **46 passed**; lint and diff checks clean. Code anchors at this pass: `nanobot/session/manager.py:277` (recovery) and `:695` (durable manager boundary).

## F5 — completed

`nanobot/session/compaction.py:35,50,229` documents ownership/lifetime and introduces `require_entry_ids()`. Projection and preparation no longer mint IDs on copies/slices; missing identities yield a specific index-bearing error without mutating source state. The owning runner assigns newly appended batch identities before checkpoint projection/finalization. Canonical legacy ingestion retains its initial position-based bootstrap to distinguish identical messages, then persists identities unchanged; switching to bare content hashes would collapse legitimate repeated requests. This selects the review's explicit-identity invariant alternative, not an unsafe content-hash shortcut.

Four tests in `tests/session/test_compaction_identity.py` prove stable IDs across slicing/reprojection, a missing checkpoint-referenced identity's precise failure, preparation refusing to mint IDs, and distinct identical legacy entries. Combined F4/F5/session/consolidator/hook run: **125 passed**; lint clean. F4's six recovery cases remain green.

## F6 — completed

Unifying projection → orphan removal → missing-result backfill → vision handoff → image budget in one governed-payload builder. Both normal/forced-finalization requests and compaction before/after measurements will use it. The explicit progress checks remain: consistent composition does not prove that an arbitrary summary actually shrinks a request.

`AgentRunner._govern_payload()` is now the only composition definition; normal/retry seams, no-tools finalization, and both compaction measurements call it. The transport-only `_request_model()` accepts that governed payload rather than composing another transform pipeline. Repair failures get one retry through the same composition, never an ungoverned fallback. Synthetic missing-tool results carry provenance referencing the existing durable assistant call, not invented entry identities; checkpoint end references still point to durable entries.

Four cases in `tests/session/test_compaction_payload.py` pin golden provider messages and the exact stage order for normal/no-tools paths, verify unchanged originals, prove the actual-payload no-progress guard fires before commit, and prove backfill can be summarized without referencing a synthetic entry. Focused suite: **406 passed** (11.81 s). Full memory-capped suite: **3487 passed / 3 baseline email timeouts / 2 collection skips** (87.30 s), versus 3448/4/2: **+39 passed, −1 failed, skips unchanged**. F4–F6 added fourteen cases; no new failure IDs. Lint clean. Logs: `/tmp/pi-compaction-review-f4-f6-focused.txt`, `/tmp/pi-compaction-review-f4-f6-full.txt`.

## F7 — applied; tail closed by Cody

Pi applied the F7 change (entry identities opt-in via `include_entry_ids`, `strip_private_fields`
scrubbed recursively at the provider boundary, at `get_history`, and in the estimate/fingerprint
signatures so private keys cannot invalidate a valid usage calibration) and then hit its ChatGPT
usage quota mid-turn. Pi's own record stops here.

The pass left one stale test red:
`tests/agent/test_auto_compact.py::TestAgentLoopTTLParam::test_process_message_reads_history_with_token_budget`
still asserted that `_process_message` passes a positive integer `max_tokens` to `get_history`.
That expectation belongs to the retired slicing path, so it was reconciled under F8 below.

Verified by Cody: `providers/` contains no `from nanobot.session` import, no `RequestBudget` and
no `CompactionError` reference; `strip_private_fields` is recursive and dependency-free in
`utils/helpers.py`; the F4 self-heal persists its repair (`get_or_create` →
`discard_invalid_checkpoint()` → save with fsync, dropping the cache entry if that save fails).

## F8 — completed (Cody)

`max_messages` is retired rather than wired into the projection: the replay is governed by the
runner's frozen request budget and by token-budget checkpoints, and a second count cap on top of
that would be a knob with no clear owner. Pi had already removed `replay_max_messages`,
`loop._max_messages` and the orphaned `_replay_overflow_boundary`, and had updated the
`get_history` docstring and the `max_messages` schema description to "Legacy compatibility field;
ignored by model replay" (the config model allows extra keys, so existing configs keep loading).

Cody closed the tail:
- `tests/agent/test_auto_compact.py`: renamed to `test_process_message_reads_projected_history`
  and re-pinned to the actual contract — `projected is True`, `include_timestamps is True`,
  `include_entry_ids is True`, and no `max_tokens`/`max_messages` in the kwargs.

## F9 — completed (Cody), audit-only contract pinned

The trim's sink is `SessionManager._archive_session_prefix` and nothing else. Verified against
`main`: both file-cap call sites there passed
`on_archive=partial(memory.raw_archive, session_key=key)`, and `raw_archive` appends to
`memory/history.jsonl` — the log `memory_search` reads and Dream consolidates from. So trimmed
prefixes did reach searchable memory before, and deliberately do not now.

Kept audit-only, because a trimmed prefix is already hidden behind a checkpoint and carried by
its summary; re-feeding that text into consolidation would duplicate material, and the local
archive is lossless (fsynced, content-addressed, idempotent) where `raw_archive` was truncated to
`_RAW_ARCHIVE_MAX_CHARS`.

- `nanobot/session/manager.py`: `_archive_session_prefix` docstring now states the contract and
  that `sessions/archive/` has no automatic reader and no retention policy — it is an
  operator-visible audit store that grows with trimming, not a live data source.
- `tests/session/test_compaction_retention.py`: new
  `test_trim_sink_is_the_audit_archive_only` — with `FILE_MAX_MESSAGES` monkeypatched to 4, a
  capped save trims, writes exactly one audit batch, and does **not** call
  `MemoryStore.raw_archive`. Re-wiring the trim back through topic memory fails this test.

Open question for Gergő, not a blocker: if trimmed material should stay visible to
`memory_search`, the fix is to wire both sinks (audit + `raw_archive`); the current choice is the
opposite and is now documented and pinned.

## Verification (Cody, after F7–F9)

- Focused: `tests/session tests/agent/test_auto_compact.py tests/agent/test_loop_runner_integration.py`
  → **160 passed** (4.77 s).
- Full memory-capped suite: **3492 passed / 3 failed / 2 skipped** (86.72 s). All three failures
  are `tests/channels/test_email_channel.py` timeout cases, reproduced identically on `main`
  (`f415978fd`) in a throwaway worktree: **3 failed, 46 passed**. Pre-existing and unrelated.
- Nothing committed, merged or pushed. The diff is uncommitted on `feat/pi-style-compaction`.

Harness note: a plain `scripts/run-tests.sh <paths>` run without `--import-mode=importlib` fails
with a pydantic `ToolsConfig is not fully defined` error that reads as four real failures
including two of the retention tests. Use the documented invocation
(`PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$TIMEOUT_SITE" scripts/run-tests.sh <paths> --import-mode=importlib
--timeout=10 --timeout-method=signal`).







