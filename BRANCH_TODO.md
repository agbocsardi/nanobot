# Branch coordination: feat/harness-contract-fixes

**Branch-only working notes — do NOT merge this file into `main`.**
It exists purely to coordinate the remaining work on this branch and should be
deleted (or kept out of the merge) when the branch lands.

Tracks the fixes from the 2026-09-15 harness audit. Order below = merge-readiness
order, not priority.

## Done — committed

- [x] `54b924f3` **Delivery acknowledgement contract** (audit #5, part of #7, #10-outbound)
  bus `DeliveryResult` + tracked publish/futures; manager dispatch acknowledges
  (unknown channel = failed, intentional skips = suppressed, ambiguous never retried);
  cron in-band/isolated record real acks; `[SILENT]` suppressed on isolated path;
  CLI mirrors channel deliveries into history only after delivery; message tool
  reports failed/unconfirmed. Tests: `tests/harness/test_delivery_contract.py` (green).
- [x] `0f5e758b` **Cron scheduler/validation/retention** (audit #1, #2, #3, #6)
  execution tasks independent of timer (mutations can't cancel runs; due jobs start
  on free slots); stop keeps claim lease (no instant replay); failed one-shots
  retained + force-runnable once; full schedule validation at add + CronTool
  reports validation errors. Tests: `tests/cron/` 155 green.

## Done — verified, NOT yet committed (commit next)

- [x] **Cron deferred-turn invalidation + in-band delivery ack** (audit #4, #5)
  `cron_turns.py`: cancelled/timed-out runs invalidate queued deferred copies
  (`Task.cancel()` cancels the awaited future — never gate on `future.done()`);
  run-loop drops stale cancelled cron copies at consumption; `_dispatch` uses
  tracked publish; suppressed replies resolve a `suppressed` DeliveryResult.
  Tests: `test_runner_injections.py`, `test_loop_cron_timezone.py` (45 green).
- [x] **Subagent ownership + task-control sender scoping** (conditional findings)
  `owned_record`: missing/mismatched sender rejected when a sender is supplied;
  no-sender callers (cron/dream/internal) are trusted; `cancel_by_session`
  registry-first for internal callers; `/task stop` now propagates sender.
  Tests: `test_task_cancel.py`, `test_subagent*.py`, `test_subagent_tools.py`
  (92 green).
- [x] **Subagent provenance** — `_persist_subagent_followup` no longer stores
  delegated announcements as assistant-authored text.

## Implemented — verification in flight (combined suite run)

- [ ] **Tool outcomes + receipts** (audit #8, #9): exec_session structured
  running/nonzero/timeout outcomes; `effect_for(params)` receipt enrollment for
  exec/cron/edit_file/memory_write/intent/goal/spawn; partial persisted as
  unknown (no redispatch-as-success); runner carries /policy approval tokens.
  Files: `exec_session.py`, `shell.py`, `registry.py`, `action_receipts.py`,
  `runner.py`, effect attrs on fs/memory/goal/spawn tools.
  Known test wart: `test_registry_persists_running_session_as_unknown...`
  races interpreter boot (marker file) — fix wait-loop or drop marker assert
  (same coverage green in `test_action_receipts.py`).
- [ ] **SDK instance isolation** (conditional): per-call hook fanout via
  ContextVar (no shared `_extra_hooks` mutation); `config_path_context` +
  `ssrf_whitelist_context` scoped per instance. Files: `nanobot.py`,
  `config/loader.py`, `security/network.py`.
- [ ] **Image-download SSRF guard** (audit #12): `validate_url_target` on
  provider URLs + every redirect hop. `providers/image_generation.py`.
- [ ] **WebSocket chat ownership** (conditional): connection-scoped
  `_owns_chat`, uniform unknown-chat errors, no reconnect reclaim; truthful
  DeliveryResult (failed=no subscriber, delivered=frame ack, unknown=partial).
- [ ] **Discord/Email truthful delivery**: fetch failure 4xx→failed,
  network/5xx→unknown; email partial-refusal→unknown, no retry-after-unknown.
- [ ] **Telegram ingress dedup + send acks** (audit #10): dedup before bus
  publish; no fallback resend after ambiguous rich-send timeout.
  NOTE: agent was cancelled before reporting — diff NOT self-audited yet.

## Implemented — NOT verified at all

- [ ] **Memory fallback on failed summarization** (audit #11): bounded
  user/assistant fallback excerpt when a history entry has no summary
  (`_fallback_session_summary`, 2k cap); legacy `MEMORY.md` paragraphs load
  alongside `memory/system/*` (dedup by paragraph, no migration writes).
  Files: `agent/memory.py`. Run `tests/agent/test_memory_store.py` before
  committing.

## Remaining work

- [ ] Collect combined-suite result; fix failures per area above.
- [ ] Audit the un-reviewed Telegram diff (ingest ordering ~L2021, send
  fallback ~L939).
- [ ] Commit groups (in this order):
  1. cron coordinator + loop delivery ack + task control/ownership
  2. tool outcomes + receipts + approval tokens
  3. memory fallback + legacy coexistence
  4. SDK isolation + SSRF guard
  5. channels: discord/email/websocket/telegram
- [ ] Final integration pass: schedule → execute → cancel/restart → deliver
  scenario + full `uv run --extra dev python -m pytest tests/ -q` +
  `uv run ruff check nanobot/` on touched files.
- [ ] Delete this file before merging to main.
