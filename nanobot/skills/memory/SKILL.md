---
name: memory
description: Hierarchical git-backed memory — agent-managed topic files, Dream-managed system files.
always: true
---

# Memory

## Structure

- `SOUL.md` — Bot personality and communication style. **Managed by Dream.** Do NOT edit.
- `USER.md` — User profile and preferences. **Managed by Dream.** Do NOT edit.
- `memory/system/*.md` — Pinned memory, always loaded in full. **Curated by Dream** (it promotes/demotes content between system/ and topic files). Do NOT edit.
- `memory/MEMORY.md` — Legacy long-term facts. **Managed by Dream.** Do NOT edit.
- `memory/**/*.md` (everything else) — **Topic files. Yours to create and edit.** Lazy-loaded: only their descriptions appear in the Memory Tree; read them on demand.
- `memory/history.jsonl` — append-only JSONL, not loaded into context. Prefer the built-in `grep` tool to search it.

## Topic files — your memory, manage it

When you learn something durable mid-conversation (a decision, a project fact, a gotcha, infrastructure detail), write it to a topic file **while context is hot** — don't wait for Dream to reconstruct it from history.

- Organize by subject: `memory/projects/<name>.md`, `memory/people/<name>.md`, `memory/infra/<host>.md`, etc. Create new files/folders freely; prefer many small focused files over one large one.
- Every topic file MUST start with YAML frontmatter containing a one-line `description:` — this is what appears in the Memory Tree and is your only navigational signal later. Keep it specific.

```markdown
---
description: Fork maintenance for nanobot — branch list, sync workflow, deploy host
---

# Nanobot fork
...
```

- Update in place; replace stale facts rather than appending contradictions.
- The Memory Tree in your system prompt lists every file with its description. Read a topic file before answering questions it likely covers.

### Committing

The workspace is a git repo. After creating or editing memory files, commit with an informative message:

```bash
git add memory/ && git commit -m "memory: <what you learned and why it changed>"
```

If the commit fails (e.g. git unavailable), don't worry — Dream auto-commits leftovers on its next run.

## Search Past Events

`memory/history.jsonl` is JSONL format — each line is a JSON object with `cursor`, `timestamp`, `content`.

- For broad searches, start with `grep(..., path="memory", glob="*.jsonl", output_mode="count")` or the default `files_with_matches` mode before expanding to full content
- Use `output_mode="content"` plus `context_before` / `context_after` when you need the exact matching lines
- Use `fixed_strings=true` for literal timestamps or JSON fragments
- Use `head_limit` / `offset` to page through long histories
- Use `exec` only as a last-resort fallback when the built-in search cannot express what you need

Examples (replace `keyword`):
- `grep(pattern="keyword", path="memory/history.jsonl", case_insensitive=true)`
- `grep(pattern="2026-04-02 10:00", path="memory/history.jsonl", fixed_strings=true)`
- `grep(pattern="keyword", path="memory", glob="*.jsonl", output_mode="count", case_insensitive=true)`
- `grep(pattern="oauth|token", path="memory", glob="*.jsonl", output_mode="content", case_insensitive=true)`

## Important

- **Do NOT edit SOUL.md, USER.md, or memory/system/.** They are automatically managed by Dream.
- **DO edit topic files** — they are your working memory. If you notice outdated information in a topic file, fix it immediately.
- If you notice outdated information in Dream-managed files, it will be corrected when Dream runs next.
- Users can view Dream's activity with the `/dream-log` command.
- When memory feels disorganized, suggest the user run the `memory-defrag` skill.
