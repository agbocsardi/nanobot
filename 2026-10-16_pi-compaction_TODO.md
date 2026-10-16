# Pi-style compaction

- [x] Read worker brief, complete canonical plan, existing persistence/runner paths; verify pinned Pi compaction/projection/controller source and MIT attribution.
- [x] Implement durable checkpoint schema, legacy-compatible stable references, legal token-sized cuts and deterministic projection.
- [x] Implement bounded iterative/split-prefix summaries, cancellation/output validation, optimistic concurrency checks and atomic commits.
- [x] Integrate frozen provider/model/window/output budgets and bounded continuation recovery between completed tool batches; preserve streaming and tool effect identity.
- [x] Route manual, idle and threshold compaction through the same engine; preserve explicit personal-memory flush policy, separate Dream/history extraction, and keep the nine-image handoff.
- [x] Add deterministic acceptance fixtures and migrate incompatible destructive-compaction assertions; run focused checks (394 passed), lint and diff checks.
- [x] Run memory-capped full current/baseline suites with comparable collections; confirm no new failures. Current: 3475 passed, 3 baseline email timeouts, 2 collection skips.
- [x] Document architecture, exact commands, collection changes, baseline failures and limitations in `2026-10-16_pi-compaction_report.md`; leave reviewable uncommitted changes.

## Log

### 2026-10-16
Read authorized brief and canonical plan. Clean isolated branch `feat/pi-style-compaction` started at `f415978fd`. Implemented in dependency order: projection/schema → bounded generation/commit → frozen runner budget/recovery → shared callers → deterministic acceptance fixtures and existing-test migration.

Original transcripts remain in their existing redacted persistence representation. Pending tool batches and checkpoints commit atomically; final saves deduplicate stable identities. Model projection and interrupted continuation rebuild without replaying tool effects or completed answers. Summaries consume image-handoff projections and model-specific usage is calibrated against the actual sent payload/schema, not mutable defaults. Verified 33 new acceptance cases, including repeated overflows in streaming/non-streaming turns, cancellation/failure/races/reload and irreducible inputs.

Focused suite: 394 passed. Full current suite: 3475 passed / 3 failed / 2 collection skips, 3478 collected. Untouched baseline: 3448 passed / 4 failed / 2 collection skips, 3452 collected. No new failing node IDs; all current failures are baseline email polling timeouts under the same 10-second guard. Collections: 3418 common, 60 added, 34 replaced/renamed. Temporary baseline export inside this worktree was removed after comparison. Lint and whitespace checks pass.

No commits, merge, push, deployment, live restart, other agents, real-provider requests or private-session reads. Live verification remains unperformed.

### 2026-10-16 — adversarial review, bounded pass 1
Read `2026-10-16_adversarial_review.md` and its constraints. F1 is fixed and documented with code/test line references in `2026-10-16_pi-compaction_review_fixes.md`: centralized checkpoint-safe active-file cap, durable lossless redacted audit batches, count-pressure checkpoint generation, and committed-prefix watermark deduplication. Synthetic 5,000-message measurements: active file 3,661,803 → 1,465,413 bytes; median load 30.281 → 12.129 ms; save 40.650 → 16.263 ms. Seven new retention tests; focused suite 401 passed. Full suite 3482 passed / 3 baseline email timeouts / 2 collection skips; no new failing node IDs versus 3448/4/2 baseline. Lint and diff checks clean. No commits.

### 2026-10-16 — adversarial review, bounded pass 2
Read the updated review including F8. Completed F2/F3 and appended evidence to `2026-10-16_pi-compaction_review_fixes.md`. Removed dead runner compaction/snipping helpers/constants and memory boundary/diagnostic helpers, deleting ten obsolete tests while re-wiring two vacuous failure tests to live repair. Preserved live normalization, orphan, image-budget and heartbeat-retention coverage. Overflow classification now belongs to `nanobot/providers/errors.py`; standalone imports and a new architecture guard verify providers have no session dependency. The alleged provider caller of `RequestBudget` did not exist, so no unrelated validation move was invented. The AutoCompact correction is explicitly retained in the review report.

Focused: 392 passed. Full: 3473 passed / 3 baseline email timeouts / 2 collection skips; no new failing node IDs versus 3448/4/2 baseline. Net −9 cases from F1 is intentional (ten dead-behavior tests removed, one provider-layer regression added). Lint and diff checks pass; no commits.

Next bounded pass: F4, then F5–F8 in order. Read the review-fixes report before proceeding; the full review is not yet resolved.

### 2026-10-16 — searchable-history writer restored (Cody)
Audited the F9 decision and found the review had missed the consolidation half: the rewrite removed
every writer of `memory/history.jsonl` (three `self.archive(chunk, ...)` call sites and the trim's
`on_archive=raw_archive` hook), so new conversation would no longer reach `memory_search`, Dream,
daily reflection, lab or perch. Added `Consolidator.archive_compacted_range()` and wired it into
both checkpoint commit sites (`compact_session`, `_commit_compaction`); the trim keeps its
audit-only sink. Ranges are disjoint per checkpoint and archived before the cap can drop them.
Three new tests plus one retitled; full suite 3498 passed / 2 skipped. Committed `c68bfc2f1`;
not merged or pushed.
