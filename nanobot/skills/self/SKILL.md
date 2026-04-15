---
name: self
description: Inspect and adjust the agent's own runtime state (model, iterations, context window, token usage, web config). Use when diagnosing why something doesn't work ("why can't you search the web?", "why did you stop?"), checking resource limits before complex tasks, adapting configuration for long or simple tasks, or remembering user preferences across turns. Also use when the user asks what model you are running, how many tokens you've used, or what your settings are.
always: true
---

# Self-Awareness

## How to use

1. **Identify the situation** from the categories below
2. **Call the self tool** with the appropriate action
3. **If modify**, warn the user before changing impactful settings (model, iterations)
4. **For detailed examples**, read [references/examples.md](references/examples.md)

## When to inspect

<rule>
**Diagnose before explaining.** When something doesn't work, inspect your state first.
</rule>

<rule>
**Check budget before complex tasks.** Know your limits before committing.
</rule>

<rule>
**Recall across turns.** Store preferences in runtime vars, read them back later.
</rule>

## When to modify

<rule>
**Only modify when benefit is clear and user is informed.** Warn before changing model.
</rule>

| Situation | Command |
|-----------|---------|
| Large codebase analysis | `self(action="modify", key="context_window_tokens", value=131072)` |
| Repetitive simple tasks | `self(action="modify", key="model", value="<fast-model>")` |
| Long multi-step task | `self(action="modify", key="max_iterations", value=80)` |

**Tradeoff:** Bias toward stability. Only modify when defaults are genuinely insufficient.

## Anti-patterns

<rule>
**Don't inspect every turn.** Costs a tool call. Use when you need information, not reflexively.
</rule>

<rule>
**Don't store sensitive data.** No API keys, passwords, or tokens in runtime vars.
</rule>

<rule>
**Don't modify workspace.** Does not update file tool boundaries — won't work.
</rule>

## Constraints

- All modifications in-memory only — restart resets everything
- Protected params have type/range validation: `max_iterations` (1–100), `context_window_tokens` (4096–1M), `model` (non-empty str)
- If `self_modify` is false, inspect only

## Related tools

| Need | Use | Persists? |
|------|-----|-----------|
| Per-session temp state | `self(action="modify", key="...", value=...)` | No |
| Long-term facts | Memory skill (`MEMORY.md`, `USER.md`) | Yes |
| Permanent config change | Edit config file | Yes |

**Rule of thumb:** Tomorrow? Memory. This turn only? Self.
