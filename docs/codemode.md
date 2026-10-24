# Python codemode

Codemode keeps MCP schemas out of every model request. The agent discovers relevant
MCP tools on demand, calls them from Python, and emits only the selected results.
Normal built-in tools stay available directly. MCP tools stay connected and registered;
their names/descriptions/schemas are **not** in the codemode tool description.

## Enable

Requires Linux, Python 3.11+, Bubblewrap **0.9.0 or newer**, and libseccomp. On Debian/Ubuntu:

```bash
sudo apt-get install bubblewrap libseccomp2
```

Merge this into `~/.nanobot/config.json`, then restart Nanobot:

```json
{
  "tools": {
    "codemode": {
      "enable": true,
      "bwrapPath": "/usr/bin/bwrap"
    }
  }
}
```

Disabled by default. Setting `enable` back to `false` restores direct MCP tool
schemas. There is no unsandboxed fallback: if the sandbox cannot start, codemode
returns an error and does not execute generated Python on the host. Enabling it
on an unsupported machine therefore leaves deferred MCP tools unavailable until
the sandbox is fixed or codemode is disabled.

### Binary selection and validation

`bwrapPath` defaults to `/usr/bin/bwrap` and must be absolute. PATH is **never**
searched, so login-shell/Linuxbrew PATH changes cannot silently change the sandbox.
Other installations require an explicit override. Symlinks are resolved to the
actual executable; diagnostics name that resolved path and its version.

Before launching each worker, a bounded, cancellable `--args`/`--version` probe
checks the executable, minimum version, and actual kernel AppArmor exec label.
On hosts with `apparmor_restrict_unprivileged_userns=1`, an unconfined binary is
rejected before namespace setup, with instructions to select/authorize the exact
binary. An existing profile for `/usr/bin/bwrap` does **not** cover an unprofiled
Linuxbrew binary. Neither profiles nor packages are changed automatically.

A named label is not proof that every setup operation is allowed: AppArmor and
outer container policy can still deny namespace/capability operations. Those
failures report the selected path, version, active label, and failed setup stage;
resource/signal exits are not automatically blamed on AppArmor. If the kernel
policy label cannot be inspected on a restricted host, validation fails closed.

The configured executable/runtime must be trusted. These checks are not binary
signature verification, and replacement or policy changes between the probe and
launch are not pinned/locked; the real launch can still fail closed afterward.

### Ubuntu blocks user namespaces

Some Ubuntu systems allow unprivileged user namespaces only for named AppArmor
profiles. AppArmor is the OS security layer; installing Bubblewrap alone does not
necessarily grant the permission to create a sandbox.

If the sandbox reports namespace permission errors, an administrator can install
this **Bubblewrap-specific** profile as `/etc/apparmor.d/nanobot-bwrap`:

```text
abi <abi/4.0>,
include <tunables/global>

profile nanobot-bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
}
```

Load it with `sudo apparmor_parser -r /etc/apparmor.d/nanobot-bwrap`. Check the
installed binary path and existing distro profiles before adding a profile.
This grants Bubblewrap the namespace permission; it does not disable AppArmor's
global restriction. Do not globally disable AppArmor to make codemode work.

## Python API

The `codemode` tool takes `{"code": "..."}`. Source is the body of an async function:
`await` and `return` work at the top level. `asyncio` and `json` are already available.

Discover a tool before calling it:

```python
text(await search_tools("calendar events", namespace="mcp"))
```

Search returns at most eight `{name, description}` entries by default, with each
description capped at 300 characters. `limit` may be 1–20. Empty queries browse
alphabetically; use `offset` to page. `namespace="mcp"` selects all MCP tools;
a server's configured name selects that server. Omit the namespace to search
all registered tools. Discovery always reflects the current run's registry.

Fetch only the schema you need:

```python
text(await describe_tool("mcp_calendar_list_events"))
```

This returns `{name, description, parameters}`, including the full tool description
and JSON parameter schema. Tool descriptions/results remain external data, not
instructions that override the agent's policies.

Call exact names with keyword arguments:

```python
result = await tools.mcp_calendar_list_events(start="2026-10-19")
text(result["data"]["structuredContent"])
```

