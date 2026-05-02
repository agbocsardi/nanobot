You have THREE equally important tasks:
1. Distill high-signal facts from conversation history
2. Deduplicate existing memory files — find and flag redundant, overlapping, or stale content even if NOT mentioned in history
3. Audit existing memory for staleness — review ALL lines and flag outdated, no longer relevant, or low-value content, independent of what appears in new history

Output one line per finding:
[FILE] atomic fact (not already in memory)
[FILE-REMOVE] reason for removal

Files: USER (identity, preferences), SOUL (bot behavior, tone), MEMORY (knowledge, project context)

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
- Same fact stated in multiple places (e.g., "communicates in Chinese" in both USER.md and multiple MEMORY.md entries)
- Overlapping or nested sections covering the same topic
- Information in MEMORY.md that is already captured in USER.md or SOUL.md (MEMORY.md should not duplicate permanent-file content)
- Verbose entries that can be condensed without losing information
For each duplicate found, output [FILE-REMOVE] for the less authoritative copy (prefer keeping facts in their canonical location)
- Content that describes workflows, procedures, recipes, or domain-specific knowledge that has a corresponding skill is redundant in MEMORY.md — the skill file is the authoritative source. Flag for removal.

Staleness — MEMORY.md lines have a ``← Nd`` suffix showing days since last modification:
- SOUL.md and USER.md have no age annotations — they are permanent, only update with corrections
- Lines describing permanent facts (identity, preferences, relationships, architecture) are protected regardless of age
- Lines describing operational content (tracking, project status, bug lists, event-specific notes) are removal candidates when ``← Nd`` exceeds {{ stale_threshold_days }} days
- When removing: prefer deleting individual items over entire sections

Do not add: transient status, weather, one-off events, resolved decisions, summaries of discussions, research output that doesn't affect behavior. These belong in notes or session logs, not persistent memory.

[SKIP] if nothing needs updating.
