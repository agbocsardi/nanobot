# Python codemode

Codemode keeps MCP schemas out of every model request. The agent discovers relevant
MCP tools on demand, calls them from Python, and emits only the selected results.
Normal built-in tools stay available directly. MCP tools stay connected and registered;
their names/descriptions/schemas are **not** in the codemode tool description.

## Enable

Requires Linux, Python 3.11+, Bubblewrap, and libseccomp. On Debian/Ubuntu:

```bash
sudo apt-get install bubblewrap libseccomp2
```

Merge this into `~/.nanobot/config.json`, then restart Nanobot:

```json
{
  "tools": {
    "codemode": {
      "enable": true
    }
  }
}
```

Disabled by default. Setting `enable` back to `false` restores direct MCP tool
schemas. There is no unsandboxed fallback: if the sandbox cannot start, codemode
returns an error and does not execute generated Python on the host. Enabling it
on an unsupported machine therefore leaves deferred MCP tools unavailable until
the sandbox is fixed or codemode is disabled.

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

## Execution and security

- One fresh Bubblewrap process per script, **not per nested tool call**. Existing
  Python tools and MCP connections remain in the parent. No persistent interpreter
  state, installed packages, or writable files survive to another script.
- The worker sees read-only system/Python runtime files and its worker module,
  not the workspace, home contents, config, credentials, or host sockets. Its
  environment is replaced, its network/PID/user/IPC/UTS namespaces are isolated,
  and capabilities are dropped. A 16 MiB private `/tmp` is the only writable tree.
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
- At most two workers are admitted per Nanobot process, including shutdown. Excess
  calls fail immediately. Codemode cannot call itself.

There is no persistent `store` API, package installation, direct model API, or
inline image emission in this initial implementation. Existing parent-side tools
remain available through the bridge.

## Limits

JSON uses camelCase aliases; snake_case is also accepted.

| `tools.codemode` field | Default | Meaning |
| --- | --- | --- |
| `enable` | `false` | Enable codemode and defer MCP schemas |
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