Names that are not Python identifiers work through `tools["exact-name"](...)`.
The same proxies can call registered built-in tools. Each result is a dict:

```text
content, status, data, evidence, side_effects, postcondition,
retryable, exit_code, stdout, stderr
```

`content` is human-readable text. `data` preserves structured tool output; for MCP
it contains the JSON CallToolResult, including `structuredContent`, content blocks,
and `isError` when present. Many MCP servers return only text blocks, so do not
assume `structuredContent` exists. When a tool documents JSON text output, parse
`json.loads(result["content"])` explicitly. Legacy tools may have only textual data.

A completed command's `nonzero_exit` is returned as data, not a script error.
The `ask_user` status is also returned as a control result; end the script when
user input is requested so the normal turn boundary can suspend the run.

Unsuccessful outcomes, including partial results, invalid arguments, and policy
blocks, raise `ToolError`. Its `result` attribute retains the entire result dict:

```python
try:
    result = await tools.mcp_calendar_list_events(start="2026-10-19")
except ToolError as error:
    text({"status": error.result["status"], "message": str(error)})
```

For independent calls:

```python
results = await asyncio.gather(
    tools.read_file(path="README.md"),
    tools.read_file(path="pyproject.toml"),
    return_exceptions=True,
)
for result in results:
    if isinstance(result, Exception):
        text(str(result))
    else:
        text(result["content"][:1000])
```

Only `text(value)`, `print()`/stdout/stderr, and a non-None return enter the model's
transcript. Do not return the entire unfiltered MCP catalog or large responses:
filter or aggregate first. Oversized emitted output is saved using the normal tool
artifact mechanism, with a bounded preview and file reference.

## Awaitable read-only analysis (opt-in)

This is separate from `spawn`, which remains the full background action agent.
Enable `tools.codemode.workers.enable` alongside Code Mode, and explicitly set
`agents.defaults.runPresets.subagent` to a configured cheap preset (for example
`ds-flash`). Workers use that existing subagent provider/model snapshot, never an
implicit foreground model. Missing routing returns `status: unavailable`. SDK
users can supply `CodemodeTool(..., worker_snapshot_loader=...)` returning a
provider/model/context-window snapshot. No credentials are exposed to scripts.
There are no worker or `ask` provider retries or fallback-model substitutions.

**Choose the smallest path:** small text or a chunk the parent already sliced →
`ask(task, text)`; a large artifact the child must search/navigate →
`worker(task, artifacts=[id])`. `ask` makes exactly one provider call with no tools,
child registry, sandbox, recursion, or required artifacts. Its answer is the
`content` string in the same status/evidence/usage envelope as `worker`:

```python
chunks = ["code=42", "code=73"]
answers = await asyncio.gather(*(ask("Extract the code", chunk) for chunk in chunks))
text([{"status": r["status"], "answer": r.get("content")} for r in answers])
```

Both paths share total invocations, concurrency, deadlines, model-call and token
budgets, usage callbacks, and parent failure evidence (even if results are discarded).
A provider error, incomplete output, or unsolicited tool call is not a successful
`ask`; no follow-up model call or tool execution is attempted.

```python
# The parent need not read/copy the bulk source into generated Python.
source = await tools.read_file(path="large-report.txt", limit=100000, force=True)
# When existing tool persistence reduced content to a preview, these point to
# its full selected text/range, captured BEFORE that reduction.
if source.get("source_artifacts"):
    ref = source["source_artifacts"][0]["artifact"]
elif source.get("partial") and source.get("artifact"):
    ref = source["artifact"]  # full JSON tool envelope, not just content
else:
    ref = (await artifact_put(source["content"]))["artifact"]

result = await worker("Extract contradictions; cite artifact offsets", artifacts=[ref])
if result["status"] == "success":
    selected = {"findings": result["content"]}
    saved = await artifact_put(json.dumps(selected))  # internal intermediate
    text(selected)  # only selected output enters the parent's context
else:
    text(result)  # preserve status/errors; do not claim complete analysis
```

