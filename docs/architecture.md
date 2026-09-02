# Architecture

Agent MCP is not a pure MCP server and it is not a pure Skill. It is a three-layer Agent runtime with a **Skill-first product surface**.

## The three layers

```text
┌─────────────────────────────────────────────┐
│ Skill layer — orchestration control plane   │
│                                             │
│ • decide whether delegation is worthwhile  │
│ • decompose tasks                           │
│ • choose CLI/model/role                     │
│ • choose parallel vs serial execution       │
│ • define task brief and acceptance criteria │
│ • synthesize and judge results              │
└──────────────────────┬──────────────────────┘
                       │ MCP tool calls
                       v
┌─────────────────────────────────────────────┐
│ MCP layer — capability plane                │
│                                             │
│ • typed stable tool surface                 │
│ • host/session boundary                     │
│ • request validation                        │
│ • daemon discovery / atomic startup         │
│ • protocol compatibility                    │
└──────────────────────┬──────────────────────┘
                       │ authenticated local RPC
                       v
┌─────────────────────────────────────────────┐
│ Daemon layer — execution plane              │
│                                             │
│ • process lifecycle                         │
│ • queue and concurrency slots               │
│ • durable task/session state                │
│ • timeout / resume / follow-up              │
│ • verification and retry                    │
│ • usage, cache, memory, policy, SSE         │
└──────────────────────┬──────────────────────┘
                       │ subprocess / adapters
                       v
        Claude / Codex / OMP / OpenCode / ...
```

## Why Skill is the entrypoint

MCP tools by themselves expose capabilities but do not encode good orchestration judgment. A host Agent still needs to know:

- when **not** to delegate;
- how large a task must be before multi-Agent overhead is justified;
- which branches can run in parallel;
- when two tasks share too much context to split safely;
- which CLI/model is appropriate;
- how to brief a worker and validate its result.

These decisions are model-facing policy, so they belong in the Skill layer.

The user-facing product should therefore feel like:

```text
install Agent MCP
      ↓
Host Agent learns the Agent MCP Skill
      ↓
Host Agent automatically decides when to use the runtime
```

Users should not have to think in terms of `tools/list`, stdio, daemon ports or task IDs during normal use.

## Why MCP remains necessary

The runtime needs a stable interface that can be reused by multiple hosts and Skills. MCP provides that boundary.

Without it, each host integration would need to understand runtime-specific scripts, process paths, serialization rules and lifecycle semantics. That would recreate the exact CLI fragmentation Agent MCP is trying to remove.

The MCP layer should stay **thin and stateless**. It is not the scheduler and it should not become the place where orchestration policy lives.

Its responsibilities are limited to:

1. expose stable tool schemas;
2. identify the caller/host/session;
3. validate and normalize requests;
4. find or atomically start the daemon;
5. forward requests and return structured results;
6. preserve protocol compatibility.

## Why the daemon remains necessary

A Skill invocation is not a reliable home for long-running mutable state.

Agent MCP needs behavior such as:

- a worker keeps running after the initiating tool call returns;
- queued tasks start when slots are available;
- tasks can be steered or followed up later;
- the host can reconnect and recover prior task state;
- timeouts kill complete process trees;
- verification can re-enter the same worker session;
- usage and events can be persisted and streamed;
- multiple MCP connections can interact with one durable runtime.

Those are runtime concerns and belong in a daemon/state machine, not in `SKILL.md`.

## Responsibility matrix

| Question | Owner |
|---|---|
| Should this task be delegated? | Skill / host Agent |
| How should it be decomposed? | Skill / host Agent |
| Which CLI/model should run it? | Skill / host Agent |
| What is the worker's prompt? | Skill / host Agent |
| How do I start a worker? | MCP capability |
| How do I wait/steer/follow up? | MCP capability |
| Where is process state stored? | Daemon |
| Who owns queueing and concurrency? | Daemon |
| Who handles process-tree termination? | Daemon |
| Who persists usage/events/session state? | Daemon |
| Who decides whether a returned result is acceptable? | Skill / host Agent |

## Repository mapping

```text
skill/
  SKILL.md              # small control plane
  cli-guide.md          # on-demand routing knowledge
  task-brief.md         # delegation contract
  runtime-guide.md      # on-demand lifecycle/recovery rules
  agents/               # specialist role prompts
  scripts/              # deterministic helpers

mcp_server.py            # MCP capability boundary

agent_mcp/
  daemon_main.py         # runtime coordinator
  daemon_http.py         # local runtime API + SSE
  db.py                  # durable state
  state_machine.py       # lifecycle semantics
  cli_adapters.py        # CLI normalization
  orchestrator.py        # daemon-side DAG execution primitive
  policies/              # runtime enforcement
  sandbox/               # runtime execution policy mapping

web/                     # runtime observability UI

docs/                    # human/contributor documentation
```

## A subtle boundary: orchestration vs runtime DAG

Agent MCP has both a Skill that performs orchestration reasoning and a daemon-side `orchestrate_task` capability. These are not duplicates.

The Skill owns **semantic planning**:

```text
What should be split?
Which branch needs which specialist?
Which result matters?
What counts as success?
```

The runtime DAG owns **deterministic execution mechanics** once a graph is already declared:

```text
A and B have no dependency -> run concurrently
C depends on A -> start C after A
worker crashes -> apply runtime recovery policy
worktree branch completes -> expose merge/discard state
```

Do not move semantic task decomposition into the daemon. The runtime should execute an explicit plan, not invent one.

## Product positioning

The repository name can remain `agent-mcp` for protocol recognition and compatibility, but the product description should be broader:

> **Agent MCP is a Skill-first universal Agent runtime that lets one host Agent safely dispatch, monitor and resume work across multiple Agent CLIs through a standard MCP capability layer.**

That wording keeps MCP as a technical foundation without implying that the product is merely another MCP tool server.

## Design constraints

Future changes should preserve these constraints:

1. **Skill-first UX:** normal users interact through host-Agent behavior, not manual tool choreography.
2. **Thin MCP:** do not accumulate business logic in `mcp_server.py`.
3. **Durable runtime:** long-running state must not depend on one model turn or MCP connection.
4. **Host independence:** the same runtime capabilities must remain reusable across Agent hosts.
5. **Progressive Skill loading:** keep always-loaded instructions small; move detailed routing/recovery knowledge to on-demand files.
6. **Explicit execution plans:** runtime primitives may execute DAGs, but semantic decomposition stays with the host Agent.
7. **Observable failure:** queues, timeouts, verification, usage and worker liveness must remain inspectable.

See [`decisions/0001-skill-first-mcp-runtime.md`](decisions/0001-skill-first-mcp-runtime.md) for the architecture decision record.