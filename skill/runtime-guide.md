# Agent MCP Runtime Guide

Load this file **only after a task has been delegated**, or when handling waiting, steering, session recovery, timeout, queueing, liveness, daemon or permission problems.

The Skill decides *what should happen*. This guide explains *how to operate the runtime safely*.

## 1. Waiting discipline

Preferred path after `spawn_agent`:

```bash
python3 skill/scripts/wait_agent.py <agent_id>
```

Why: it blocks locally and avoids repeated MCP round-trips entering the host Agent context.

Use MCP `wait_agent` when the host cannot run the helper or the daemon is remote.

Do not repeatedly call `list_agents` or `get_agent_activity` just to ask whether a worker has finished.

### wait helper

```bash
python3 skill/scripts/wait_agent.py <agent_id>
python3 skill/scripts/wait_agent.py <agent_id> --timeout 1200
python3 skill/scripts/wait_agent.py <agent_id> --json
```

Exit codes:

- `0`: terminal state reached;
- `4`: total wait timeout;
- `2/3`: HTTP/connection failure.

When the daemon is not in the default local state directory, use the helper's `--state-dir` option.

## 2. Wait timeout vs task timeout

These are different.

### Wait timeout

The caller stopped blocking. The worker may still be healthy.

### Task timeout

`timeout_seconds` on spawn/follow-up is an execution limit. When it expires, the daemon terminates the process tree and records an incomplete/timeout result.

Never treat a wait timeout alone as proof that a worker is stuck.

## 3. Liveness evidence

Use runtime evidence before declaring a worker dead:

- worker PID alive/dead;
- stdout/stderr log size or mtime growth;
- state transitions;
- adapter/session evidence returned by wait hints.

Some CLIs emit progress mainly on stderr. Lack of stdout is not enough to declare a hang.

Decision rule:

```text
PID alive OR logs growing
    -> worker is probably healthy; continue waiting

PID dead AND logs stopped
    -> treat as truly stalled; recover/re-dispatch
```

## 4. Steering and follow-up

Use `steer_agent` when the active worker must change direction immediately.

Use `followup_task` for a new turn that should reuse the worker/session context after the current work, or after a result needs correction.

Prefer follow-up over spawning a fresh worker when the same specialist already has the right local context.

## 5. Session ownership and reconnection

Agent IDs are scoped by a stable host conversation/session identity.

A new MCP connection in the **same host conversation** should be able to recover previously created workers.

If an Agent ID appears unavailable:

1. call `list_agents` with cross-session visibility only for diagnosis;
2. determine whether this is the same host conversation reconnecting or a genuinely different conversation;
3. reuse the old worker only when ownership rules permit it;
4. otherwise spawn a new worker and include the prior summary/context.

Do not blindly reuse an Agent ID from another conversation.

## 6. Error recovery

| Symptom | Preferred action |
|---|---|
| Tool missing from visible list | Verify host MCP registration/protocol support; do not invent an alternate shell protocol |
| Tool returns parameter/session error | Read the structured error and fix the exact cause; stop if ownership is ambiguous |
| Wait timeout but PID/logs healthy | Continue waiting; do not interrupt |
| PID dead and logs stopped | Interrupt/clean up if needed, then re-dispatch with prior summary |
| Authentication failure | Check the target CLI's own login/provider credentials |
| Binary not found | Verify the executable path/PATH for that CLI |
| Permission denied | Raise `permission_mode` only as far as the task actually requires |
| Queue remains queued | Inspect occupied slots; interrupt only genuinely lower-priority work |
| Daemon unavailable | Let MCP attempt its normal startup path; use manual daemon startup only for diagnosis |
| Verification failed | Let configured retry/follow-up finish before judging the branch |

If the cause remains unclear, return `BLOCKED` with the evidence already collected. Do not burn tokens with no-op commands or repeated identical calls.

## 7. Permission modes

Default to the least privilege that can complete the subtask.

Typical progression:

```text
plan -> acceptEdits -> fullAccess
```

Do not preemptively grant full access to read-only exploration or review tasks.

## 8. Runtime tools by lifecycle stage

### Start work

- `spawn_agent`
- `orchestrate_task` only when the semantic plan/DAG is already explicit

### Communicate/change direction

- `send_message`
- `steer_agent`
- `followup_task`

### Wait/inspect

- local `wait_agent.py` helper first
- `wait_agent` when needed
- `list_agents` for task-tree inspection/recovery
- `get_agent_activity` for debugging/diagnostics, not polling

### Stop

- `interrupt_agent`

### Cost/accounting

- `get_token_usage`

### Durable project knowledge

- `memory_store`
- `memory_recall`

### Runtime governance

- `policy_list`
- `policy_add`
- `policy_state`

## 9. Verification boundary

A daemon-side `verify_command` proves only that the configured deterministic check passed.

The host Agent must still judge:

- whether the result solves the actual user goal;
- whether the branch stayed within scope;
- whether evidence supports the worker's claims;
- whether another review/follow-up is required.

## 10. Escalation contract

Workers should not guess through unresolved ambiguity.

Use:

- `BLOCKED: ...`
- `NEEDS_CONTEXT: ...`
- `NEEDS_DECISION: ...`

Include what was tried and the smallest missing input/decision required.

The host Agent should intervene only when such a decision state or real runtime failure is reached.