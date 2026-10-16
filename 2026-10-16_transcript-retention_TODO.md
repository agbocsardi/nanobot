# Durable tool history and idle-only microcompaction

## Tasks

- [x] Confirm user policy: retain tool-call arguments and bounded text tool results on disk; idle work only hides older outputs; full summaries use stored evidence.
- [x] Confirm delegation settings: Herdr Pi workers, openai-codex / gpt-6-luna / high; user authorizes a local WIP checkpoint, no push/deployment.
- [x] Checkpoint unfinished implementation so isolated worktrees inherit it (`860da7db8`).
- [x] Persistence worker: authoritative RAM/JSONL tool history, recovery/replay parity, safe image references and reasoning removal (`c261ee3cb` → `1c5ab5ee3`; hardening `6f3706015` → `ab07f7e26`; independently reviewed).
- [x] Compaction worker: idle projection and scheduling, pressure estimates, original-evidence summary input, memory/archive boundary and docs (`81a4f2166` → `0d68ffbbe`, `21cfda35e` → `d5e9f09e6`, `7b9c4fd5f` → `e43e7ce42`).
- [x] Independent read-only WIP review: examine invariants, integration hazards and command routing; report actionable issues (`/tmp/nb-review-860da7-report.md`).
- [x] Make checkpoint-range searchable archival idempotent across failed-save/retry, including raw fallback and malformed-record scan (`64f586330` → `b19ce0ae9`).
- [x] Review/integrate worker commits, validate real second-turn replay, restart parity, idle projection, manual/pressure summaries and rollback.
- [x] Run relevant existing memory-capped tests and lint; record remaining limitations and report results.

## Contract

One authoritative transcript, cached in RAM and durably stored in session JSONL. Saving must not replace text tool results with placeholders or erase call arguments. Existing tool-output size/offload limits remain. Continue removing `reasoning_content` and embedded structured image payloads; safe local image references remain recoverable. Preserve established provider-signed `thinking_blocks` required by provider replay. Do not destructively truncate code-bearing tool arguments or redact arbitrary source-text data URIs: user accepts tool-data retention; generic binary externalization is not part of this change. Historical results already stripped by old releases cannot be reconstructed.

Chat projection is disposable. Idle microcompaction marks a durable boundary, hides only older eligible text outputs in normal model requests, and never writes placeholders back over originals. Keep the newest ten eligible results within the idle boundary, hide older strings >=500 chars, do not affect new results or the vision policy. Idle work must never invoke an LLM, personal-memory flush, searchable-history generation or Dream.

Full /compact and pressure recovery summarize the previous checkpoint plus uncovered original stored evidence, not idle placeholders. Summarizer input/calls remain bounded; actual chat-view pressure/progress govern admission and commit. Preserve checkpoint atomicity, optimistic concurrency, legal tool pairing and lossless checkpoint-hidden archival/file cap. Memory/history.jsonl is a separate bounded searchable archive, not the authoritative transcript.

## Ownership

- Persistence: `nanobot/session/manager.py`; relevant existing session persistence/recovery/privacy tests and `tests/agent/test_session_manager_history.py`. Keep the `sanitize_message_for_persistence` API but change its session semantics to retain text/arguments; coordinate archive callers with the compaction worker via parent. Do not edit memory.py, runner.py, loop.py or shared TODO files.
- Compaction: `nanobot/session/microcompaction.py`, `nanobot/agent/{autocompact,memory,runner,loop}.py`, `nanobot/config/schema.py`, `docs/configuration.md`; existing idle/runner/command/compaction-payload tests. Do not edit manager.py or persistence-owned tests/TODO files. Assume persistence sanitizer will preserve bounded text and arguments while retaining reasoning/image safety. Explicitly keep searchable-history pruning distinct where necessary.
- Review: read-only, no edits to shared checkout. Review the committed WIP and design; parent will request final integration review separately if needed.

| Agent | Pane / workspace | Branch / role | Report |
| --- | --- | --- | --- |
| `nb-retention-860da7` | `w1G:p1` / `w1G` | `feat/tool-history-retention` — persistence | `/tmp/nb-retention-860da7-report.md` |
| `nb-microcompact-860da7` | `w1H:p1` / `w1H` | `feat/idle-microcompact-integration` — compaction | `/tmp/nb-microcompact-860da7-report.md` |
| `nb-review-860da7` | `w1E:p2` / `w1E` | read-only review in parent checkout | `/tmp/nb-review-860da7-report.md` |

