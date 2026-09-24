# Runtime Guide

This document explains the MCP/daemon execution boundary for humans and contributors. Agent-facing recovery rules belong in [`../skill/runtime-guide.md`](../skill/runtime-guide.md).

## Runtime ownership

The runtime begins **after** the host Agent has decided to delegate work.

```text
Host Agent / Skill
      |
      | spawn_agent(...)
      v
MCP server
      |
      | validate + forward
      v
Daemon
      |
      | create durable task state
      | schedule a slot
      | launch adapter/worker
      v
Target Agent CLI
```

## MCP server responsibilities

`mcp_server.py` is intentionally a thin capability boundary.

It should own:

- MCP tool/resource/prompt registration;
- protocol negotiation and compatibility;
- request validation and normalization;
- host/session identity derivation;
- session ownership enforcement at the protocol boundary;
- daemon discovery and safe startup;
- forwarding requests to the daemon;
- structured response shaping.

It should **not** own:

- semantic task decomposition;
- model/CLI strategy;
- queue scheduling policy that depends on live runtime state;
- durable task state;
- process lifecycle supervision;
- worker log tailing;
- retry loops that must survive one MCP connection.

## Daemon responsibilities

The daemon is the durable execution service.

It owns:

- task and worker lifecycle;
- process-tree launch/termination;
- concurrency slots and queueing;
- worker heartbeat and liveness evidence;
- session continuation metadata;
- verification/retry loops;
- token/cost usage persistence;
- runtime cache;
- memory storage/recall primitives;
- policy enforcement that depends on runtime state;
- SSE/event history;
- web dashboard data;
- worktree/DAG execution mechanics.

## Task lifecycle

A typical worker lifecycle is:

```text
requested
   |
   +--> queued -----------+
   |                      |
   v                      |
starting                  |
   |                      |
   v                      |
running <-----------------+
   |
   +--> needs_advisor
   |       |
   |       +--> follow-up / steer
   |
   +--> verifying
   |       |
   |       +--> retry same session
   |
   +--> terminated
   +--> incomplete/timeout
   +--> error
   +--> orphaned
```

The exact database/state-machine names are implementation details; the important architectural rule is that transitions are persisted by the runtime, not inferred from one model turn.

## Waiting

Waiting is a special case because naive polling can flood the host Agent's context with repeated tool results.

Preferred path:

```text
spawn_agent
   ↓
local wait helper
   ↓
block outside model context
   ↓
return once terminal/decision state is reached
```

The bundled helper is:

```bash
python3 skill/scripts/wait_agent.py <agent_id>
```

Use MCP `wait_agent` directly when the host cannot run the helper or the daemon is remote.

Avoid repeated `list_agents` / `get_agent_activity` calls solely to ask "is it done yet?". Those endpoints are primarily for inspection and debugging.

## Liveness and timeouts

Two different timeout concepts must stay separate:

### Wait timeout

A wait timeout only means the caller stopped blocking. It is **not** evidence that the worker is dead.

### Task timeout

A task timeout is an execution policy. When exceeded, the runtime terminates the task's process tree and records an incomplete/timeout result.

Liveness should be judged from runtime evidence such as:

- worker PID alive/dead;
- output/error log growth;
- adapter/session evidence;
- state-machine transitions.

Do not treat lack of stdout as proof of failure: some CLIs emit progress primarily to stderr.

## Session continuity

The MCP connection itself is not the task owner. A stable host conversation/session identity is the ownership boundary.

This allows:

```text
host conversation
   ↓
MCP connection A -> spawn worker
   ↓
connection closes
   ↓
MCP connection B in same host conversation
   ↓
recover / wait / follow up same worker
```

The runtime should persist enough metadata to make reconnection possible without making cross-session worker IDs universally mutable.

## CLI adapters

Adapters normalize incompatible Agent CLIs into one runtime contract.

Adapter responsibilities typically include:

- executable/argument construction;
- permission-mode translation;
- model argument mapping;
- session/resume handling when supported;
- stdout/stderr event parsing;
- usage extraction;
- final-answer extraction;
- CLI-specific startup quirks.

Adapters should not decide *why* a task was delegated or whether another CLI would be semantically better. That is Skill/host policy.

## Verification and follow-up

Verification can be daemon-side because it is deterministic execution control:

```text
worker completes
   ↓
verify_command
   ↓
pass -> terminal result
fail -> follow up same session (within configured attempts)
```

The host Agent still owns semantic acceptance. A passing test command does not prove that the worker solved the user's actual goal.

## Runtime policies

Policies such as budgets, approvals, tool restrictions and sandbox mappings may be enforced in the runtime because they constrain execution regardless of which host Agent initiated it.

A useful distinction is:

```text
Skill policy      = what is a good orchestration choice?
Runtime policy    = what execution is allowed?
```

Examples:

- "Do not delegate a two-line edit" -> Skill policy.
- "Do not exceed this token budget" -> runtime policy.
- "Use a security reviewer for auth changes" -> Skill policy.
- "This worker may not use network access" -> runtime policy.

## Observability

The dashboard is a view over runtime truth, not a second source of state.

Its data should come from persisted/events-backed runtime state so that CLI clients, MCP calls and the web UI all describe the same task graph.

## Boundary test

When deciding where a new feature belongs, ask:

> Would this feature still need to exist if the host Agent were replaced by a different model/client tomorrow?

- If **yes**, it probably belongs in MCP/runtime.
- If **no**, and it encodes judgment about how an Agent should reason or orchestrate, it probably belongs in the Skill.

Then ask a second question:

> Must this behavior survive the current MCP connection or model turn?

- If **yes**, it belongs in the daemon/runtime rather than the MCP facade.