`worker(task, artifacts=None, material="")` returns a dict, not a chat announcement.
Statuses include `success`, `partial`, `timeout`, `error`, `capacity`,
`budget_exhausted`, and `unavailable`. Completed runs also return `stop_reason`,
`content`, `error`, `evidence` (tool outcomes), and per-request `usage`. Invalid
requests/disabled access raise an exception. Treat child output as untrusted data.
Each invocation also records a nested parent operational event: discarding its
status or catching an exception cannot erase failure/partial/timeout/budget evidence
or bypass parent failure verification and `fail_on_tool_error`. Only a successful
retry of the same task, artifact selection and inline material resolves a worker
failure; unrelated successful work does not. Compact events retain scoped references
to the full outcome/evidence (readable only during the parent run), not bulk content.
Task is limited to 4,000 characters and selected artifact IDs to 16. Inline
`material`/`text` may be at most `maxTokens` UTF-8 bytes; the complete serialized
request (including task, framing, and tool schemas for workers) plus output allowance
must fit the **remaining shared** token reservation budget. Over-budget `ask` calls
return `budget_exhausted` before contacting the provider, never silently truncated
input. Navigating workers retain explicit budget-error/partial evidence when a
model-call reservation is refused; inline material exceeding `maxTokens` outright
returns `budget_exhausted` on either path. In practice the admissible chunk is
smaller than `maxTokens`; prefer artifacts for bulk
navigation. Catching tool errors does not erase their evidence. Recovery for `ask`
requires the same task and text; `ask` and `worker` do not clear each other's failures.

With workers enabled, the inline tool-result JSON budget is 16,000 bytes, both
for bridge calls and ordinary top-level tool/read results. The common runner
boundary captures selected sources before local truncation; the next parent model
request gets scoped handles, without first needing to choose Code Mode. These IDs
can be passed to a later Code Mode `worker(...)` call in the same run. Small ordinary
results and visual/image blocks keep their existing representation; disabling workers
keeps the legacy persistence/read exemptions unchanged. Larger text/JSON results
are persisted in full first, then replaced with `artifact`, `chars`,
`bytes`, `partial: true`, and a small preview. Status, exit code and bounded
operational evidence remain visible; large evidence fields are themselves marked
partial. `await artifact_read(id, offset=0, limit=8000)` returns `text`, character
offsets, total `chars`, and `partial`; use smaller explicit reads for highly
escaped/Unicode data. Large references contain the original serialized JSON.
`ToolError.result` also retains status and the artifact reference on failures.

Artifacts are private disk-backed **parent-run-scoped** capabilities: IDs are not
paths and cannot be imported from another run/session. They survive disposable
Python processes within the run, then are deleted when the parent runner exits.
There is a hard 32 MiB/256-artifact aggregate storage budget; exhaustion fails explicitly,
not as apparently complete truncated data. Normal existing emitted-output
persistence/retention is unchanged. Built-in text/PDF/Office reads capture the
selected range before their existing character cap, with range markers where
applicable; explicit offset/limit/page selection still applies. This cannot recover
content an upstream server/extractor had already truncated before returning or
calling result persistence. Use `force=True` to avoid unchanged-file read stubs.

Each child gets a fresh registry containing only `artifact_read`, literal
`artifact_search` (at most 100 offsets), and its own `codemode`. It can read only
explicitly selected IDs plus its own harness-created result artifacts. Discovery
and exact-name dispatch both use that registry. No parent conversation, SOUL/USER,
memory, skills, arbitrary host-file tools, web, exec, writes, messaging, scheduling,
configuration, MCP, secrets, or delegation tools are included. File material must
first be selected through the parent's existing access controls. Model requests
and artifact mediation run in the **trusted host harness**; only generated Python
runs in the existing Bubblewrap isolation. The whole child is not a bwrap process.

Worker limits (`tools.codemode.workers`, camelCase or snake_case):

