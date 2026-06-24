# Decouple fork from upstream and strip unused subsystems

> Branch: `2026-06-16_decouple`
> Started: 2026-06-16
> Goal: stop treating `HKUDS/nanobot` as a rebase target, make `origin/main` the independent main line, and remove the WebUI + OpenAI-compatible API server because they are not needed.

## Decisions

- **Decoupling mode:** keep the `upstream` remote for read-only reference (security/provider change review), but stop all rebasing/merging. Retire `personal-build`; `origin/main` becomes the integration branch.
- **Channels to keep:** Telegram, Discord, Email, WebSocket.
- **Channels to remove:** DingTalk, Feishu/Lark, Matrix, MoChat, MS Teams, QQ/NapCat, Signal, Slack, WeCom, WeChat/Weixin, WhatsApp.
- **Providers to keep:** `custom`, `openrouter`, `openai_codex`, and current OpenCode-backed providers: `anthropic`, `openai`, `deepseek`, `dashscope`, `minimax`, `minimax_anthropic`, `moonshot`, `xiaomi_mimo`, `zai`, `zhipu`.
- **Providers to remove:** empty/unused direct, gateway, local, and OAuth providers: `aihubmix`, `azure_openai`, `bedrock`, `byteplus`, `byteplus_coding_plan`, `gemini`, `github_copilot`, `groq`, `mistral`, `ollama`, `ovms`, `qianfan`, `siliconflow`, `stepfun`, `vllm`, `volcengine`, `volcengine_coding_plan`, plus registry-only unused providers (`huggingface`, `skywork`, `novita`, `lm_studio`, `atomic_chat`, `nvidia`, `assemblyai`, `longcat`, `ant_ling`) unless proven in use.
- **Subsystems to keep:** built-in skills, kept providers/tools, agent core, memory/session, CLI.
- **Subsystems to remove now:** WebUI (`webui/`, `nanobot/web/`, `nanobot/webui/`), OpenAI-compatible API server (`nanobot/api/`), unused channels, unused providers, and the WhatsApp bridge (`bridge/`).
- **Strip strategy:** bounded deletion for WebUI/API/channels/providers (well-defined surfaces), then evaluate deeper tool/skill pruning later.

## Top-level checklist

- [x] 1. Update remotes and branch model
- [ ] 2. Update documentation (AGENTS.md, CONTRIBUTING.md, README)
- [x] 3. Remove API server (`nanobot/api/`)
- [x] 4. Remove WebUI runtime (`nanobot/webui/`)
- [x] 5. Remove bundled WebUI dist (`nanobot/web/`)
- [x] 6. Remove WebUI source app (`webui/`)
- [x] 7. Strip WebUI/API config and CLI commands
- [x] 8. Remove unused channels
- [x] 9. Remove WhatsApp bridge (`bridge/`)
- [x] 10. Prune unused providers
- [x] 11. Clean up provider/tool references to removed surfaces
- [x] 12. Update `pyproject.toml` build config and dependencies
- [x] 13. Delete or update affected tests
- [x] 14. Run targeted test/lint checks
- [x] 15. Merge `2026-06-16_decouple` into `main` and push to `origin`
- [ ] 16. (Later) audit tools and skills for removal

## Detailed plan

### 1. Update remotes and branch model

- Keep `upstream https://github.com/HKUDS/nanobot.git` as a fetch-only reference.
- Delete local tracking branches for upstream (e.g. `upstream/main`, upstream feature branches).
- Retire `personal-build`. Future workflow: work on topic branches off `origin/main`, merge via PR.
- Update `.agent/design.md` or `AGENTS.md` if they mention the old fork workflow.

> Tip for upstream review: `git fetch upstream && git log --oneline upstream/main --since='2 weeks ago'` lets you scan security/provider changes without rebasing.

### 2. Update documentation

- `AGENTS.md`: remove the "Nanobot Fork Workflow" section (upstream/origin/personal-build instructions).
- `CONTRIBUTING.md`: remove upstream contribution flow; replace with local fork contribution flow.
- `README.md`: remove WebUI screenshots/mentions and API server usage examples if present.
- Add a short note that this is an independent fork and upstream is kept only for reference.

### 3. Remove API server

