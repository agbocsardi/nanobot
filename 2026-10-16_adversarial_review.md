# Adversarial review — Pi-style compaction branch

Reviewer: Cody. Scope: the whole uncommitted change on `feat/pi-style-compaction` vs `main`
(27 files, +797/−1445, 4 new files), reviewed against the code, not the report's claims.

Overall: the design is sound and the id/checkpoint invariants hold today. The findings below are
about (a) a bound that was silently lost, (b) code that is now unreachable but still tested,
(c) layering, and (d) error-path robustness. None of them is a reason to reject the design.

Fix all of F1–F7. Where a finding is wrong or already handled, say so with evidence instead of
inventing a fix.

---

## F1 — HIGH: the durable transcript is now unbounded

Evidence:
- `main` called `session.enforce_file_cap(...)` at `nanobot/agent/loop.py:2054` and `:2410`.
  This branch removed both call sites.
- `grep -rn enforce_file_cap nanobot/` now returns only the definition
  (`nanobot/session/manager.py:562`). `Session.retain_recent_legal_suffix` and
  `FILE_MAX_MESSAGES` are likewise unreferenced.
- `AutoCompact` now early-returns for any session that has a checkpoint, so it no longer
  compacts long idle sessions either.

Impact: nothing bounds a long-lived session any more. The main Telegram session is the
pathological case. Per-turn cost scales with the whole transcript: full JSONL parse on load,
an `ensure_ids` walk, a `fingerprint()` JSON dump of the entire transcript (called from
`_commit_compaction`, `compact_session`, and `maybe_consolidate_by_tokens`), and a full-file
rewrite on every `save()`.

Required:
- Re-wire a bound that is safe with respect to the projection: trim only messages strictly
  before the checkpoint's `first_kept_entry_id` — those are already hidden from the model, so
  trimming them cannot change what the model sees. Keep the raw-archive callback so the audit
  trail survives (do not drop the record, archive it). Never drop anything at or after the
  retained entry.
- If you judge a cap undesirable, say so explicitly in the report with measurements instead of
  leaving the dead code in place.

Acceptance:
- No unreachable bound-related helper left: re-wired or deleted.
- New test: a session over the cap keeps its checkpoint's retained entry, and its
  `get_history(projected=True)` output is identical before and after the trim.
- Numbers in the report: session file size, load time, and per-turn `save()` time for a
  ~5,000-message session, before vs after.

## F2 — MEDIUM: dead code that still looks live, plus a stale comment

Unreferenced in production code (verified by grep across `nanobot/`), referenced only by tests:
- `nanobot/agent/runner.py:1951` `_microcompact`
- `nanobot/agent/runner.py:2005` `_apply_tool_result_budget`
- `nanobot/agent/runner.py:2026` `_snip_history`
- `nanobot/agent/runner.py:81` `_SNIP_SAFETY_BUFFER` (used only at `:2038`, inside the dead
  `_snip_history`)
- `nanobot/agent/memory.py:1393` `pick_consolidation_boundary`
- `nanobot/session/manager.py` `enforce_file_cap` / `retain_recent_legal_suffix` /
  `FILE_MAX_MESSAGES` (tie to F1)

Also: `nanobot/providers/base.py:816` justifies a synthetic-message insertion by referencing
`_snip_history`, a path that can no longer run. The comment is now wrong.

Tests `tests/agent/test_runner_governance.py`, `tests/agent/test_runner_image_budget.py`, and
`tests/agent/test_runner_image_microcompact.py` exercise these helpers, so the suite reports
green coverage for behavior that no longer exists in production.

Required: delete the unreachable helpers and only the test cases that cover them (keep live
coverage, e.g. anything still exercising `_prepare_umans_image_budget`); fix the stale comment.
If a helper is deliberately kept, mark it and justify it in the report.

Acceptance: no defined symbol without a production caller; suite green; the report lists every
deleted symbol and explains the test-count delta.

## F3 — MEDIUM: provider layer now depends on the session layer

