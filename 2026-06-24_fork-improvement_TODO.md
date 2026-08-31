# Nanobot Fork Improvement TODO

Status after 2026-06-24 work: cron noise suppression, background observability,
designated background model presets, and structured/summarized history archives
are shipped on `origin/main`.

## Completed

- [x] Silent cron success suppression
  - `payload.silent=true` suppresses successful chat delivery only
  - exact `[SILENT]` response marker also suppresses cron success output
  - errors still notify
- [x] Background run observability
  - shared run-record writer in `nanobot/utils/run_records.py`
  - cron records under `runs/` include kind, provider/model, usage, silent flag, status/result
  - subagent records under `subagents/` include kind, task, params, provider/model, usage, iterations, tool events, status/result
- [x] Designated background model presets
  - `agents.defaults.runPresets` maps `subagent`, `cron`, `dream`, `consolidator` to existing model presets
  - resolver order: explicit override → runPresets[kind] → fallback kind where configured → active modelPreset → default
  - cron/subagent/consolidator avoid ambient expensive chat model
- [x] Conversation archive summaries
  - consolidator restores upstream-style LLM summaries
  - idle compact summarizes the full unconsolidated tail but archives only dropped messages
  - one `history.jsonl` record per archived conversation/chunk; no extra summary cursor lines
- [x] Contentless v3 conversation history
  - new conversation records write `schema_version=3`, structured `messages`, optional `summary`
  - no new top-level `content` for conversation records
  - old v1/v2 `content` records still read via `history_entry_text()` fallback

## Next focus: Letta-style memory architecture

Goal: turn memory into explicit, inspectable, editable artifacts instead of relying on
opaque prompt stuffing.

### Phase A — Memory read/write tools

- [ ] Add `memory_read` tool
  - list/search topic memory files under `memory/`
  - show summaries/descriptions before full file bodies
  - support exact text search over topic files and `history.jsonl` summaries/messages
- [ ] Add `memory_write` tool
  - create/update topic memory files with frontmatter
  - require explicit path/title/description
  - write atomically and avoid touching `history.jsonl`
- [ ] Add safety guardrails
  - no direct write access to `.dream_cursor`, `history.jsonl`, session files
  - path restricted to memory directory
  - clear tool docs: topic memory is curated; history is append-only evidence

### Phase B — Topic memory layout

- [ ] Decide canonical topic-file schema
  - frontmatter: `title`, `description`, `updated`, maybe `tags`
  - body: concise durable facts, decisions, preferences, project state
- [ ] Add/read memory index from frontmatter descriptions
  - existing memory tree should become the primary discovery surface
  - system memory remains always-loaded; topic memory is opt-in via tools
- [ ] Add tests for memory file discovery, reads, writes, and path rejection

### Phase C — History as evidence/index

- [ ] Keep `history.jsonl` append-only and tool-read-only
- [ ] Make `memory_read` search `summary` first, then `messages`, then old `content`
- [ ] Consider a small `history_search` helper behind `memory_read`
  - return cursor, timestamp, session_key, summary/snippet
  - no semantic search yet; literal search first

### Phase D — Dream / consolidation follow-up

- [ ] Teach Dream to prefer v3 `summary` but inspect `messages` when needed
- [ ] Consider Dream promoting durable `[permanent]` / `[durable]` summary facts into topic files
- [ ] Keep `.dream_cursor` semantics unchanged: only Dream advances it

## Deferred / maybe

- [ ] Run-record preset-name attribution (`run_preset`) if debugging needs it
- [ ] Backfill recent old history entries into v3 shape if worth it
- [ ] Usage rollup command — skipped until explicitly wanted
- [ ] Semantic/vector memory search — later, literal search first

## Log

### 2026-06-24

- Implemented silent cron jobs and exact `[SILENT]` marker suppression.
- Added shared cron/subagent background run records with provider/model/usage.
- Added designated run presets for cron, subagent, dream, and consolidator.
- Restored LLM consolidation summaries while preserving structured conversation evidence.
- Switched new conversation archives to contentless v3 (`messages` + optional `summary`).
- Pushed all completed work to `origin/main` through `f7940c9d`.

## M2 hybrid trigger design

### 2026-09-02

- Designed the M2 Dream hybrid trigger policy (item from `2026-06-16_decouple_TODO.md`):
  cron + every-N-user-turns + idle timer + manual, compaction as optional bonus.
- Wrote `docs/dream-trigger-design.md`: current trigger audit (gateway cron system job,
  `/dream` manual, both funneling into the M1b-M1d guarded `MemoryStore.run_dream`),
  proposed `dream.triggers.*` config (defaults preserve pure cron behavior, others off),
  per-loop counter/idle-tick trigger state, single-flight + cooldown dedup rules, and edge
  cases (restarts reset counters, deferral around active user turns, bounded budget via
  no-turn-in-flight gate, M1b-M1d rollback interplay incl. a CancelledError hardening gap).
- Implementation plan covers schema, new `DreamTriggerCoordinator`, loop/cron/builtin hooks,
  and a test list; open decisions flagged for Gergő.
- Docs only — no runtime code changes, nothing committed.
