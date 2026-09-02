---
name: agent-mcp
description: Use when a coding or research task may benefit from delegation across multiple Agent CLIs, especially for parallel exploration, specialist review, long-running execution, steering/follow-up, or multi-CLI synthesis. Default to direct execution for small tasks; use Agent MCP only when delegation has clear value.
---

# Agent MCP Orchestration Skill

Agent MCP is a **Skill-first, MCP-backed runtime**.

- The **host Agent + this Skill** decide whether/how to delegate.
- **MCP** exposes stable execution capabilities.
- The **daemon** owns long-running processes, queues, sessions, retries and durable state.

Do not move semantic decomposition or result judgment into the runtime.

## 1. First decide whether to delegate

Default: **do the task directly**.

Use `estimate_complexity` as advisory input, then apply judgment:

| Level | Typical shape | Default action |
|---|---|---|
| S | one file, tiny edit, quick answer, no parallel value | do not spawn |
| M | 2–3 files, mostly sequential, modest exploration | at most one useful delegated branch |
| L | >3 files, independent branches, context pressure, specialist review | use full orchestration |

Never delegate merely because tools are available.

Do **not** spawn for tiny edits, strongly sequential work, multiple edits to the same small file, or tasks whose coordination cost exceeds the implementation work.

## 2. Decompose for cognitive locality

Split only into independently verifiable subgoals.

Prefer parallel branches when they have little shared mutable context, for example:

- repository exploration vs security review;
- independent subsystem analysis;
- documentation research vs implementation investigation.

Keep work together when branches need the same evolving mental model or will edit the same files.

If a task graph is useful, the host Agent defines the semantic graph first. `orchestrate_task` may execute an explicit DAG, but the daemon must not invent the decomposition.

## 3. Choose role, CLI and model deliberately

Load [`cli-guide.md`](cli-guide.md) **only when choosing or comparing execution backends/models**.

Load one role preset from [`agents/`](agents/) only when a specialist role is actually needed, such as:

- planner
- architect
- code-explorer
- code-reviewer
- security-reviewer
- tdd-guide
- build-error-resolver
- e2e-runner
- refactor-cleaner
- doc-updater

Role presets define behavior, not a permanently hard-coded CLI/model. Choose the execution backend at dispatch time.

## 4. Brief every delegated task

Before spawning, load [`task-brief.md`](task-brief.md) when the task needs nontrivial scope/boundaries.

A useful brief contains only the fields that matter:

1. measurable goal;
2. allowed files/directories/commands;
3. explicit boundaries and forbidden changes;
4. required self-check level;
5. output contract ending in `FINAL_ANSWER: <summary>`;
6. escalation contract: `BLOCKED` / `NEEDS_CONTEXT` / `NEEDS_DECISION`.

Prefer concise briefs. Do not duplicate the entire parent conversation into every worker.

## 5. Dispatch through MCP

Use `spawn_agent` for independent workers.

Important dispatch choices:

- `cwd` must identify the correct workspace;
- use the least required `permission_mode`;
- use `context_mode` / `summary_chars` / `return_ref` to control returned context;
- use `token_budget` when cost limits matter;
- use `verify_command` only for deterministic checks that are safe to automate;
- keep dependent writes serial unless isolated worktrees make the dependency explicit.

For a declared DAG with explicit dependencies, use `orchestrate_task` when runtime-side scheduling is beneficial.

## 6. After dispatch, load runtime guidance only when needed

For waiting, steering, follow-up, session recovery, timeout, liveness, queue, daemon or permission issues, load:

[`runtime-guide.md`](runtime-guide.md)

Default waiting path:

```bash
python3 skill/scripts/wait_agent.py <agent_id>
```

This avoids repeated MCP polling entering the host Agent context.

Do not repeatedly call `list_agents` / `get_agent_activity` just to ask whether work is finished.

## 7. Synthesize; do not blindly trust workers

For each returned branch:

1. compare `FINAL_ANSWER` with the delegated goal;
2. spot-check the key evidence/files/tests;
3. return the same worker via `followup_task` when correction is needed;
4. deep-review only where risk justifies it;
5. synthesize the useful result into the parent task.

A deterministic verification command is evidence, not proof that the user's real goal is satisfied.

## 8. Keep responsibilities separated

### Skill / host Agent owns

- whether to delegate;
- semantic decomposition;
- role/CLI/model choice;
- parallel vs serial topology;
- task briefs;
- semantic acceptance and synthesis.

### MCP/daemon owns

- stable runtime tool interface;
- process lifecycle;
- queue/concurrency state;
- task timeout and process-tree termination;
- durable sessions/resume/follow-up mechanics;
- runtime usage/events/cache/memory/policy;
- deterministic verification/retry mechanics.

When uncertain where new behavior belongs, ask:

> Does this encode Agent judgment, or must it remain correct even if the host model/client changes?

Agent judgment belongs here. Host-independent durable execution belongs in MCP/runtime.

## References

Load these progressively rather than all at once:

- [`cli-guide.md`](cli-guide.md) — backend/model selection.
- [`task-brief.md`](task-brief.md) — delegation contract and examples.
- [`runtime-guide.md`](runtime-guide.md) — lifecycle, waiting and recovery.
- [`agents/`](agents/) — one selected specialist preset.
- [`scripts/wait_agent.py`](scripts/wait_agent.py) — low-context blocking wait helper.