- Delete `nanobot/api/`.
- Remove the `api` subcommand in `nanobot/cli/commands.py` (`api_server` function and its `@app.command()` registration).
- Remove `[project.optional-dependencies] api = [...]` from `pyproject.toml`.
- Delete `tests/test_api_attachment.py` and `tests/test_api_stream.py`.
- Check for any remaining `from nanobot.api.server` imports.

### 4. Remove WebUI runtime

- Delete `nanobot/webui/`.
- Delete `nanobot/session/webui_turns.py` (title generation can move to a generic session helper later, or be removed if it was WebUI-only).
- Remove WebUI aliases in `nanobot/utils/__init__.py` (`webui_thread_disk`, `webui_transcript`, etc.).
- Update `nanobot/cli/commands.py`:
  - Remove `webui` command and all WebUI-related flags (`webui_static_dist`, `webui_runtime_surface`, etc.).
  - Remove `WebuiTurnCoordinator` and `TokenUsageHook` imports.
  - Remove the "wait for gateway, open browser" logic if WebUI-specific.
- Update `nanobot/config/schema.py`:
  - Remove WebUI-related config fields (`webui_allow_local_service_access`, gateway token settings, etc.).
- Update `nanobot/config/paths.py`:
  - Remove `get_webui_dir()`.
- Update `nanobot/agent/tools/shell.py`:
  - Remove the local WebUI preview path (`webui_allow_local_service_access` flag and the preview server branch).

### 5. Remove bundled WebUI dist

- Delete `nanobot/web/` (only contained the built Vite artifact).
- Remove `nanobot/web/dist/**/*` from `pyproject.toml` `[tool.hatch.build]` includes/artifacts.

### 6. Remove WebUI source app

- Delete `webui/` directory entirely.
- Remove `webui/` references from `.gitignore` if any are no longer needed.

### 7. Remove unused channels

Keep:

- `nanobot/channels/telegram.py`
- `nanobot/channels/discord.py`
- `nanobot/channels/email.py`
- `nanobot/channels/websocket.py`
- `nanobot/channels/base.py`, `manager.py`, `registry.py`, `__init__.py`

Remove:

- `nanobot/channels/dingtalk.py`
- `nanobot/channels/feishu.py`
- `nanobot/channels/matrix.py`
- `nanobot/channels/mochat.py`
- `nanobot/channels/msteams.py`
- `nanobot/channels/napcat.py`
- `nanobot/channels/qq.py`
- `nanobot/channels/signal.py`
- `nanobot/channels/slack.py`
- `nanobot/channels/wecom.py`
- `nanobot/channels/weixin.py`
- `nanobot/channels/whatsapp.py`

Then update:

- `nanobot/channels/__init__.py`
- `nanobot/channels/manager.py` / `registry.py` if they assume removed modules exist.
- `nanobot/config/schema.py` channel config fields.
- Tests under `tests/channels/` for removed channels.

### 8. Remove WhatsApp bridge

- Delete `bridge/`.
- Remove `bridge/` from `pyproject.toml` source distribution includes.
- Remove `[tool.hatch.build.targets.wheel.force-include]` entry that maps `bridge` to `nanobot/bridge`.
- Remove bridge build/runtime docs from `README.md` if present.

### 9. Prune unused providers

Keep provider implementation files:

- `nanobot/providers/base.py`
- `nanobot/providers/factory.py`
- `nanobot/providers/fallback_provider.py`
- `nanobot/providers/registry.py`
- `nanobot/providers/openai_compat_provider.py`
- `nanobot/providers/anthropic_provider.py`
- `nanobot/providers/openai_codex_provider.py`
- `nanobot/providers/openai_responses/`
- `nanobot/providers/transcription.py` and `nanobot/providers/whisper/` if voice transcription stays enabled.
- `nanobot/providers/image_generation.py` if the image-generation tool/skill stays enabled.

Keep provider registry/config entries:

- `custom`
- `openrouter`
- `openai_codex`
- `anthropic`
- `openai`
- `deepseek`
- `dashscope`
- `minimax`
- `minimax_anthropic`
- `moonshot`
- `xiaomi_mimo`
- `zai`
- `zhipu`

Remove provider implementation files:

- `nanobot/providers/azure_openai_provider.py`
- `nanobot/providers/bedrock_provider.py`
- `nanobot/providers/github_copilot_provider.py`

Remove provider registry/config entries for unused providers:

