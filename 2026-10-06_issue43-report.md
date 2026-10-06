# Issue #43 investigation

Agent `issue43-luna-wt` (pane `wW:p1`), branch `feat/issue-43-reconnect`.

**Finding:** The reported lost-final-answer case is already handled; no runtime changes needed. `NanobotClient.handleOpen()` re-attaches known chats, while `ThreadShell` marks refresh pending on non-open status and calls `refreshCanonicalHistory()` on reopen. This triggers `useSessionHistory` to request canonical replay (conditional revision/cache), which projects and reconciles the server snapshot with in-flight UI state. Existing ThreadShell regressions cover truncated-answer recovery, reconnect before first delta, canonical completion, and active resumed turns.

Added a focused real-`NanobotClient`/`ThreadShell` regression that drops and reconnects the socket, verifies re-attach and replay fetch, then confirms the canonical final answer replaces the stale partial answer. This closes the test gap where reconnect behavior was previously simulated with a mock status emitter.

- Test commit: `fe91d2cee` (`test(webui): cover reconnect recovery with real client`).
- Checks: Vitest `thread-shell.test.tsx` + `nanobot-client.test.ts` — 182 passed; WebUI TypeScript build typecheck — passed; ESLint changed test — passed; `git diff --check` — passed.
- Limits: fetch/server behavior is simulated; no live gateway end-to-end run. No issue42-owned files or shared TODO changed.
