# Tool Policies

Declarative, ordered rules control which tool calls are allowed, denied, or
require interactive approval. The policy engine lives in
`nanobot/agent/tools/policy.py` and is enforced by the tool registry before any
tool executes.

## Rule shape

Rules are configured under `tools.policies` in `~/.nanobot/config.json`
(camelCase) or `config.yaml`. Later matching rules take precedence, so order
matters — put broad rules first and narrow carve-outs last.

```json
{
  "tools": {
    "policies": [
      {
        "id": "audit-readonly",
        "outcome": "deny",
        "mode": "audit",
        "mutation": "write",
        "reason": "audit mode is read-only"
      },
      {
        "id": "deploy",
        "outcome": "ask",
        "tool": "exec",
        "mode": "foreground",
        "reason": "deploys need your sign-off"
      }
    ]
  }
}
```

Fields:

| Field | Default | Meaning |
|---|---|---|
| `id` | – (required) | Rule identifier; also the identifier used for approvals |
| `outcome` | – (required) | `allow`, `deny` or `ask` |
| `mode` | `*` | Interaction mode (fnmatch): `audit`, `exploration`, `foreground`, `cron`, `heartbeat`, `delegated`, or `*` |
| `tool` | `*` | Tool name or glob, e.g. `exec`, `write_*` |
| `resource` | `*` | Resource glob. Paths are normalized to absolute paths; URLs match the bare URL and `host:…`; other keys match `key:value` (e.g. `repo:upstream/*`) |
| `mutation` | `*` | `read`, `write`, or `*` (write = any non-read-only tool) |
| `model` | `*` | Model name glob (fnmatch) |
| `preset` | `*` | Model preset name glob (fnmatch) |
| `reason` | `""` | Shown in the policy block message |

The last matching rule wins. If no rule matches, the call is allowed
(unchanged default behavior).

## Interaction modes

Every agent run carries a deterministic interaction mode that policy rules can
match. Entry points stamp it onto the request:

| Mode | Set by | Notes |
|---|---|---|
| `foreground` | chat channels and the interactive CLI | default for live turns |
| `cron` | scheduled cron turns (bound and isolated) | |
| `heartbeat` | the built-in heartbeat job | |
| `delegated` | subagent launches | autonomous; no approval surface |
| `audit` | `process_direct(metadata={"interaction_mode": "audit"})` / SDK `run(mode="audit")` | read-only enforced when enabled |
| `exploration` | same surfaces with `mode="exploration"` | mutation ban when enabled |

Modes are matched via fnmatch, so `mode="cron*"`, `mode="foreground|audit"` is
not supported (one glob per rule) — use two rules if needed.

### Audit / exploration defaults

Two config flags install default catch-all rules (prepended, so explicit rules
listed later still win for their specific matches):

```json
{
  "tools": {
    "auditModeReadOnly": true,
    "explorationModeDenyMutations": true
  }
}
```

- `tools.auditModeReadOnly` — in `audit` mode, every write (state-mutating)
  tool call is denied unless an explicit rule allows that specific call.
- `tools.explorationModeDenyMutations` — same policy for `exploration` mode.

When the flags are off (the default), audit/exploration runs behave exactly
like foreground runs with respect to mutation policy — no behavior changes
unless policies are configured.

## Ask approvals (`/policy`)

An `ask` rule blocks the tool call and records a pending approval in the
session's `ApprovalStore` (see `nanobot/agent/tools/approval.py`). The
structured `policy_block` result tells the user how to respond:

```
Policy requires explicit approval for tool 'exec' (rule: deploy): deploys need your sign-off.
Approve with `/policy approve pa1-deploy` or deny with `/policy deny pa1-deploy`.
```

The user (in chat or the interactive CLI) then runs:

- `/policy` — list pending approvals for the session
- `/policy approve <token|rule-id>` — allow the call; the decision is cached
  for the session so the same call is not re-prompted
- `/policy deny <token|rule-id>` — deny the call; it blocks with the same
  structured `policy_block` as an explicit `deny` rule for the session

A bare rule id works when exactly one pending approval uses it; otherwise use
the exact token. Approvals are per-session (`sessionKey`) and bounded:

| Config | Default | Meaning |
|---|---|---|
| `tools.approval.timeoutS` | `300` | pending approval validity; expires afterwards and must be re-requested |
| `tools.approval.cacheTtlS` | `600` | approved/denied decision cache window for the session |

```json
{
  "tools": {
    "approval": { "timeoutS": 120, "cacheTtlS": 1800 }
  }
}
```

Legacy `approved_policy_rules` entries in request metadata still work: an
`ask` rule whose id appears there is treated as approved.

## Programmatic mode selection

Via the Python SDK:

```python
bot = Nanobot.from_config()
await bot.run("Summarize the repo", mode="audit")
```

Via `process_direct`:

```python
await loop.process_direct("inspect only", metadata={"interaction_mode": "audit"})
```

## Enforcement

The registry evaluates policy after parameter validation and before execution
(`ToolRegistry.evaluate_policy`). Outcomes:

- `allow` — tool runs normally
- `deny` — structured `policy_block` with `decision: "deny"`, `rule_id`,
  `resource`, and tool-policy evidence
- `ask` — same `policy_block` shape with `decision: "ask"` plus an
  `approval_token` when an approval store is attached