- `aihubmix`
- `azure_openai`
- `bedrock`
- `byteplus`
- `byteplus_coding_plan`
- `gemini`
- `github_copilot`
- `groq`
- `mistral`
- `ollama`
- `ovms`
- `qianfan`
- `siliconflow`
- `stepfun`
- `vllm`
- `volcengine`
- `volcengine_coding_plan`
- `huggingface`
- `skywork`
- `novita`
- `lm_studio`
- `atomic_chat`
- `nvidia`
- `assemblyai`
- `longcat`
- `ant_ling`

Implementation notes:

- Preserve dynamic custom-provider support (`ProvidersConfig.model_config = ConfigDict(extra="allow")`) so one-off OpenAI-compatible endpoints can still be added without code changes.
- Do not collapse all OpenCode routes into a single `opencode` provider in this pass; that would require config/preset migration and is easier after the deletion pass is stable.
- Keep `openai_responses/` because both `openai_compat_provider.py` and `openai_codex_provider.py` use it.
- Delete or update tests for removed provider files/specs.

### 10. Update `pyproject.toml`

- Remove `aiohttp` from `[project.optional-dependencies] api` (already optional, but verify it is not needed elsewhere; note `dev` may still use it).
- Remove optional dependencies for deleted channels:
  - `wecom`
  - `weixin`
  - `msteams`
  - `matrix`
- Remove base dependencies used only by deleted channels, if no remaining imports need them:
  - `dingtalk-stream`
  - `lark-oapi`
  - `python-socketio`
  - `slack-sdk`
  - `slackify-markdown`
  - `qq-botpy`
  - `python-socks[asyncio]` if only proxy support for removed channels used it
- Keep Discord optional dependency (`discord.py`) and Telegram dependency (`python-telegram-bot`).
- Remove `nanobot/web/dist/**/*` artifacts/includes.
- Review build hook in `hatch_build.py` for WebUI build steps; remove if present.
- Remove `bridge` `force-include` since WhatsApp bridge is removed.

### 11. Clean up remaining references

Run these searches and fix any remaining imports:

```bash
grep -RIn "nanobot.webui\|nanobot.web\|nanobot.api" nanobot/ tests/
grep -RIn "WebuiTurnCoordinator\|webui_turns\|webui_allow_local" nanobot/ tests/
grep -RIn "get_webui_dir" nanobot/ tests/
```

### 12. Tests

- Delete tests that only cover WebUI/API.
- Update any tests that import removed modules.
- Run targeted checks:
  ```bash
  uv run ruff check nanobot/cli/commands.py nanobot/config/schema.py nanobot/agent/tools/shell.py
  uv run --extra dev python -m pytest tests/config tests/agent -x -q
  ```

### 13. Final verification

- `uv run nanobot --help` should still work and not list `api`/`webui` commands.
- `uv run nanobot gateway` should start without WebUI attachment errors.
- `uv run ruff check nanobot/` should pass.

## Memory redesign notes — Letta/MemFS inspiration

Sources reviewed:

- Letta Code Memory docs: agents vs conversations, `/init`, `/remember`, `/sleeptime`, `/doctor`, MemFS default for new agents.
- Letta Code MemFS docs: git-backed markdown memory repository, required `description:` frontmatter, special `system/` directory loaded every turn, agent-scoped skills, memory CLI (`status`, `diff`, `backup`, `restore`, `tokens`), memory subagents using git worktrees.
- Letta / Letta Code READMEs: stateful agents, memory blocks/MemFS, subagents, schedules/channels, skills, model-agnostic provider setup.
- Cameron Pfiffer's Co-3 post: lived example of a long-running personal Letta agent; memory organized around `system/cameron.md`, `system/how_we_work.md`, `system/persona.md`, `system/procedures.md`, `system/recursive_improvement.md`, `system/now.md`, and `system/subconscious.md`.

### What nanobot already has

- Git-backed workspace memory (`GitStore`) with markdown files.
- `memory/system/*.md` loaded fully into every prompt.
- Lazy topic memory via Memory Tree descriptions.
- Frontmatter `description:` parsing for discoverability.
- Agent-editable topic files and Dream-managed system files.
- Agent-scoped skills under `skills/`.
- Dream/reflection and memory-defrag concepts.

This is already close to Letta MemFS. The main gap is not storage; it is memory lifecycle control.

### Lessons to import

