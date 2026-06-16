# Decouple fork from upstream and strip unused subsystems

> Branch: `2026-06-16_decouple`
> Started: 2026-06-16
> Goal: stop treating `HKUDS/nanobot` as a rebase target, make `origin/main` the independent main line, and remove the WebUI + OpenAI-compatible API server because they are not needed.

## Decisions

- **Decoupling mode:** keep the `upstream` remote for read-only reference (security/provider change review), but stop all rebasing/merging. Retire `personal-build`; `origin/main` becomes the integration branch.
- **Channels:** keep all for now. No channel deletion in this pass.
- **Subsystems to keep:** bridge services, built-in skills, all providers/tools, agent core, memory/session, CLI.
- **Subsystems to remove now:** WebUI (`webui/`, `nanobot/web/`, `nanobot/webui/`) and the OpenAI-compatible API server (`nanobot/api/`).
- **Strip strategy:** bounded deletion for WebUI/API (well-defined surface), then lazy-load evaluation for future channel/provider/tool pruning.

## Top-level checklist

- [ ] 1. Update remotes and branch model
- [ ] 2. Update documentation (AGENTS.md, CONTRIBUTING.md, README)
- [ ] 3. Remove API server (`nanobot/api/`)
- [ ] 4. Remove WebUI runtime (`nanobot/webui/`)
- [ ] 5. Remove bundled WebUI dist (`nanobot/web/`)
- [ ] 6. Remove WebUI source app (`webui/`)
- [ ] 7. Strip WebUI/API config and CLI commands
- [ ] 8. Clean up provider/tool references to removed surfaces
- [ ] 9. Update `pyproject.toml` build config and dependencies
- [ ] 10. Delete or update affected tests
- [ ] 11. Run targeted test/lint checks
- [ ] 12. Merge `2026-06-16_decouple` into `main` and push to `origin`
- [ ] 13. (Later) audit channels, providers, tools, skills for removal

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

### 7. Update `pyproject.toml`

- Remove `aiohttp` from `[project.optional-dependencies] api` (already optional, but verify it is not needed elsewhere; note `matrix` and `dev` also use it).
- Remove `nanobot/web/dist/**/*` artifacts/includes.
- Review build hook in `hatch_build.py` for WebUI build steps; remove if present.
- Remove `bridge` `force-include` only if bridge is also being removed; **keep it** since bridge services are retained.

### 8. Clean up remaining references

Run these searches and fix any remaining imports:

```bash
grep -RIn "nanobot.webui\|nanobot.web\|nanobot.api" nanobot/ tests/
grep -RIn "WebuiTurnCoordinator\|webui_turns\|webui_allow_local" nanobot/ tests/
grep -RIn "get_webui_dir" nanobot/ tests/
```

### 9. Tests

- Delete tests that only cover WebUI/API.
- Update any tests that import removed modules.
- Run targeted checks:
  ```bash
  uv run ruff check nanobot/cli/commands.py nanobot/config/schema.py nanobot/agent/tools/shell.py
  uv run --extra dev python -m pytest tests/config tests/agent -x -q
  ```

### 10. Final verification

- `uv run nanobot --help` should still work and not list `api`/`webui` commands.
- `uv run nanobot gateway` should start without WebUI attachment errors.
- `uv run ruff check nanobot/` should pass.

## Future work (do not do in this pass)

- Audit channels: if only 1–2 are used, delete the rest and their optional deps.
- Audit providers: keep only OpenAI-compatible + Anthropic if that covers all models.
- Audit tools: remove `image_generation`, `long_task`, `cron`, `mcp`, etc. if unused.
- Audit skills: many `nanobot/skills/` directories may be WebUI or API-specific.
- Audit dependencies: after removals, trim `pyproject.toml` deps to match.

## Log

### 2026-06-16

- Created `2026-06-16_decouple` branch off `personal-build`.
- Decided to keep `upstream` remote read-only, retire `personal-build`, and remove WebUI/API server as the first bounded deletion.
- Wrote this plan.
