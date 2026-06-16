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

- [ ] 1. Update remotes and branch model
- [ ] 2. Update documentation (AGENTS.md, CONTRIBUTING.md, README)
- [ ] 3. Remove API server (`nanobot/api/`)
- [ ] 4. Remove WebUI runtime (`nanobot/webui/`)
- [ ] 5. Remove bundled WebUI dist (`nanobot/web/`)
- [ ] 6. Remove WebUI source app (`webui/`)
- [ ] 7. Strip WebUI/API config and CLI commands
- [ ] 8. Remove unused channels
- [ ] 9. Remove WhatsApp bridge (`bridge/`)
- [ ] 10. Prune unused providers
- [ ] 11. Clean up provider/tool references to removed surfaces
- [ ] 12. Update `pyproject.toml` build config and dependencies
- [ ] 13. Delete or update affected tests
- [ ] 14. Run targeted test/lint checks
- [ ] 15. Merge `2026-06-16_decouple` into `main` and push to `origin`
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