`nanobot/providers/base.py` lazily does `from nanobot.session.compaction import
is_context_overflow` inside the retry loop, to dodge an import cycle. The provider layer should
not know about sessions, and a lazy import hides the inversion. The same inversion shows up as
`RequestBudget.freeze` raising `CompactionError` (a session-layer exception) as control flow in
the provider path.

Required: move `is_context_overflow` into the provider layer (e.g. `providers/base.py` or a
small `providers/errors.py`) and have `session/compaction.py` import it from there. Keep a
re-export only if a caller needs it.

Acceptance: `grep -rn "from nanobot.session" nanobot/providers/` is empty; importing
`nanobot.providers.base` standalone is clean; full suite green.

## F4 — MEDIUM: an invalid checkpoint bricks the session

`Session.get_history` calls `project()`, which raises `CompactionError` when the checkpoint's
`first_kept_entry_id` is not present in the transcript. The runner also re-raises it
(`except CompactionError: raise` in `compact()`). So every subsequent turn for that session
fails until the user runs `/new`, which drops the history. Reachable via a checkpoint from an
older format, an out-of-band transcript edit/prune, or a partially written session file.

Required: keep it loud, make it self-healing. Validate the checkpoint when the session is
loaded (`SessionManager.get_or_create` / `Session.__post_init__`); if it does not reference a
present entry, log at ERROR, drop it from metadata, persist, and continue with unprojected
history. Never silently keep a summary that no longer matches the transcript.

Acceptance: test that a session whose checkpoint references a missing entry returns history,
has the checkpoint removed from metadata *and* from disk, and logs an ERROR; plus a test that a
valid checkpoint is left untouched.

## F5 — LOW/MEDIUM: fallback entry ids are position-dependent

`ensure_ids` derives `sha256(f"{index}:{payload}")`, so the same message gets a different id
depending on the list it is found in. It is called from `Session.__post_init__`, `save`,
`get_history`, and inside `project()` on whatever list it is handed — including transformed or
sliced lists. This holds today only because every message acquires an id at load/save time. Any
future subset call mints a divergent id for the same message, and the commit check
(`first_kept_entry_id in ids`) or a later `project()` then fails.

Required: make the fallback id content-derived (drop the index), or assert that ids are present
in `project`/`prepare` and fail loudly there rather than minting a divergent one. Document the
invariant ("entry ids are stable for the lifetime of a message").

Acceptance: test asserting a message keeps the same `_entry_id` when the list is sliced or
re-projected; test that a genuinely missing id at a checkpoint-referenced position produces a
clear, specific error.

## F6 — LOW: the transform pipeline is composed twice, differently

`compact()` composes project → drop-orphans → vision-handoff → image-budget, then re-runs
vision-handoff + image-budget on the candidate purely to measure it. The request path composes
drop-orphans → backfill → vision-handoff → image-budget. Two different compositions, which is
exactly why `compact()` needs the "Checkpoint did not reduce the actual request payload" guard.

Required: one helper that builds the governed payload from (messages, checkpoint), used by every
site, with the composition order stated once.

Acceptance: single definition; the "no progress" guard still fires when it should (keep the
existing test); the emitted payload for a given transcript is unchanged by the refactor (prove
it with a test, not by inspection).

## F7 — LOW: private-key hygiene at the history boundary

`get_history` now returns `_entry_id` / `_compaction_summary` whenever a checkpoint exists
(`projected = projected or checkpoint is not None`), so every caller inherits them; only the
runner strips them. `nanobot/webui/session_context.py` returns that replay in the WebUI payload,
so `_entry_id` starts appearing in an API response once a checkpoint exists.

Required: strip private keys at one boundary, or gate ids behind an explicit opt-in
(`include_entry_ids=True`) used only by the runner path. Confirm no provider-bound payload can
carry a `_`-prefixed key.

Acceptance: test that no message handed to a provider contains a key starting with `_`; WebUI
payload either unchanged vs `main` for a checkpointed session or the change is documented.

## F8 — MEDIUM: `max_messages` is now a silently inert config knob