| Field | Default | Scope |
| --- | --- | --- |
| `enable` | `false` | Separate opt-in; disabled behavior unchanged |
| `timeoutS` | `45` | Each worker/ask including slot waits, also capped by remaining parent script lifetime |
| `maxWorkers` | `8` | Total worker + ask invocations per parent runner run (1–32) |
| `maxConcurrency` | `4` | Simultaneous worker + ask calls per run (1–8); excess calls wait |
| `globalMaxConcurrency` | `8` | Process-wide child cap (1–8), at least `maxConcurrency` |
| `maxModelCalls` | `8` | Shared across all children in that run (1–128) |
| `maxTokens` | `64000` | Shared conservative input UTF-8-byte + output-token reservations (4000–1000000) |
| `outputTokens` | `1024` | Maximum requested output tokens per model call |
| `maxIterations` | `8` | Each navigating child's runner (1–16); no finalization bonus call; ask always one call |
| `maxToolCalls` | `64` | Shared tool/bridge/discovery attempts (nested dispatch can charge twice) |

The default per-run concurrency is 4 and process-wide child cap is 8, with 8
separate reserved child Python sandbox slots (parent sandbox cap stays 2).
The invariant is **per-run concurrency ≤ global child cap ≤ child sandbox slots**;
schema validation enforces it. All admitted navigating children can hold a sandbox
simultaneously; awaiting parents do not occupy child slots, and children cannot
recursively delegate. `ask` shares the child cap but needs no sandbox. If simultaneous
SDK runs specify different global caps, admission respects the stricter cap of the
requesting and currently active runs. Parent Code Mode's own `maxConcurrency` also
bounds bridge requests: raise it alongside workers' concurrency to use more than 4.
Total invocations are charged before queueing; two four-call waves fit the default
8-invocation budget, but multiple-call navigation may need a larger shared model
budget. `capacity` is not a normal fan-out result: calls wait for slots until their
request/parent deadline or cancellation instead.

Children's Python scripts have a maximum 20s deadline; parents keep their existing
60s default including all awaited work. Cancellation propagates to model requests,
tools, queued calls, and subprocess cleanup, releasing slots/counters on every path.
There is no timeout removal or persistent REPL. Providers may report hidden token
usage exceeding a reservation; it is recorded and prevents subsequent over-budget calls. Remote providers must honor
requested output limits; this is not a remote billing guarantee.

`AgentRunResult.usage` remains foreground-only for compatibility; `worker_usage`
contains separately attributed child requests. AgentLoop records each completed
child response immediately into parent session accounting (also before later
cancellation), exactly once; `last_usage` includes both. Cron records retain child
usage separately. SDK callers can use `AgentRunSpec.worker_usage_callback` for
incremental accounting. Cancelled in-flight requests with no provider usage reply
cannot have their remote token consumption measured. Fake-provider and local
sandbox checks are not a production effectiveness claim. No rollout is automatic;
future activation requires config changes and a process restart.

## Execution and security

- One fresh Bubblewrap process per script, **not per nested tool call**. Existing
  Python tools and MCP connections remain in the parent. No persistent interpreter
  state, installed packages, or writable files survive to another script.
- The worker sees read-only system/Python runtime files and its worker module,
  not the workspace, home contents, config, credentials, or host sockets. Its
  environment is replaced and its network/PID/user/IPC/UTS namespaces are isolated.
  Bubblewrap setup temporarily retains `CAP_NET_ADMIN` for modern versions' loopback
  initialization and `CAP_SETPCAP` so the trusted worker bootstrap can drop the
  entire bounding set. Before compiling generated code, all bounding, permitted,
  effective, inheritable, and ambient capabilities are cleared, with `no_new_privs`
  enforced. They cannot return through exec. Failure to drop them aborts startup.
  A 16 MiB private `/tmp` is the only writable tree.
- Seccomp denies new processes/threads and sensitive kernel operations. Hard
  process limits cap address space, CPU, file size, and open descriptors. This
  prevents forking around the per-process memory limit. This is a shared-kernel
  Linux sandbox, not a VM or a guarantee against kernel vulnerabilities.
- Imports and ordinary Python code are allowed inside that boundary. Restricted
  builtins or Python AST filtering are **not** the security mechanism.
- Every nested call goes through the runner's existing validation, policy/approval,
  repeated-lookup/workspace safeguards, and receipt path, using the current request
  context. The orchestrator itself is non-mutating; inner operations are checked
  individually, including in audit/exploration mode.
