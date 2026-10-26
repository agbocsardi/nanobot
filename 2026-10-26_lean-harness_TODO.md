# Lean Code Mode harness

- [x] Create `feat/lean-codemode-harness` from clean main and review prior worker decisions.
- [x] Fix artifact EOF semantics and make complete, bounded artifact reads straightforward.
- [x] Separate script transport limits from model-visible output limits; preserve structured values.
- [x] Consolidate validation, authorization and receipts at one tool-execution boundary.
- [x] Simplify overlapping worker budgets without weakening shared spend/resource ceilings.
- [x] Stop ordinary-chat read-only failures from forcing verification/task verdicts; retain mutation and unattended-run safeguards.
- [x] Remove repeated-lookup quota and file-read dedup; trim redundant prompt guidance.
- [x] Update affected documentation/consumers; run existing checks and focused runtime probes; review diff.

## Merge and deployment

- [x] Commit feature branch and merge into main; push origin/main.
- [x] Fast-forward the clean local editable install at `/home/gergo/nanobot`.
- [x] Restart the personal gateway and verify startup plus safe installed-runtime probes.
- [x] Record deployment result without changing live config or other gateway services.

## Log

### 2026-10-26

User approved implementation after a read-only audit of source, local config, session JSONL,
gateway journal, upstream `3230013963a81189b0f7a2951994f7638a174232`, and installed Pi 1.4.0.
Preserve Python Code Mode, read-only workers, sandbox isolation, scoped artifacts, policy,
durable side-effect receipts, truthful event evidence, cancellation and background completion
safeguards. No live config/service/deployment changes or upstream merge. Use existing tests
and temporary runtime probes; do not add a new test suite. Main starts at `a5cbd5595`.
Confirmed EOF trap: artifact `partial` stays true after a nonzero-offset read reaches EOF;
`next_offset` remains unchanged on empty reads. Script transport also unnecessarily uses
the 16 KB model-context offload threshold.

Implemented:
- Artifact reads terminate with `next_offset=None`/`partial=false` at EOF. Code Mode
  `artifact_read(id)` can read a fitting artifact in one call; explicit chunks accept
  up to 128,000 characters. Oversize complete reads refuse rather than silently truncate.
- Script bridge/worker results retain structured JSON up to 1 MiB; ordinary model
  context stays at 16 KB. Original data, scope checks and resource/frame caps remain.
- All direct and nested tools dispatch through ToolRegistry.execute for validation,
  policy and receipts. Removed duplicate preparation/policy code and one duplicate
  child tool charge. Raw list/dict successes retain their original shape.
- `maxModelCalls` also bounds the navigating worker loop; `maxIterations` is a retired
  config input alias (canonical value wins). Existing default remains 8; the user's
  on-disk setting of 128 will no longer be silently capped by an independent default
  of 8 after deployment/restart. Shared conservative UTF-8-byte/output reservations,
  per-worker/tool counts, timeouts and concurrency limits remain unchanged.
- Ordinary foreground/audit/exploration chat does not adjudicate failed read-only
  tools/workers/computation as task failure. Evidence and fail-on-tool-error remain;
  mutations, unknown effects, direct policy blocks and background runs stay strict.
- Removed hardcoded third-lookup blocking and file-read dedup; retained freshness
  warnings. `force` remains a compatible no-op. Shortened bundled prompts and removed
  retired-channel instructions; marked annotate_line_ages explicitly legacy/no-op.

Validation:
- Saved initial full-suite baseline: 3,864 passed, 1 pre-existing date-sensitive cron
  failure (`tests/cron/test_cron_tool_list.py::test_add_at_job_uses_default_timezone_for_naive_datetime`).
- Updated existing expectations/test doubles for changed contracts; no tests added.
- Final `scripts/test-delta.sh`: 3,865 collected; 3,864 passed; same 1 known failure;
  NEW 0 / FIXED 0. Existing audioop deprecation warning remains.
- Changed-file `uv run ruff check` and `git diff --check` pass.
- Temporary offline runtime probes exercised real Bubblewrap: 22,103-character
  artifact terminates after 3 pages; 3,000 structured rows filter inside one script;
  full artifact JSON decodes without newline repair; oversized Unicode full reads
  fail explicitly while chunks work; nested operations each validate/authorize once;
  one worker Code Mode call plus one artifact tool costs exactly 2 tool charges.
- Foreground read-only error used 2 provider requests and retained failed evidence;
  strict read-only and mutation cases still used verification (3 requests). Code Mode
  policy denial stayed a policy-block verdict. All providers in probes were fakes.

Reviewed source/security/accounting changes. No live-provider test, config edit,
service restart, merge, push, deployment or commit performed. Changes remain on
`feat/lean-codemode-harness`. Production effectiveness is not established by these
checks; run-scoped artifacts still expire at parent-run end.

### 2026-10-26 — merge/deployment authorized

User requested merge and deployment. Both development main and the clean editable
install `/home/gergo/nanobot` start at `a5cbd5595`; origin/main is identical after fetch.
Deploy to the personal local `nanobot-gateway.service`, not the separate Szárszó bot.
Restart is part of activation. No live config, secrets, dependency or service-unit
changes are planned. The calling Pi process is outside the gateway cgroup.

Feature committed as `a93742a21`, merged into main as `3ad30b2b6`, and pushed to
origin. The merged tree is identical to the tested feature tree. Re-ran the full
suite on main: 3,864 passed, same 1 known date-sensitive cron failure, clean delta.
Fast-forwarded the clean editable deployment checkout to the merge (no reset).

The last inbound message before activation was `/new`, not an in-flight model
turn. Restarted only `nanobot-gateway.service` at 11:31:34 CET; new PID 417456.
Both MCP servers connected and Agent loop started at 11:31:36; service remains
active/running. The existing COROS enabledTools catalog-mismatch warning persists.
Config file SHA-256 was identical before and after restart; no settings changed.
The separate Szárszó bot was not restarted.

Installed-runtime verification ran from `/home/gergo` through the uv-managed
editable install, confirming imports from `/home/gergo/nanobot`. Real Bubblewrap
plus an offline fake provider processed 3,000 structured rows, read back full
artifact JSON with terminating EOF, and aggregated to the expected sum in two
provider calls. Repeated installed file reads both returned full content. The
existing config validates with max_model_calls=128 and no separate iteration field.
No real LLM request or private external-data task was run; user-facing effectiveness
still needs normal live usage. This final record is documentation-only and needs
no additional service restart.