All three launched as Pi with `--provider openai-codex --model gpt-6-luna --thinking high`, no focus changes. Independent assignments submitted; parent owns integration. Initial and final integration reviews completed; implementation/hardening reports integrated, final fallback-idempotency blocker fixed and checked.

## Final verification and limitations

- Integrated production HEAD: `b19ce0ae9` on `feat/idle-compaction-backoff`; no push, merge to main, deployment or restart.
- Focused memory-capped suite: **510 passed**, twice including final fallback fix. Log: `/tmp/nanobot-retention-final-focused.log`.
- Final full suite with 10s pytest-timeout overlay: **3,585 passed / 3 failed**, same as untouched `d98528359` export under identical flags (**3,588 collected** each). All three are existing email polling timeouts: `test_start_applies_post_action_only_after_delivery`, `test_start_skips_post_action_when_delivery_fails`, `test_start_keeps_post_actions_for_successful_emails_when_later_delivery_fails`. No new failing node IDs. Logs: `/tmp/nanobot-retention-final-full.log`, `/tmp/nanobot-retention-baseline-full.log`.
- Ruff on all changed Python files and whitespace checks passed.
- Parent offline runtime validation exercised real SessionManager/Consolidator/AgentRunner/command-router paths: idle generated zero provider/flush/history operations; two old tool results were hidden in actual chat requests but remained in result.messages and reloaded JSONL; overflow-triggered engine and actual `/compact` dispatch both summarized original evidence, not idle placeholders; original transcript preserved.
- Default idle threshold remains **0 (disabled)**; configure `idleCompactAfterMinutes` to 15/30 to enable. Existing output limits/offloading remain; complete call arguments are retained. Old already-stripped history is unrecoverable. Provider-signed thinking blocks and inline data URIs in actual source text/args remain as documented above. Failure backoff is process-local; idle boundaries/checkpoints persist.
- Herdr child worktrees/tabs remain open pending user-approved cleanup. No private session reads or live-provider verification was performed.

## Log

### 2026-10-16 — setup
User approved the design and tool-call/result disk retention. Existing WIP on `feat/idle-compaction-backoff` adds a microcompaction boundary and chat-only projection, but the old session sanitizer still strips every tool result and call arguments. Last focused run: 228 passed / 1 failed (`test_auto_compact_hides_old_results_without_archiving`); failure exposes this real persistence mismatch. Earlier stale fake-compactor tests were partly migrated. Ruff passed for touched files. This WIP is not deployable and has not been pushed or restarted.

Persistence report received from `nb-retention-860da7` / `w1G:p1`; reviewed code/test diff and cherry-picked `c261ee3cb` as `1c5ab5ee3`. Worker reports 70 focused + 15 recovery/checkpoint tests, Ruff and offline end-to-end checks passing. Sent commit and integration notes to compaction worker; assigned follow-up read-only persistence review. Compaction and review then hit Codex quota; after the user's reset, inspected agent state/recent output and resumed those two tasks without discarding work. Persistence follow-up review reported 60 existing checks passing and raised argument bounds, inline encoded text, signed thinking blocks and pending-checkpoint coverage. Parent triage: preserve complete arguments and provider-signed thinking blocks (Anthropic consumes these); no arbitrary destructive caps or regex deletion of source text. Assigned persistence worker narrow text-block extra-metadata hardening and actual pending-call restart verification, with residual limitations to document. Compaction worker informed; broad new externalization machinery is out of scope. Persistence hardening report received: narrow allowlisted text-block metadata filtering and fresh-manager pending-call restart/recovery verified. Requested removal of an unrequested new standalone test before integrating its follow-up commit; production change reviewed. Inline encoded data in actual source text/args remains intact deliberately, not silently redacted.

Initial read-only review received from `nb-review-860da7` / `w1E:p2`: end-to-end persistence coverage confirmed as essential; archive-before-checkpoint save can duplicate searchable records on retry; stale idle tests need migration; synthetic repairs need provenance/non-persistence verification (not blind removal, since existing bounded projection tests permit them). Findings routed to owning workers; archive retry idempotency added to compaction worker scope. No implementation has been integrated yet.

Parent Herdr pane: `w1E:p1`, workspace: `w1E`. CLI is at `$HERDR_BIN_PATH` (not on bash PATH). Shared checkout: `/home/gergo/nanobot`. Required input is tracked source/tests/docs; no private data needed. Each editor must work in its own Herdr-managed worktree, use uv, use scripts/run-tests.sh for tests, and commit its own bounded changes. Do not create new tests/test scaffolding by default; migrate affected existing tests and use temporary offline synthetic runtime checks. Do not read private sessions, call live providers, push, deploy, restart, or remove workspaces.
