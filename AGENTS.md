# Nanobot Agent Guidance

This file provides guidance to AI coding agents working with this repository.

## Project Overview

nanobot is a lightweight, open-source AI agent framework written in Python. It
centers around a small agent loop that receives messages from chat channels,
invokes an LLM provider, executes tools, and manages session memory.

**This is an independent fork.** It is no longer a rebase target of upstream
`HKUDS/nanobot`. WebUI, the OpenAI-compatible API server, the WhatsApp bridge,
and most channels/providers have been removed to keep the surface small and the
maintenance honest. See [Fork philosophy](#fork-philosophy) below.

## Development Commands

```bash
# Python: run single test / lint (use uv, never pip)
uv run --extra dev python -m pytest tests/agent/test_memory_store.py -v
uv run ruff check nanobot/

# Gateway / interactive CLI
nanobot gateway          # channels + agent core
nanobot                  # interactive REPL
```

There is no WebUI, no `serve`/API command, and no bundled `web/` dist. The
`api`/`webui`/`serve` subcommands were removed in the decouple pass.

## High-Level Architecture

### Core Data Flow

Messages flow through an async `MessageBus` (`nanobot/bus/queue.py`) that
decouples chat channels from the agent core:

1. **Channels** (`nanobot/channels/`) receive messages and publish
   `InboundMessage` events to the bus.
2. **`AgentLoop`** (`nanobot/agent/loop.py`) consumes inbound messages, builds
   context, and coordinates the turn.
3. **`AgentRunner`** (`nanobot/agent/runner.py`) runs the multi-turn LLM
   conversation: send to provider, receive tool calls, execute tools, stream.
4. Responses are published as `OutboundMessage` events back to the channel.

### Key Subsystems

- **Agent Loop** (`nanobot/agent/loop.py`, `runner.py`): the core processing
  engine. `AgentLoop` owns session keys, hooks, and context building;
  `AgentRunner` executes the multi-turn conversation.
- **LLM Providers** (`nanobot/providers/`): built on a common base (`base.py`).
  Kept backends: Anthropic, OpenAI-compatible (`openai_compat_provider.py`),
  OpenAI Responses API (`openai_responses/`), OpenAI Codex (`openai_codex_provider.py`).
  `factory.py` and `registry.py` handle instantiation/model discovery;
  `fallback_provider.py` does failover. Image generation (`image_generation.py`)
  and audio transcription (`transcription.py`, `whisper/`) have their own sub-registries.
- **Channels** (`nanobot/channels/`): only Telegram, Discord, Email, and a minimal
  programmatic WebSocket channel remain. `manager.py`/`registry.py` discover and
  coordinate them via `pkgutil` scan + entry-point plugins.
- **Tools** (`nanobot/agent/tools/`): filesystem, shell (with sandbox backends),
  web search/fetch, MCP servers, cron, subagent spawning, sustained goals
  (`long_task.py`), image generation, CLI apps, and self-modification (`self.py`).
  Auto-discovered via `pkgutil` scan + entry-point plugins.
- **Memory** (`nanobot/agent/memory.py`): session history persistence into
  `memory/history.jsonl`, atomic fsync writes, single-phase Dream consolidation,
  and the **Consolidator** (token-budget archiving).
- **Session Management** (`nanobot/session/`): per-session history, context
  compaction, TTL-based auto-compaction (`manager.py`), sustained goal state
  (`goal_state.py`).
- **Config** (`nanobot/config/schema.py`, `loader.py`): Pydantic config loaded
  from `~/.nanobot/config.json`, camelCase aliases for JSON compatibility.
- **Command Router** (`nanobot/command/`): slash command routing + built-ins.
- **Heartbeat** (`nanobot/templates/HEARTBEAT.md`): periodic task list checked
  via `cron` jobs.
- **Pairing** (`nanobot/pairing/`): DM sender approval store with persistent
  pairing codes per channel.
- **Skills** (`nanobot/skills/`): built-in skill definitions (long-goal, cron,
  github, image-generation, memory, memory-defrag, my, skill-creator, ...) loaded
  into agent context.
- **Security** (`nanobot/security/`): PTH file guard and workspace boundaries,
  activated at CLI entry.

### Entry Points

- **CLI**: `nanobot/cli/commands.py`
- **Python SDK**: `nanobot/nanobot.py`

## Fork-Native Features

These are fork additions/forks-from-upstream, not part of stock HKUDS nanobot.

- **Dream safeguards (M1a–M1d).** Dream honors `dream.max_batch_size`,
  `dream.max_iterations` (Dream-specific, not the chat iteration budget),
  `dream.model_override`, `dream.timeout_s` (default 300s), plus the M1b
  limits `dream.max_changed_files` (default 8) and `dream.max_diff_chars`
  (default 32,000). Incomplete or timed-out Dream runs no longer auto-commit;
  `MemoryStore.run_dream` snapshots the memory surfaces before the run and
  rolls back byte-for-byte (timeout, exception, max iterations, or diff-limit
  violations), and logs model/limits/stop reason/changed files/diff size on
  every outcome. Remaining hardening (worktree-based Dream execution) is
  tracked in the TODO.
- **Session token usage tracking.** Each completed turn accumulates usage into
  `session.metadata["usage"]` with a per-provider/model breakdown
  (`usage["by_model"]["provider/model"]`). When a session is archived into
  `memory/history.jsonl`, the **delta since the last archive** is written into
  the conversation record's `usage` field — summing per `session_key` gives
  total archived usage; active (unarchived) usage stays in session metadata.
  `/new` archives the final unarchived delta and resets usage. Helpers live in
  `nanobot/utils/usage.py`.
- **Kept providers beyond the OpenCode-backed set:** `opencode_zen`
  (curated coding models) and `opencode_go` (low-cost coding models), both
  `openai_compat` gateways sharing `OPENCODE_API_KEY`.

## Fork Philosophy

This fork deliberately shed surface area to stay maintainable:

- **Removed:** WebUI (`webui/`, `nanobot/webui/`, `nanobot/web/`), the
  OpenAI-compatible API server (`nanobot/api/`), the WhatsApp bridge (`bridge/`),
  unused channels (Slack, Feishu, Matrix, QQ, WeChat, WeCom, DingTalk, MS Teams,
  Signal, MoChat, Napcat), and unused native provider backends (Azure, Bedrock,
  GitHub Copilot, plus many registry-only entries).
- **Kept channels:** Telegram, Discord, Email, WebSocket (programmatic/dev).
- **Kept providers:** `custom`, `openrouter`, `openai_codex`, `anthropic`,
  `openai`, `deepseek`, `dashscope`, `minimax`, `minimax_anthropic`, `moonshot`,
  `xiaomi_mimo`, `zai`, `zhipu`, plus the fork-added `opencode_zen`/`opencode_go`.
- **Do not re-merge upstream.** Upstream ships WebUI/API/channels this fork has
  intentionally dropped. Re-merging is conflict hell and undoes the decoupling.

See [`2026-06-16_decouple_TODO.md`](./2026-06-16_decouple_TODO.md) for the full
decouple record, decisions, and remaining work.

## Project-Specific Notes

- Architecture constraints: [`.agent/design.md`](.agent/design.md)
- Security boundaries: [`.agent/security.md`](.agent/security.md)
- Common gotchas: [`.agent/gotchas.md`](.agent/gotchas.md)

## Remote Layout & Workflow

- `origin` = `agbocsardi/nanobot` — **source of truth for this fork.**
- `main` tracks `origin/main`. It is the integration line.
- `upstream` = `HKUDS/nanobot` — **fetch-only reference.** Never merge or rebase
  onto it. Used only to read/cherry-pick useful fixes.

```
origin/main ─── feat/* topic branches (off main, merged back)
upstream/main ─ fetch-only, scar-raid source
```

### Normal flow

**Branch by size.** Match the git flow to the size of the change:

- **Trivial fixes → commit straight to `main`.** One-liners, typos, small bug
  fixes, doc tweaks, and other isolated single-purpose changes can be committed
  and pushed directly on `main`. This is the default for narrow, well-understood
  fixes (e.g. closing a one-line-issue).
- **Substantial work → topic branch off `main`.** Anything multi-file, behavioral,
  speculative, or that benefits from review/separation should go on a
  `feat/*` branch, merged back into `main` when ready:

```bash
git checkout main
git checkout -b feat/my-feature
# develop, commit, push
git checkout main
git merge feat/my-feature
git push origin main
```

When unsure which bucket a change falls into, default to a branch — branches are
cheap, a messy `main` history is not.

Before making substantial edits, check `git status`. If there are uncommitted
changes, ask whether to commit first so nothing gets lost in the diff.

### Stealing from upstream (scar-raiding)

Upstream is read-only reference. When it ships something worth taking:

```bash
git fetch upstream
git log --oneline upstream/main --since='2 weeks ago'   # scan for useful fixes
```

Cherry-pick or hand-port the *change*, not a merge. Prefer one faithful
`git cherry-pick -x <sha>` per upstream commit so attribution and revert stay
clean. When a cluster of commits depends on an earlier prerequisite upstream
commit that wasn't taken, fill it in explicitly. Targeted checks beat broad
lint during a port:

```bash
uv run ruff check <touched files>
uv run --extra dev python -m pytest <relevant test files> -q
```

### Conflict minimization

1. **Surgical diffs.** Change only the lines you need; don't reformat neighbors.
2. **New files > modified files.** They conflict less on rebase.
3. **Enable `rerere`:** `git config --global rerere.enabled true`.

## Server Deploy (`uhl`)

After `origin/main` is verified:

```bash
ssh uhl
cd ~/nanobot
git fetch origin
git checkout main
git reset --hard origin/main
```

The live install on `uhl` is editable, so changes are live after checkout/reset.
Restart any already-running Nanobot process to pick up imported Python changes.
**Run nanobot from a workspace directory, not from inside the repo** — otherwise
Python picks up the source tree instead of the installed version.

## Code Style

- Python 3.11+, asyncio throughout.
- Line length: 100.
- Linting: `ruff` with rules E, F, I, N, W (E501 ignored).
- pytest with `asyncio_mode = "auto"`.

## Common File Locations

- Config schema: `nanobot/config/schema.py`
- Provider base / new provider template: `nanobot/providers/base.py`
- Provider registry: `nanobot/providers/registry.py`
- Channel base / new channel template: `nanobot/channels/base.py`
- Tool registry: `nanobot/agent/tools/registry.py`
- Token-usage helpers: `nanobot/utils/usage.py`
- Tests mirror the `nanobot/` package structure.

## Verification

Targeted checks during an update are more useful than broad lint:

```bash
uv run ruff check nanobot/agent/memory.py nanobot/agent/context.py nanobot/config/schema.py
uv run --extra dev python -m pytest tests/agent/test_memory_store.py tests/agent/test_context_builder.py
uv run --extra dev python -m pytest tests/channels/test_base_channel.py
```

If STT changed, restart Nanobot and send a voice note — a successful
transcription is the real end-to-end check.