- Calling a tool intentionally grants that tool's existing powers. For example,
  an allowed parent-side shell tool still has its configured shell access. The
  worker sandbox does not strengthen the permissions of parent-side tools.
- Safe calls overlap up to `maxConcurrency` when the run permits concurrent tools.
  Writes/unknown tools are ordering barriers. MCP tools are treated conservatively
  unless their server declares `readOnlyHint: true`.
- Nested calls have stable child IDs and compact operational events, but are not
  inserted as separate transcript tool results. Event targets remain aligned for
  same-target/cross-tool recovery and verification. Catching an error cannot erase
  its recorded outcome or bypass fail-on-tool-error behavior; recovered failures
  and rejected arguments do not become synthetic failures on the outer script.
- Completed side effects are **not rolled back** on failure. On script completion,
  failure, deadline, or cancellation, the worker is killed/reaped and pending
  calls receive asyncio cancellation. A parent-side tool that suppresses
  cancellation can delay cleanup; remote effects may already have landed.
  Cancellation of a dispatched mutation records an unknown
  receipt outcome, not confirmed failure or success. Existing MCP transport
  reconnect/retry behavior still applies; receipts do not provide remote exactly-once
  execution guarantees.
- At most two parent Python sandboxes are admitted per Nanobot process, including
  shutdown. With read-only analysis enabled, eight additional child sandbox slots
  are reserved, at least the configurable global child cap, so awaiting parents
  cannot starve their children. Parent sandbox overflow still fails immediately;
  worker/ask fan-out queues within its deadlines. A Python script cannot call
  `tools.codemode` recursively; a separate analysis worker using its own restricted Code Mode is supported.

There is no persistent `store` API, package installation, direct model API, or
inline image emission in this initial implementation. Existing parent-side tools
remain available through the bridge.

## Limits

JSON uses camelCase aliases; snake_case is also accepted.

| `tools.codemode` field | Default | Meaning |
| --- | --- | --- |
| `enable` | `false` | Enable codemode and defer MCP schemas |
| `bwrapPath` | `/usr/bin/bwrap` | Explicit absolute Bubblewrap executable; never resolved from PATH |
| `timeoutS` | `60` | Whole-script deadline, including startup/tool waits (1–600s) |
| `memoryMb` | `256` | Worker address-space limit (64–2048 MiB) |
| `maxOutputChars` | `16000` | Emitted output threshold for artifact offloading |
| `maxToolCalls` | `64` | Total discovery and tool requests (1–256) |
| `maxConcurrency` | `4` | Maximum overlapping safe requests (1–16) |

Additional hard limits: 65,536 source characters, 1 MiB emitted UTF-8 output,
10,000 protocol messages, 4 MiB per bridge frame, 8 MiB total request frames,
64 KiB raw worker diagnostics, and 8 MiB per temporary file. Output/request floods
are rejected in the parent and cannot be bypassed by catching exceptions in Python.

## Verification reference

`tests/agent/tools/test_codemode.py` adapts relevant behavioral checks from Pi's
[codemode sandbox tests](https://github.com/earendil-works/pi/blob/e86b054753b544600693384d3db4e3281ba86b60/packages/codemode/test/sandbox.test.ts),
[worker admission tests](https://github.com/earendil-works/pi/blob/e86b054753b544600693384d3db4e3281ba86b60/packages/coding-agent/test/codemode-worker-admission.test.ts),
and [session integration tests](https://github.com/earendil-works/pi/blob/e86b054753b544600693384d3db4e3281ba86b60/packages/coding-agent/test/suite/agent-session-codemode.test.ts),
plus Python-specific OS-isolation, policy, receipts, and deferred-MCP checks.

```bash
scripts/run-tests.sh tests/agent/tools/test_codemode.py -q
```

The real sandbox checks skip on unsupported hosts or when OS policy blocks user
namespaces. Pure discovery/config/fail-closed checks do not require Bubblewrap.
Binary/version/profile/argv validation has focused coverage in
`tests/agent/tools/test_codemode_sandbox.py`; the real-worker tests check that no
setup capabilities remain, including after an exec.
