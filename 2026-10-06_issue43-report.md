# Issue #43 investigation

Agent `issue43-luna-wt` (pane `wW:p1`), branch `feat/issue-43-reconnect`.

**Finding:** The reported lost-final-answer case is already handled; no runtime changes needed. `NanobotClient.handleOpen()` re-attaches known chats, while `ThreadShell` marks refresh pending on non-open status and calls `refreshCanonicalHistory()` on reopen. `useSessionHistory` requests canonical replay and reconciles the returned snapshot against live UI state.

The reconnect regression now exercises the actual scenario with a real `NanobotClient`: starts from a canonical user turn, receives correlated `goal_status` + partial `delta`, drops the socket, then reconnects, verifies attach + `attached` ACK, and receives canonical replay with two missed message rows, full `stream_end` text, and `turn_end`. It asserts each replay row/final answer appears once, the live partial is replaced, and Stop response disappears. Existing payload helper `canonicalThreadPayload` builds the initial canonical user event; replay adds canonical projection events directly.

- Earlier commits: `fe91d2cee` (real-client reconnect coverage), `7c912171e` (initial report).
- Follow-up: strengthened that regression and refreshed this report (commit recorded in branch history).
- Checks: focused Vitest (`thread-shell.test.tsx`, `nanobot-client.test.ts`) — 182 passed; WebUI TypeScript typecheck — passed; ESLint changed test — passed; `git diff --check` — passed.
- Limit: HTTP replay payload is simulated; no live gateway E2E. No runtime changes, issue42-owned files, or shared TODO changes.
