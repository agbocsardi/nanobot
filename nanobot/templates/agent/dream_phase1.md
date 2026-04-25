You have THREE equally important tasks:
1. Extract new facts from conversation history
2. Deduplicate existing memory files — find and flag redundant, overlapping, or stale content even if NOT mentioned in history
3. Audit existing memory for staleness — review ALL lines and flag outdated, no longer relevant, or low-value content, independent of what appears in new history

Output one line per finding:
[FILE] atomic fact (not already in memory)
[FILE-REMOVE] reason for removal

Files: USER (identity, preferences), SOUL (bot behavior, tone), MEMORY (knowledge, project context)

Rules:
- Atomic facts: "has a cat named Luna" not "discussed pet care"
- Corrections: [USER] location is Tokyo, not Osaka
- Capture confirmed approaches the user validated

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

Do not add: current weather, transient status, temporary errors, conversational filler.

[SKIP] if nothing needs updating.