1. **Make memory a first-class filesystem, not a hidden Dream side effect.**
   - Add explicit CLI/command affordances inspired by Letta: memory status, diff, backup, restore, tokens.
   - Make memory git state visible before/after background edits.

2. **Split Dream from Defrag.**
   - Dream/reflection should only ingest recent events and make small targeted updates.
   - Defrag/doctor should own restructuring, deduplication, splitting/merging files, and system/ promotion/demotion.
   - Dream must not perform wholesale hierarchy redesign.

3. **Adopt a better `system/` taxonomy.**
   Current files are `procedures.md`, `corrections.md`, `now.md`. Consider expanding toward:
   - `system/user.md` or keep `USER.md`: stable user profile and preferences.
   - `system/persona.md` or keep `SOUL.md`: agent identity and behavioral principles.
   - `system/how_we_work.md`: interaction style, anti-patterns, collaboration norms.
   - `system/recursive_improvement.md`: corrections, recurring model/agent failure modes.
   - `system/procedures.md`: general policies and memory rules.
   - `system/now.md`: high-churn active context, aggressively pruned.
   - `system/subconscious.md`: scratch/catcher's-mitt area for background reflection observations that are not yet promoted.

4. **Use trigger policy matched to actual usage.**
   - Compaction-only is not enough here because sessions are usually short and rarely compact.
   - Prefer a hybrid trigger:
     - every N user turns across all channels, e.g. 8–12;
     - plus idle/background timer, e.g. every 6–12 hours if new history exists;
     - plus manual `/remember` and `/dream-now`;
     - plus optional compaction trigger as a bonus, not the main trigger.

5. **Guardrails for Dream are mandatory.**
   - Separate Dream config from regular max tool iterations.
   - Small max iterations, e.g. 6–10.
   - Wall-clock timeout.
   - Max changed files and max diff size.
   - Require final explicit completion marker/tool.
   - If Dream times out or hits max iterations, rollback memory working tree and do not advance cursor.
   - Commit only if changed files pass validation.

6. **Use git worktrees or checkpoints for background memory writes.**
   - Letta memory subagents use git worktrees so background edits do not collide with the main agent.
   - Minimum viable nanobot version: take a git checkpoint before Dream and rollback on failure.
   - Better version: Dream edits in a temporary worktree/branch, validates, then merges into memory.

7. **Make primary-agent memory updates more direct.**
   - Keep topic files hot-editable by the main agent.
   - Add `/remember <text>` for targeted user-directed memory writes.
   - Consider allowing the main agent to propose small updates to user/profile/correction memory via a constrained memory-write tool, instead of waiting for Dream to infer everything later.

8. **Track memory health explicitly.**
   - Add memory token accounting for `system/`.
   - Add stale `now.md` checks.
   - Add frontmatter/description validation.
   - Add duplicate/overlap audit via doctor/defrag.

### PR #3990 / old Dream safeguard findings

User observed Dream going off the rails in a read/write/edit loop for ~200 iterations. Investigation:

- PR #3990 (`d1a94dae`, final PR branch fetched as `upstream/pr-3990`) replaced the old two-phase `Dream` class with `AgentLoop.process_direct(..., ephemeral=True)`.
- The old `Dream` class had real guardrails:
  - `max_batch_size` default 20: maximum `history.jsonl` entries per Dream run.
  - `max_iterations` default 10 in `Dream.__init__`, wired from config default 15: maximum Phase 2 agent/tool iterations.
  - `max_tool_result_chars` default 16,000.
  - per-entry prompt preview cap: `_HISTORY_ENTRY_PREVIEW_MAX_CHARS = 4_000`.
  - prompt preview caps for memory files.
  - `model_override` config field for a Dream-specific model.
  - cursor advanced only on successful completion.
- After #3990, the fields still exist in `DreamConfig` but are explicitly marked deprecated/no longer used:
  - `model_override` comment: "pending implementation"
  - `max_batch_size`: "Deprecated: no longer used"
  - `max_iterations`: "Deprecated: no longer used"
  - `annotate_line_ages`: "Deprecated: no longer used"
- Current single-phase Dream still caps history entries internally via `MemoryStore.build_dream_prompt(max_entries=20)`, but the cron handler calls it with the default hardcoded value and does not read config.
- Current single-phase Dream inherits the normal agent's `max_iterations` via `process_direct`, which explains a 200-iteration read/write/edit loop if runtime max iterations had been raised.
- Current single-phase Dream has no Dream-specific timeout, no max changed files/diff-size validation, and no rollback of file edits when the run is incomplete. It only avoids cursor advancement.

