---
name: memory-defrag
description: Reorganize the memory filesystem into a clean hierarchy of focused topic files. Run on demand or via cron when memory becomes disorganized.
---

# Memory Defragmentation

Over long-horizon use, memory files sprawl: duplicates accumulate, files grow too large, descriptions go stale. This skill reorganizes the memory repository into a clean hierarchy. Run it when the user asks, or when the Memory Tree shows obvious disorder (overlapping files, missing descriptions, files >200 lines).

## Safety first

1. The workspace is a git repo — verify a clean slate before starting:
   ```bash
   git add memory/ && git commit -m "memory: checkpoint before defrag" || true
   ```
2. Never touch `SOUL.md`, `USER.md`, `memory/system/`, `memory/history.jsonl`, or `memory/.dream_cursor`. Only topic files (`memory/**/*.md` outside `system/`, plus legacy `memory/MEMORY.md`) are in scope.

## Procedure

Spawn a subagent for the heavy lifting (or do it directly for small memory sets):

1. **Inventory.** Read every in-scope file. Note size, frontmatter description, and topic overlap.
2. **Plan a target hierarchy** of roughly 15–25 focused files grouped by subject, e.g.:
   ```
   memory/projects/<name>.md
   memory/people/<name>.md
   memory/infra/<host-or-service>.md
   memory/decisions/<area>.md
   ```
3. **Reorganize:**
   - Split files covering multiple subjects.
   - Merge files covering the same subject; keep the most complete phrasing of each fact, once.
   - Delete stale content: resolved incidents, superseded decisions, ephemeral task state.
   - Migrate anything still in legacy `memory/MEMORY.md` into topic files, leaving it empty.
   - Give every file YAML frontmatter with a specific one-line `description:`.
4. **Verify:** every file has a description; no fact appears twice; no file mixes unrelated subjects.
5. **Commit** with a summary of the restructuring:
   ```bash
   git add -A memory/ && git commit -m "memory: defrag — <files merged/split/deleted summary>"
   ```

## Rollback

If the result looks wrong, the checkpoint commit makes recovery trivial:

```bash
git log --oneline -5          # find the checkpoint
git revert --no-edit <sha>..HEAD
```

## Report

Tell the user what changed: files created/merged/deleted, facts pruned, and the commit SHA.
