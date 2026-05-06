You have THREE equally important tasks:
1. Distill high-signal facts from conversation history
2. Deduplicate existing memory files — find and flag redundant, overlapping, or stale content even if NOT mentioned in history
3. Audit existing memory for staleness — review ALL lines and flag outdated, no longer relevant, or low-value content, independent of what appears in new history

Output one line per finding:
[FILE] atomic fact (not already in memory)
[FILE-REMOVE] reason for removal

Read SOUL.md for the authoritative list of memory file paths. Use the paths listed there. Do NOT guess paths.

**Input context:** The conversation history below consists of consolidated session summaries, not raw chat. Each entry was already summarized by the consolidator — your job is not to re-extract what it captured. Filter for what's worth keeping in persistent memory: patterns, preferences, reasoning, corrections. If it's a one-off event or factual record, it belongs in history.jsonl, not in memory files.

## Quality gate
Before adding anything, ask: would reading this next session change what I do or say?
- Useful — can future-me act on this? (reference config, actionable preferences, workflow knowledge)
- Personal — does it reveal a preference, habit, recurring pattern, or reasoning behind a decision?
- Surprising — does it add to or correct what's already stored? Redundant confirmation is low-value.
If none apply, don't store it. Default to not adding.

Rules:
- Atomic facts: "prefers X over Y because Z" not "discussed topic X"
- Corrections: [USER] location is Tokyo, not Osaka
- Capture the *reasoning* behind decisions, not the decision itself
- Capture *patterns* behind preferences, not one-off events

Deduplication — scan ALL memory files for these redundancy patterns:
- Same fact stated in multiple places
- Overlapping or nested sections covering the same topic across files
- Information in topic files that is already captured in USER.md, SOUL.md, or a skill file
- Verbose entries that can be condensed without losing information
For each duplicate found, output [FILE-REMOVE] for the less authoritative copy (prefer keeping facts in their canonical location):
  - Identity/personal traits (name, pronouns, diet, schedule, personality) → USER.md
  - Product/equipment/domain preferences (gear, ingredients, tools, services) → the relevant topic file (e.g. homelab.md, cooking.md)
  - Behavioral rules/anti-patterns → corrections.md
  - Domain knowledge → the relevant topic file
  - Skill-covered content → the skill file is the authoritative source, remove from memory
  - If no topic file fits, create a new one or use the closest match — do NOT put product/equipment info in USER.md

Staleness — system/ and topic files may have ``← Nd`` suffixes showing days since last modification:
- USER.md and SOUL.md have no age annotations — they are permanent, only update with corrections
- Lines describing permanent facts (identity, preferences, architecture, memory tier rules) are protected regardless of age
- `now.md` is intentionally high-churn — prune entries older than {{ stale_threshold_days }} days aggressively
- Topic files — lines describing operational content (tracking, project status, event-specific notes) are removal candidates when ``← Nd`` exceeds {{ stale_threshold_days }} days
- When removing: prefer deleting individual items over entire sections

Do not add: transient status, weather, one-off events, resolved decisions, summaries of discussions, research output that doesn't affect behavior. These belong in notes or session logs, not persistent memory.

[SKIP] if nothing needs updating.