Conclusion: restore safeguards **around** the current process-direct Dream path, not by reviving the old two-phase Dream class.

Minimal restoration plan:

1. Rewire existing config fields:
   - Rename comments to active again.
   - `dream.max_batch_size` passed to `store.build_dream_prompt(max_entries=...)`.
   - `dream.max_iterations` passed through to the Dream-only agent run.
   - `dream.model_override` resolves a separate provider/model snapshot for Dream.
2. Add Dream-specific process-direct override:
   - Extend `AgentLoop.process_direct(..., max_iterations: int | None = None, provider: LLMProvider | None = None, model: str | None = None, context_window_tokens: int | None = None)` or add a small `run_ephemeral_dream(...)` wrapper that calls `runner.run(AgentRunSpec(... max_iterations=dream_cfg.max_iterations ...))`.
   - Prefer wrapper to avoid making normal direct calls more complex.
3. Add incomplete-run protection:
   - Before Dream, record git `HEAD` and dirty diff for memory files.
   - If stop reason is not `completed`, rollback Dream edits and do not advance cursor.
   - If rollback feels too broad, first MVP can `git diff -- memory SOUL.md USER.md skills` and refuse to commit while leaving cursor unchanged; better is rollback.
4. Add new guardrail fields:
   - `timeout_s` default 300–600.
   - `max_changed_files` default 8.
   - `max_diff_chars` default 32,000.
   - `keep_sessions` default 10.
5. Keep existing single-phase prompt and tools for now.
   - Do not restore `dream_phase1.md` / `dream_phase2.md` or the old `Dream` class.
   - Keep the simpler file-edit tool registry from current `MemoryStore.build_dream_tools()`.

### Proposed next memory phases

- [ ] M0. Document current memory architecture and Letta/MemFS target model.
- [ ] M1. Restore Dream safeguards without restoring the old class: config-wired batch size, Dream-specific model, Dream-specific max iterations, timeout, changed-file/diff limits, and rollback-on-incomplete.
  - [x] M1a. Rewire `max_batch_size`, `model_override`, `max_iterations`, and 300s timeout into current single-phase Dream path.
  - [ ] M1b. Add changed-file/diff limits.
    - Add `dream.max_changed_files` (suggested default: 8).
    - Add `dream.max_diff_chars` (suggested default: 32,000).
    - After Dream returns `completed`, inspect git diff for tracked memory surfaces before auto-commit.
    - If the diff is too broad, treat the run as incomplete: do not commit and do not advance cursor.
    - Count only memory-owned surfaces: `SOUL.md`, `USER.md`, `memory/**/*.md`, `skills/**/SKILL.md`.
    - Add tests for successful small diff, too many files, and too-large diff.
  - [ ] M1c. Add rollback-on-incomplete.
    - Before Dream starts, snapshot memory working tree state.
    - If Dream times out, raises, hits max iterations, or violates diff limits, restore the pre-Dream state.
    - First implementation can require a clean memory git state before Dream and use `git restore`/dulwich checkout for tracked memory files plus removal of new untracked memory files.
    - Better later implementation: run Dream in a temporary git worktree/branch and merge only validated commits.
    - Add tests that incomplete Dream leaves memory files unchanged and cursor unchanged.
  - [ ] M1d. Add observability.
    - Log Dream model, batch size, max iterations, timeout, stop reason, changed files, and diff size.
    - Include incomplete reason in Dream session metadata or commit/log output.
- [ ] M2. Change Dream trigger policy from pure cron to hybrid turn-count + idle timer + manual, with compaction as optional extra.
- [ ] M3. Add `/remember` for targeted memory writes and `/memory status|diff|backup|tokens` command surface.
- [ ] M4. Split Dream and Defrag responsibilities in prompts/tools: Dream = recent deltas; Defrag/Doctor = reorganization.
- [ ] M5. Consider worktree-based Dream execution after rollback/checkpoint MVP is stable.

## Future work (do not do in this pass)

