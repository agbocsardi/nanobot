# Tools & skills removal audit (decouple TODO #16)

Date: 2026-09-02 · Base: `main` @ `d203bf19` · Method: static reference scan
(`grep -rI` over `nanobot/`, `tests/`, `docs/`, `AGENTS.md`), plus loader
inspection. Heuristic only — counts = files containing the bare name.

## Discovery model (why static refs understate usage)

- **Tools:** `ToolLoader` discovers every module under `nanobot/agent/tools/`
  via `pkgutil.iter_modules`, skipping only `_*` and
  `_SKIP_MODULES = {base, schema, registry, context, loader, config,
  file_state, sandbox, mcp, __init__, runtime_state}`. Every other module is
  registered by construction, so **no tool is dead code by construction**;
  "unused" tools can still be called by the agent at runtime.
- **Skills:** `SkillLibrary` loads every `skills/<name>/SKILL.md` unless
  disabled or requirements-unmet. Same conclusion — all 14 built-in skills are
  registered.

## Tools — reference counts

| tool | nanobot/ | tests/ | notes / verdict |
|---|---|---|---|
| message | 73 | 115 | core; keep |
| self | 80 | 105 | core (`my`); keep |
| web | 28 | 32 | keep |
| cron | 21 | 35 | keep |
| search | 20 | 21 | keep |
| mcp | 14 | 14 | keep |
| shell | 14 | 19 | keep |
| filesystem | 11 | 14 | keep |
| approval | 10 | 3 | new (#22); keep |
| sandbox | 10 | 4 | infra; keep |
| image_generation | 8 | 4 | keep |
| file_state | 8 | 5 | infra; keep |
| spawn | 5 | 12 | keep |
| apply_patch | 4 | 7 | keep |
| cli_apps | 3 | 4 | keep |
| exec_session | 3 | 4 | keep |
| reaction | 3 | 2 | keep |
| memory_search | 3 | 1 | new (#1); keep |
| memory_read | 2 | 1 | new (Phase A); keep |
| runtime_inspector | 1 | 2 | new; keep |
| memory_write | 1 | 1 | new (Phase A); keep |
| mcp_oauth | 1 | 1 | keep (MCP auth) |
| discord_history | 1 | 1 | thin refs; verify it is exercised before pruning |

**Verdict:** no tool removal recommended. Everything is auto-discovered and
referenced; the thin new tools (memory_read/write, runtime_inspector,
approval) are deliberately young features. `discord_history` has the thinnest
trace — keep unless Gergő confirms it is unused.

## Skills — reference counts

| skill | nanobot/ | tests/ | docs/AGENTS | notes / verdict |
|---|---|---|---|---|
| memory | 357 | 336 | 66 | core; keep |
| cron | 349 | 437 | 30 | core; keep |
| my | 60 | 83 | 62 | core; keep |
| summarize | 28 | 32 | 5 | keep |
| github | 22 | 22 | 40 | keep |
| weather | 8 | 23 | 2 | keep |
| clawhub | 8 | 0 | 0 | no tests/docs; agent-invoked only — review |
| tmux | 27 | 0 | 0 | agent-invoked only; review |
| long-goal | 5 | 0 | 1 | review |
| image-generation | 2 | 0 | 7 | docs-linked; review |
| memory-defrag | 2 | 0 | 1 | review |
| update-setup | 2 | 0 | 0 | weakest case; likely prune if unused |
| skill-creator | 4 | 5 | 1 | keep |
| dream (system) | n/a | n/a | n/a | system-managed; keep |

**Recommendation:** do not remove skills yet — several are invoked by the
agent at runtime with zero static refs (tmux, clawhub). Mark `update-setup`
and `long-goal` as **prune candidates pending Gergő confirmation**; add
doc/test coverage notes for tmux/clawhub if they stay.

## Follow-ups (next session, if Gergő wants)

1. Confirm real usage for `discord_history`, `tmux`, `clawhub`,
   `update-setup`, `long-goal` (check live workspace skills + session logs).
2. If pruning is approved: remove the module/SKILL.md, update config schema
   hints/disabled defaults, and add a loader test asserting the removal.
3. Consider moving `memory_read`/`memory_write`/`approval` docs entries from
   the thin-list into the fork feature reference.

## Live usage evidence (2026-09-02, read-only inspection)

- Main bot workspace `~/.nanobot/workspace/skills`: **48 custom skills**
  (add-to-lists, biometrics, cinema-scout, daily-reflection, vault, voice,
  weekly-*, wiki-nearby, …). These are workspace skills in addition to the 14
  built-ins — the built-in list above understates real usage.
- Szárszó bot workspace `~/.nanobot-szarszo/workspace/skills`: discourse-diary,
  memory.
- Implication: built-in skills with zero static refs (tmux, clawhub,
  long-goal, update-setup, memory-defrag) may still be invoked by the agent
  via tool/context hints; **do not prune without checking session usage**.
  Prune candidates stay: update-setup, long-goal (weakest traces).
