# ADR-0001: Skill-first, MCP-backed runtime

- Status: Accepted
- Date: 2026-09-02
- Scope: Product architecture and documentation ownership

## Context

Agent MCP began as an MCP-oriented multi-Agent orchestration project, but its responsibilities have expanded beyond a tool server. It now includes:

- a reusable orchestration Skill;
- MCP tools for spawn/wait/steer/follow-up/memory/policy/orchestration;
- a durable daemon with queueing, sessions, retries, usage, persistence and SSE;
- multiple Agent CLI adapters;
- runtime observability and policy controls.

This creates a product-architecture question: should the project remain MCP-centric, become a pure Skill, or combine both?

## Decision

Agent MCP will use a **Skill-first, MCP-backed runtime** architecture.

The three layers are:

1. **Skill layer — orchestration control plane**
   - decides whether delegation is useful;
   - decomposes tasks;
   - chooses role/CLI/model;
   - defines task briefs and acceptance criteria;
   - synthesizes and judges worker results.

2. **MCP layer — capability plane**
   - exposes stable typed runtime capabilities;
   - handles protocol compatibility, host/session identity and request validation;
   - remains thin and mostly stateless;
   - forwards durable operations to the daemon.

3. **Daemon layer — execution plane**
   - owns process lifecycle, queues, durable state, sessions, retry/verification, usage, events and observability;
   - survives individual model turns and MCP connections.

The product experience should be Skill-first: normal users should not need to manually choreograph MCP tools. MCP remains the standard reusable interface under that experience.

## Why not pure Skill

A pure Skill would make the initial installation story simpler, but it would weaken the runtime boundary.

Agent MCP needs capabilities that are not naturally represented by instruction files alone:

- long-running workers that outlive one tool call;
- durable queues and session continuation;
- process-tree supervision;
- multi-host reuse;
- typed external capabilities that other hosts/Skills can call;
- centralized runtime policy and observability.

Implementing these as ad-hoc scripts embedded behind each Skill host would recreate host-specific integration fragmentation.

## Why not pure MCP

A pure MCP server exposes tools but does not guarantee good orchestration behavior.

The host Agent still needs reusable policy for:

- when not to delegate;
- how much decomposition is worthwhile;
- how to avoid context-locality mistakes;
- how to choose execution backends;
- how to brief and verify specialists;
- how to synthesize results.

Those are model-facing reasoning conventions and fit the Skill abstraction better than server code.

## Consequences

### Positive

- Users get a simpler, higher-level Skill-driven experience.
- Runtime capabilities remain reusable across hosts and other Skills.
- Long-running state has a clear durable owner.
- The MCP server can stay small instead of becoming an orchestration monolith.
- Documentation ownership becomes clearer.
- Future protocol evolution can be absorbed at the MCP boundary without rewriting orchestration policy.

### Costs

- The project maintains three conceptual layers instead of one.
- Installation must register runtime capabilities and install orchestration instructions where supported.
- Some concepts exist at adjacent layers (for example semantic orchestration in Skill vs deterministic DAG execution in daemon) and require clear documentation to prevent duplication.

## Documentation rules resulting from this ADR

- `README.md`: product positioning, quick start, major capabilities, high-level architecture, documentation links.
- `docs/`: human/contributor architecture, runtime, integration, operations and historical engineering material.
- `skill/SKILL.md`: compact always-loaded orchestration control plane.
- `skill/*.md`: on-demand Agent-facing guidance such as routing, task briefs and runtime recovery.
- `mcp_server.py`: protocol/capability facade, not the place for semantic orchestration policy.
- `agent_mcp/`: durable runtime implementation.

## Non-goals

This ADR does not require:

- renaming the repository;
- removing MCP terminology from the technical implementation;
- moving every orchestration feature out of the daemon;
- rewriting working runtime code merely to match documentation structure.

The immediate change is architectural clarity and progressive documentation loading; runtime refactors should happen only when they improve an actual boundary violation.