Evidence:
- `get_history(projected=True)` skips both the message-count and token slices, and
  `projected = projected or checkpoint is not None`. The main turn path
  (`loop.py:2011`, `:2376`) passes `max_messages` together with `projected: True`, so the count
  cap no longer applies to the replay at all.
- `maybe_consolidate_by_tokens` accepts `replay_max_messages` (`memory.py:1654`) and both call
  sites pass `self._max_messages` (`loop.py:1999`, `:2361`), but the body never uses it — it
  fetches `session.get_history(projected=True)` (`memory.py:1664`).
- `_replay_overflow_boundary` (`memory.py:1431`) has no caller left; on `main` it was called at
  `memory.py:1475` and the value was threaded through at `:1710`.
- The `get_history` docstring still claims "History is sliced by message count first
  (``max_messages``), then by token budget from the tail (``max_tokens``) when provided", which
  is no longer true for the projected path.

Impact: a documented config knob now does nothing in the main path, and its plumbing
(parameter, helper, two call-site arguments) survives as decoration — the same class of problem
as F2, but user-visible because the config still advertises it.

Required: pick one and finish it.
- Wire it: make `max_messages` cap the *retained* suffix in the projected path (a count cap
  inside `project()`, so the estimate and the request stay consistent), or
- Retire it: delete `_replay_overflow_boundary`, drop the unused `replay_max_messages`
  parameter and the two call-site arguments, and document the config field as legacy/no-op.

Either way, fix the `get_history` docstring so it describes the projected path accurately.

Acceptance: no parameter or helper without a live consumer; the docstring matches behaviour; a
test that pins the chosen semantics (either "the projected replay never exceeds N messages" or
"max_messages has no effect on replay" — whichever you chose).

## F9 — MEDIUM: the trim point no longer feeds the memory raw archive

Found while verifying F1.

Evidence: on `main`, both `enforce_file_cap` call sites passed
`on_archive=partial(self.context.memory.raw_archive, session_key=key)`
(`nanobot/agent/loop.py:2054` and `:2410`), so messages trimmed out of the active session file
reached `MemoryStore.raw_archive()`. The branch now wires the single remaining call site
(`nanobot/session/manager.py:840`) to `_archive_session_prefix`, which writes to
`sessions/archive/<safe-key>/<fingerprint>.jsonl`. Nothing in `nanobot/` reads that directory.

Impact: whatever consumed `raw_archive` no longer sees the trimmed prefix. The trimmed messages
are checkpoint-hidden, so the model-visible projection is unaffected — but if a memory
extraction pass reads `raw_archive`, it silently loses that material, and the new archive
directory is write-only and unpruned.

Required: state the intended contract explicitly, then make the code match it.
- If the trimmed prefix must still reach memory extraction, wire both callbacks (archive for
  audit, `raw_archive` for extraction) — the old behaviour.
- If it must not, say why in the report (e.g. "the checkpoint summary already covers this
  prefix") and note that `sessions/archive/` has no reader and no retention policy.

Acceptance: a test that pins the chosen contract — either "trimming invokes both callbacks" or
"trimming invokes only the audit archive", with the reasoning recorded. Also document in the
report whether `sessions/archive/` needs a retention/cleanup story, given it grows forever and
nothing consumes it.

---

## Constraints for the fix pass

- No commit, no merge, no push. Leave the diff reviewable in the worktree.
- Fix the class of problem, not the instance. No unrelated refactors. Keep the Pi attribution
  and `THIRD_PARTY_NOTICES.md` intact.
- Verify, do not assert: run the focused compaction suite and the full suite, and report the
  pass/fail delta against the untouched baseline (3448 passed / 4 failed / 2 collection skips).
  Any new failure means the fix is not done. Re-run `git diff --check` and the linter.
- Keep each work turn bounded; write progress into the report file as you go rather than holding
  it in context.
- Append your results to `2026-10-16_pi-compaction_review_fixes.md`: per finding — what changed
  (file:line), the test that proves it, and anything deliberately not fixed with the reason.
