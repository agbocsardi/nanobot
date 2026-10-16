# Pi-style compaction implementation

Branch: `feat/pi-style-compaction`, based on `f415978fd`. All changes are uncommitted in this isolated worktree. No merge, push, deployment, service restart, other agents, private-session reads, or real-provider requests.

## Architecture

- `nanobot/session/compaction.py` and `nanobot/templates/agent/compaction.md`: versioned continuity checkpoints, stable legacy-compatible message references, deterministic projection, legal token-sized cuts, split-turn prefixes, evolving summaries, structured read/modified paths, usage calibration, frozen budgets and explicit overflow classification. Summary inputs are partitioned only when necessary; output, calls (8), and wall time (120 seconds) are bounded. Failed/aborted/empty/length-truncated/no-progress summaries cannot become checkpoints.
- `nanobot/session/manager.py`: durable original transcript retains the existing redacted tool arguments/results. Checkpoints coexist in metadata; projected context is summary plus retained suffix. Saves serialize with metadata-file rewrites and atomically replace/fsync the transcript and checkpoint together. Legacy cursors/summaries still load; clear/fork handling respects checkpoint references.
- `nanobot/agent/runner.py`: safe request seams between completed tool batches, frozen provider/model/window/output allowance, actual transformed-payload checks including schemas and byte limits, usage tied to the sent model/payload/schema, bounded local/explicit-provider overflow recovery. The original append boundary never shifts; tools and completed streamed answers are not replayed. The nine-raw-image handoff remains unchanged, and summaries use the handoff projection rather than losing existing image captions.
- `nanobot/agent/loop.py`: checkpoint commit includes every referenced run-local pending batch, validates against concurrent transcript/checkpoint changes, and deduplicates the final save by stable identity. The configured summarizer route/settings are captured coherently. Stale post-turn background consolidation and normal destructive file caps are removed from these paths.
- `nanobot/agent/memory.py`, `autocompact.py`, `nanobot/command/builtin.py`: manual, idle and threshold callers share the checkpoint engine. Explicit pre-compaction personal-memory flushing remains separate; continuity never depends on Dream/history extraction. Failed compaction does not raw-archive or advance `last_consolidated`.
- `nanobot/providers/base.py`: explicit context errors return to the runner without generic retries/image stripping. Summarizers have a genuine single-call retry mode.
- Updated configuration documentation and retained Pi's MIT notice in `THIRD_PARTY_NOTICES.md`. Verified compaction/projection/controller source at `badlogic/pi-mono` revision `0fbdbd3f8ab1876ac260c0f08226a400759323b8`.

## Verification

### Focused checks

```bash
scripts/run-tests.sh tests/session tests/agent/test_runner*.py tests/agent/test_loop_runner_integration.py tests/agent/test_loop_consolidation_tokens.py tests/agent/test_consolidator.py tests/agent/test_consolidation_ratio.py tests/agent/test_auto_compact.py tests/agent/test_autocompact_unit.py tests/command/test_compact_command.py tests/agent/test_vision_handoff.py -q --disable-warnings --tb=short
```

**394 passed, 0 failed, 0 skipped** (11.23 seconds). Includes 33 new acceptance cases in `tests/session/test_compaction*.py`, notably three forced overflows within one turn with ten effects executed exactly once, both streaming modes, pending-prefix commit/final-save deduplication, reload parity, split turns, rejected/cancelled summaries, failed atomic save, races, byte-only overflow, image expansion/captions, frozen model/output settings, usage invalidation, deterministic rejection, and irreducible fixed/exchange inputs.

```bash
uv run ruff check $(git diff --name-only -- '*.py') nanobot/session/compaction.py tests/session/test_compaction.py tests/session/test_compaction_persistence.py
git diff --check
```

Both clean.

### Memory-capped full suite and baseline

An uncapped-duration run stalls in existing email polling tests, so a temporary `pytest-timeout` overlay was used with the repository's memory-capped runner. It was not added to project dependencies or installed globally.

```bash
TIMEOUT_SITE=$(uv run --with pytest-timeout python -c 'from pathlib import Path; import pytest_timeout; print(Path(pytest_timeout.__file__).parent)')
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$TIMEOUT_SITE" scripts/run-tests.sh tests --import-mode=importlib --timeout=10 --timeout-method=signal -q --disable-warnings --tb=short
```

**3 failed, 3475 passed, 2 skipped** (86.92 seconds); **3478 tests collected**. The two skips are collection skips, not two additional collected test cases.

The unchanged `f415978fd` source/tests were exported temporarily **inside this worktree**, run under the same memory/time limits and import mode with their own source first on `PYTHONPATH`, and then removed:

```bash
mkdir -p .compaction-baseline
git archive f415978fd | tar -x -C .compaction-baseline
PYTHONSAFEPATH=1 PYTHONPATH="$PWD/.compaction-baseline:$TIMEOUT_SITE" scripts/run-tests.sh .compaction-baseline/tests --import-mode=importlib --timeout=10 --timeout-method=signal -q --disable-warnings --tb=short
```

Baseline: **4 failed, 3448 passed, 2 skipped; 3452 collected**. All three current failures are present on the baseline:

- `tests/channels/test_email_channel.py::test_start_applies_post_action_only_after_delivery`
- `tests/channels/test_email_channel.py::test_start_skips_post_action_when_delivery_fails`
- `tests/channels/test_email_channel.py::test_start_keeps_post_actions_for_successful_emails_when_later_delivery_fails`

These hit the 10-second timeout; the email implementation is untouched. Baseline additionally failed `tests/agent/test_subagent_task_control.py::test_retry_creates_new_run_with_lineage_and_budgets`; the current run did not. No subagent implementation was changed.

Comparable full collections were checked explicitly: **3418 common node IDs, 60 added, 34 replaced/renamed**, a net increase of 26. Changes are confined to the new acceptance fixtures and updated compaction/history/manual-command tests. Explicit memory archival coverage is retained; assertions requiring destructive truncation, raw fallback advancement, and the old competing summarizer were replaced rather than skipped or xfailed. **No new failing node IDs versus baseline.**

Logs and collection manifests: `/tmp/pi-compaction-focused-final.txt`, `/tmp/pi-compaction-full-final.txt`, `/tmp/pi-compaction-baseline.txt`, `/tmp/pi-compaction-current-collection.txt`, `/tmp/pi-compaction-baseline-collection.txt`, `/tmp/pi-compaction-collection-delta.txt`.

## Limitations

- Full suite is not wholly green because of the baseline failures above.
- No live-provider or deployed-process verification was performed.
- Provider overflow after user-visible streamed text is not retried without a trustworthy continuation cursor; this deliberately avoids duplicate delivery. Ordinary pre-generation context errors and completed tool batches recover normally.
- Fallback wrappers do not identify the physical responder in `LLMResponse`; their usage is conservatively excluded from model-specific calibration, while heuristic and final-payload checks remain active.
