# Durable tool history and idle-only microcompaction

## Tasks

- [x] Confirm user policy: retain tool-call arguments and bounded text tool results on disk; idle work only hides older outputs; full summaries use stored evidence.
- [x] Confirm delegation settings: Herdr Pi workers, openai-codex / gpt-6-luna / high; user authorizes a local WIP checkpoint, no push/deployment.
- [ ] Checkpoint unfinished implementation so isolated worktrees inherit it.
- [ ] Persistence worker: authoritative RAM/JSONL tool history, recovery/replay parity, safe image references and reasoning removal.
- [ ] Compaction worker: idle projection and scheduling, pressure estimates, original-evidence summary input, memory/archive boundary and docs.
- [ ] Independent read-only review: examine invariants, integration hazards and command routing; report actionable issues.
- [ ] Review/integrate worker commits, validate real second-turn replay, restart parity, idle projection, manual/pressure summaries and rollback.
- [ ] Run relevant existing memory-capped tests and lint; record remaining limitations and report results.

## Contract

One authoritative transcript, cached in RAM and durably stored in session JSONL. Saving must not replace text tool results with placeholders or erase call arguments. Existing tool-output size/offload limits remain. Hidden reasoning and embedded image bytes must not leak into persistence; safe local image references remain recoverable. Historical results already stripped by old releases cannot be reconstructed.

Chat projection is disposable. Idle microcompaction marks a durable boundary, hides only older eligible text outputs in normal model requests, and never writes placeholders back over originals. Keep the newest ten eligible results within the idle boundary, hide older strings >=500 chars, do not affect new results or the vision policy. Idle work must never invoke an LLM, personal-memory flush, searchable-history generation or Dream.

Full /compact and pressure recovery summarize the previous checkpoint plus uncovered original stored evidence, not idle placeholders. Summarizer input/calls remain bounded; actual chat-view pressure/progress govern admission and commit. Preserve checkpoint atomicity, optimistic concurrency, legal tool pairing and lossless checkpoint-hidden archival/file cap. Memory/history.jsonl is a separate bounded searchable archive, not the authoritative transcript.

## Ownership

- Persistence: `nanobot/session/manager.py`; relevant existing session persistence/recovery/privacy tests and `tests/agent/test_session_manager_history.py`. Keep the `sanitize_message_for_persistence` API but change its session semantics to retain text/arguments; coordinate archive callers with the compaction worker via parent. Do not edit memory.py, runner.py, loop.py or shared TODO files.
- Compaction: `nanobot/session/microcompaction.py`, `nanobot/agent/{autocompact,memory,runner,loop}.py`, `nanobot/config/schema.py`, `docs/configuration.md`; existing idle/runner/command/compaction-payload tests. Do not edit manager.py or persistence-owned tests/TODO files. Assume persistence sanitizer will preserve bounded text and arguments while retaining reasoning/image safety. Explicitly keep searchable-history pruning distinct where necessary.
- Review: read-only, no edits to shared checkout. Review the committed WIP and design; parent will request final integration review separately if needed.

## Log

### 2026-10-16 — setup
User approved the design and tool-call/result disk retention. Existing WIP on `feat/idle-compaction-backoff` adds a microcompaction boundary and chat-only projection, but the old session sanitizer still strips every tool result and call arguments. Last focused run: 228 passed / 1 failed (`test_auto_compact_hides_old_results_without_archiving`); failure exposes this real persistence mismatch. Earlier stale fake-compactor tests were partly migrated. Ruff passed for touched files. This WIP is not deployable and has not been pushed or restarted.

Parent Herdr pane: `w1E:p1`, workspace: `w1E`. CLI is at `$HERDR_BIN_PATH` (not on bash PATH). Shared checkout: `/home/gergo/nanobot`. Required input is tracked source/tests/docs; no private data needed. Each editor must work in its own Herdr-managed worktree, use uv, use scripts/run-tests.sh for tests, and commit its own bounded changes. Do not create new tests/test scaffolding by default; migrate affected existing tests and use temporary offline synthetic runtime checks. Do not read private sessions, call live providers, push, deploy, restart, or remove workspaces.
