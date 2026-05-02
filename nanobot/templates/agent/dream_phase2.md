Update memory files based on the analysis below.
- [FILE] entries: add the described content to the appropriate file
- [FILE-REMOVE] entries: delete the corresponding content from memory files

## File paths (relative to workspace root)

Read SOUL.md for the authoritative list of memory file paths and their purposes. Edit only the files listed there.

Do NOT guess paths. Prefer editing system/ files over topic files for high-signal updates.

## Editing rules
- Edit directly — file contents provided below, no read_file needed
- Use exact text as old_text, include surrounding blank lines for unique match
- Batch changes to the same file into one edit_file call
- For deletions: section header + all bullets as old_text, new_text empty
- Surgical edits only — never rewrite entire files
- If nothing to update, stop without calling tools
- Cross-check [FILE-REMOVE] candidates against existing skills listed below. When you suspect content is duplicated in a skill, use read_file on the relevant skill to verify before removing — the skill is the authoritative source.

## Quality
- Every line must carry standalone value
- Concise bullets under clear headers
- When reducing (not deleting): keep essential facts, drop verbose details
- If uncertain whether to delete, keep but add "(verify currency)"
