# Nanobot Agent Guidance

This file provides guidance to AI coding agents working with this repository.

## Project Overview

nanobot is a lightweight, open-source AI agent framework written in Python with a React/TypeScript WebUI. It centers around a small agent loop that receives messages from chat channels, invokes an LLM provider, executes tools, and manages session memory.

## Development Commands

```bash
# Python: run single test / lint
pytest tests/test_openai_api.py::test_function -v
ruff check nanobot/

# WebUI: dev server (proxies API/WS to gateway :8765), build, test
# Build outputs to ../nanobot/web/dist (bundled into the Python wheel)
cd webui && bun run dev      # or NANOBOT_API_URL=... bun run dev
cd webui && bun run build
cd webui && bun run test

# Gateway
nanobot gateway
```

## High-Level Architecture

### Core Data Flow

Messages flow through an async `MessageBus` (`nanobot/bus/queue.py`) that decouples chat channels from the agent core:

1. **Channels** (`nanobot/channels/`) receive messages from external platforms and publish `InboundMessage` events to the bus.
2. **`AgentLoop`** (`nanobot/agent/loop.py`) consumes inbound messages, builds context, and coordinates the turn.
3. **`AgentRunner`** (`nanobot/agent/runner.py`) handles the actual LLM conversation loop: send messages to the provider, receive tool calls, execute tools, and stream responses.
4. Responses are published as `OutboundMessage` events back to the appropriate channel.

### Key Subsystems

- **Agent Loop** (`nanobot/agent/loop.py`, `runner.py`): The core processing engine. `AgentLoop` manages session keys, hooks, and context building. `AgentRunner` executes the multi-turn LLM conversation with tool execution.
- **LLM Providers** (`nanobot/providers/`): Provider implementations (Anthropic, OpenAI-compatible, OpenAI Responses API, Azure, Bedrock, GitHub Copilot, OpenAI Codex, etc.) built on a common base (`base.py`). Includes image generation (`image_generation.py`) and audio transcription (`transcription.py`). `factory.py` and `registry.py` handle instantiation and model discovery.
- **Channels** (`nanobot/channels/`): Platform integrations (Telegram, Discord, Slack, Feishu, Matrix, WhatsApp, QQ, WeChat, WeCom, DingTalk, Email, MoChat, MS Teams, WebSocket). `manager.py` discovers and coordinates them. Channels are auto-discovered via `pkgutil` scan + entry-point plugins.
- **Tools** (`nanobot/agent/tools/`): Agent capabilities exposed to the LLM: filesystem (read/write/edit/list), shell execution (with sandbox backends), web search/fetch, MCP servers, cron, notebook editing, subagent spawning, long-running tasks / sustained goals (`long_task.py`), image generation, and self-modification. Tools are auto-discovered via `pkgutil` scan + entry-point plugins.
- **Memory** (`nanobot/agent/memory.py`): Session history persistence with Dream two-phase memory consolidation. Uses atomic writes with fsync for durability.
- **Session Management** (`nanobot/session/`): Per-session history, context compaction, TTL-based auto-compaction (`manager.py`), and sustained goal state tracking (`goal_state.py`).
- **Config** (`nanobot/config/schema.py`, `loader.py`): Pydantic-based configuration loaded from `~/.nanobot/config.json`. Supports camelCase aliases for JSON compatibility.
- **Bridge** (`bridge/`): TypeScript services (e.g. WhatsApp bridge) bundled into the wheel via `pyproject.toml` `force-include`.
- **WebUI** (`webui/`): Vite-based React SPA that talks to the gateway over a WebSocket multiplex protocol. The dev server proxies `/api`, `/webui`, `/auth`, and WebSocket traffic to the gateway.
- **API Server** (`nanobot/api/server.py`): OpenAI-compatible HTTP API (`/v1/chat/completions`, `/v1/models`) for programmatic access.
- **Command Router** (`nanobot/command/`): Slash command routing and built-in command handlers.
- **Heartbeat** (`nanobot/templates/HEARTBEAT.md`): Periodic task list checked via `cron` jobs (legacy dedicated service removed).
- **Pairing** (`nanobot/pairing/`): DM sender approval store with persistent pairing codes per channel.
- **Skills** (`nanobot/skills/`): Built-in skill definitions (long-goal, cron, github, image-generation, etc.) loaded into agent context.
- **Security** (`nanobot/security/`): PTH file guard and other security measures activated at CLI entry.

### Entry Points

- **CLI**: `nanobot/cli/commands.py`
- **Python SDK**: `nanobot/nanobot.py`

## Project-Specific Notes

