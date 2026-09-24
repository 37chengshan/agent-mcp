# Agent MCP Documentation

Agent MCP uses a **Skill-first, MCP-backed runtime** architecture: the Skill decides how to orchestrate work, MCP exposes stable capabilities, and the daemon owns long-running execution state.

This directory is the human/developer documentation hub. Agent-facing orchestration instructions live under [`../skill/`](../skill/).

## Start here

| Goal | Document |
|---|---|
| Understand the product in 5 minutes | [`../README.md`](../README.md) |
| Install Agent MCP | [`install-guide.md`](install-guide.md) |
| Understand the architecture | [`architecture.md`](architecture.md) |
| Understand MCP + daemon runtime behavior | [`runtime.md`](runtime.md) |
| Understand why the project is not pure Skill or pure MCP | [`decisions/0001-skill-first-mcp-runtime.md`](decisions/0001-skill-first-mcp-runtime.md) |
| Integrate DeepSeek Harness | [`dsh-integration.md`](dsh-integration.md) |
| Add a custom Agent CLI | [`custom-cli.md`](custom-cli.md) |
| Check supported capabilities | [`capability-matrix.md`](capability-matrix.md) |
| Run acceptance checks | [`acceptance.md`](acceptance.md) |

## Documentation map

### Architecture and runtime

- [`architecture.md`](architecture.md) — the three-layer model: Skill / MCP / daemon.
- [`runtime.md`](runtime.md) — tool boundary, task lifecycle, sessions, waiting, recovery and observability.
- [`architecture.svg`](architecture.svg) — implementation-oriented architecture diagram.
- [`workflow.svg`](workflow.svg) — orchestration lifecycle diagram.
- [`decisions/`](decisions/) — architecture decision records (ADR).

### Installation and integrations

- [`install-guide.md`](install-guide.md) — installation and host registration.
- [`dsh-integration.md`](dsh-integration.md) — DeepSeek Harness integration.
- [`custom-cli.md`](custom-cli.md) — custom CLI adapter format.
- [`custom-cli-examples/`](custom-cli-examples/) — custom CLI examples.

### Verification and compatibility

- [`capability-matrix.md`](capability-matrix.md) — capability coverage.
- [`acceptance.md`](acceptance.md) — acceptance criteria and checks.

### Historical engineering material

These files are useful for implementation history, but they are **not the canonical product entrypoint**:

- [`plans/`](plans/) — design and implementation plans.
- [`research/`](research/) — benchmark, coverage and technical research snapshots.
- [`review/`](review/) — iteration reviews and completion reports.

## Agent-facing documentation

The `skill/` directory is intentionally separate from `docs/` because it is loaded into an Agent's working context.

| Path | Responsibility |
|---|---|
| [`../skill/SKILL.md`](../skill/SKILL.md) | Small orchestration control plane; always-loaded workflow and invariants |
| [`../skill/cli-guide.md`](../skill/cli-guide.md) | Load only when choosing a CLI/model or comparing execution backends |
| [`../skill/task-brief.md`](../skill/task-brief.md) | Load before preparing a delegated task brief |
| [`../skill/runtime-guide.md`](../skill/runtime-guide.md) | Load after delegation or when handling lifecycle/runtime errors |
| [`../skill/agents/`](../skill/agents/) | Role-specific presets, loaded only for the selected specialist |
| [`../skill/scripts/`](../skill/scripts/) | Deterministic helper scripts, especially low-context waiting |

## Documentation ownership rules

To prevent the repository from drifting back into duplicated long-form documentation:

1. **README is the landing page, not the manual.** Keep product positioning, quick install, key capabilities and links there.
2. **`docs/` explains the system to humans and contributors.** Architecture, integration, operations, validation and historical design belong here.
3. **`skill/SKILL.md` is a control plane, not a knowledge dump.** Keep only the orchestration path and non-negotiable invariants in it.
4. **Runtime API details belong in one place.** Human-facing details go in `docs/runtime.md`; Agent execution rules go in `skill/runtime-guide.md`.
5. **Role-specific behavior stays out of the entrypoint.** Put it in `skill/agents/*.md` and load only the selected role.
6. **Plans and research are evidence, not current truth.** Current behavior is defined by code, tests, capability matrix and the architecture/runtime docs.

## Canonical model

```text
User task
   |
   v
Host Agent
   |
   | loads orchestration policy
   v
Skill layer              -> decides whether/how to delegate
   |
   | calls stable tools
   v
MCP layer                -> exposes typed capabilities
   |
   | authenticated local RPC
   v
Daemon runtime           -> owns queues, processes, sessions and durable state
   |
   v
Claude / Codex / OMP / OpenCode / Grok / Kimi / ...
```

If a document contradicts this ownership model, treat [`architecture.md`](architecture.md) and ADR-0001 as the current design direction.