- Audit whether Email is actually used; if not, delete it in a later pass.
- Keep WebSocket as the minimal programmatic/dev channel; revisit only if it creates real maintenance cost.
- Consider collapsing OpenCode-backed providers into one explicit `opencode` provider after current config/presets are stable.
- Audit image-generation and transcription provider clients separately; they have their own provider sub-registries.
- Audit tools: remove `image_generation`, `long_task`, `cron`, `mcp`, etc. if unused.
- Audit skills: many `nanobot/skills/` directories may be WebUI or API-specific.
- Audit dependencies: after removals, trim `pyproject.toml` deps to match.

## Log

### 2026-06-16

- Created `2026-06-16_decouple` branch off `personal-build`.
- Decided to keep `upstream` remote read-only, retire `personal-build`, and remove WebUI/API server as the first bounded deletion.
- Wrote this plan.
- Updated channel scope: keep Telegram, Discord, and Email; remove all other channels including WebSocket unless explicitly needed later.
- Revised channel scope again: keep WebSocket for programmatic/dev use.
- Reclassified `bridge/` as removable because it is WhatsApp-specific and WhatsApp is no longer in scope.
- Added provider-pruning scope: keep active OpenCode-backed providers, `custom`, `openrouter`, and `openai_codex`; remove empty/unused provider specs and native backends.
- Executed the first code-pruning pass: removed WebUI/API, unused channels, WhatsApp bridge, and unused native provider backends; kept Telegram, Discord, Email, and a minimal programmatic WebSocket channel.
- Simplified WebSocket so it no longer depends on WebUI gateway services; it now supports plain text, `new_chat`, `attach`, and `message` JSON envelopes.
- Updated provider registry/config to keep `custom`, `openrouter`, `openai_codex`, `anthropic`, `openai`, `deepseek`, `dashscope`, `minimax`, `minimax_anthropic`, `moonshot`, `xiaomi_mimo`, `zai`, and `zhipu`.
- Verification: targeted lint/compile passed; `uv run nanobot --help` no longer lists `serve`/API or WebUI commands; combined kept-channel/provider/security/config subset passed (`327 passed, 1 skipped`). Full suite still has unrelated/stale failures, first observed in `tests/agent/test_auto_compact.py::TestAutoCompactEdgeCases::test_auto_compact_with_nothing_summary`.
- Added planning notes from Letta Code Memory/MemFS docs, Letta/Letta Code READMEs, and Cameron Pfiffer's Co-3 post. Key conclusion: nanobot is already structurally close to MemFS; next work should focus on Dream guardrails, hybrid triggers, memory command surface, and clearer Dream-vs-Defrag ownership.
- Investigated upstream PR #3990 (`d1a94dae`, final PR branch fetched as `upstream/pr-3990`). Found that old Dream safeguards were mostly config fields and runner limits removed from the execution path, not storage architecture. Plan is to restore those guardrails around current single-phase `process_direct` Dream instead of restoring the old two-phase Dream class.
- Implemented M1a: Dream now honors `dream.max_batch_size`, `dream.model_override`, `dream.max_iterations`, and `dream.timeout_s` (default 300s). Incomplete/timed-out Dream runs no longer auto-commit. Targeted tests passed: `tests/config/test_dream_config.py`, `tests/agent/test_dream.py`, `tests/command/test_builtin_dream.py`, `tests/agent/test_dream_tools.py`.
- Documented remaining Dream hardening work: changed-file limits, diff-size limits, rollback-on-incomplete, and observability.

### 2026-06-24

- Scar-raided selected upstream v0.2.2 hardening onto `feat/upstream-port-2026-06`, then fast-forwarded fork `main` through `decouple + ports`: reasoning wrapper leak normalization, Anthropic tool-ID sanitization/deduplication, builtin-parameter strictness, memory cursor guards, replay-window durability, git-in-workspace-subdir shell support, and first-class `opencode_zen` / `opencode_go` providers.
- Verification before merge: import sanity passed, `uv run nanobot --help` no longer showed API/WebUI commands, and 306 targeted tests passed. Known pre-existing stale failure remains: `tests/agent/test_auto_compact.py::TestAutoCompactEdgeCases::test_auto_compact_with_nothing_summary`.
- Completed branch-model switch locally: `main` now tracks `origin/main` as the independent fork line. `upstream/main` remains fetch-only reference for future manual scar-raiding; no upstream tracking/merging on fork `main`.
- Deferred the workspace exact-file allowlist arc as not load-bearing for current needs; Dream M1a limits and planned rollback/diff guards cover the realistic runaway-Dream failure mode.