- Architecture constraints: [`.agent/design.md`](.agent/design.md)
- Security boundaries: [`.agent/security.md`](.agent/security.md)
- Common gotchas: [`.agent/gotchas.md`](.agent/gotchas.md)

## Upstream Branching Strategy

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for contribution flow and PR guidelines.

## Code Style

- Python 3.11+, asyncio throughout.
- Line length: 100.
- Linting: `ruff` with rules E, F, I, N, W (E501 ignored).
- pytest with `asyncio_mode = "auto"`.

## Common File Locations

- Config schema: `nanobot/config/schema.py`
- Provider base / new provider template: `nanobot/providers/base.py`
- Channel base / new channel template: `nanobot/channels/base.py`
- Tool registry: `nanobot/agent/tools/registry.py`
- WebUI dev proxy config: `webui/vite.config.ts`
- Tests mirror the `nanobot/` package structure.

---

# Nanobot Fork Workflow

## Remote layout

- `upstream` = `HKUDS/nanobot` (source of truth)
- `origin` = `agbocsardi/nanobot` (this fork)
- `origin/main` should mirror `upstream/main`
- Long-lived fork features live as topic branches off `main`
- `personal-build` is a disposable integration/deploy branch

```
upstream/main ─── origin/main
        ├── feat/hierarchical-memory-next
        ├── feat/faster-whisper-next
        └── docs/personal-build-workflow

personal-build = main + selected topic branches
```

## Sync workflow

Use this when updating the fork after upstream releases. Keep conflicts isolated by rebasing topic branches one at a time, then rebuilding `personal-build` from scratch.

Before changing branches:

```bash
git status --short --branch
git remote -v
git fetch upstream
git fetch origin
```

If the working tree is dirty, stop and inspect the diff. Stash or commit intentional edits before continuing.

### 1. Mirror upstream main

```bash
git checkout main
git reset --hard upstream/main
git push --force-with-lease origin main
```

### 2. Rebase each topic branch

```bash
git checkout feat/hierarchical-memory-next
git rebase main
git push --force-with-lease origin feat/hierarchical-memory-next

git checkout feat/faster-whisper-next
git rebase main
git push --force-with-lease origin feat/faster-whisper-next

git checkout docs/personal-build-workflow
git rebase main
git push --force-with-lease origin docs/personal-build-workflow
```

Resolve conflicts on the topic branch where they belong.

### 3. Rebuild personal-build

`personal-build` is cattle, not pet. Recreate it from `main` and merge the selected topic branches:

```bash
git checkout -B personal-build main
git merge --no-ff origin/docs/personal-build-workflow
git merge --no-ff origin/feat/hierarchical-memory-next
git merge --no-ff origin/feat/faster-whisper-next

# verify the branch is only main + integration merges
git log --oneline --first-parent main..personal-build

git push --force-with-lease origin personal-build
```

## Active topic branches

- `feat/hierarchical-memory-next` — hierarchical memory, Memory Tree, Dream prompt changes
- `feat/faster-whisper-next` — local faster-whisper STT provider
- `docs/personal-build-workflow` — this workflow doc

Old branches may exist for history. Prefer the `*-next` branches above for future maintenance.

## Conflict minimization

1. **Surgical diffs.** Change only the lines you need. Don't reformat adjacent code.
2. **Topic branches off `main`.** Do not develop directly on `personal-build`.
3. **New files > modified files.** New files conflict less during rebase.
4. **Enable `rerere`.** Remembers conflict resolutions:
   ```bash
   git config --global rerere.enabled true
   ```
5. **Drop superseded patches.** If upstream ships equivalent behavior, remove the topic branch from the rebuild.

## Server deploy (`uhl`)

Use this only after `origin/personal-build` is updated and verified.

```bash
ssh uhl
cd ~/nanobot
git status --short --branch
git fetch origin
git checkout personal-build
git reset --hard origin/personal-build
```

The live install on `uhl` is editable, so code changes are live after checkout/reset. Restart any already-running Nanobot process to pick up imported Python changes. Run nanobot from a workspace directory, not from inside the repo.

## Verification

Targeted checks are more useful than broad lint during an update:

```bash
uv run ruff check nanobot/agent/memory.py nanobot/agent/context.py nanobot/config/schema.py
uv run --extra dev python -m pytest tests/agent/test_memory_store.py tests/agent/test_context_builder.py
uv run --extra dev python -m pytest tests/providers/test_transcription.py tests/channels/test_base_channel.py
```

If STT changed, restart Nanobot and send a voice note. A successful transcription is the real end-to-end check.
