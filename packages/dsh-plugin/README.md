# dsh-plugin-agentmcp

<p align="center">
  <img src="assets/agentmcp-mark-256.png" width="96" height="96" alt="agentmcp mark">
</p>

<p align="center">
  <strong>DeepSeek Harness (DSH) bundle plugin for <a href="https://github.com/37chengshan/agent-mcp">agent-mcp</a></strong><br>
  One <code>dsh plugin add</code> → tool catalog shows <code>mcp__agentmcp__*</code>
</p>

English · [中文](./README.zh.md)

## What this is

An npm-publishable DSH **bundle plugin**. It does not reimplement agent-mcp; it only ships a
`cordis.patch.yml` that inserts the official `@deepseek-ai/dsh-mcp-client` plugin and points it at
agent-mcp’s stdio MCP server (`mcp_server.py`).

```
DSH session ── spawn mcp_server.py (stdio JSON-RPC) ──► agent-mcp daemon (http://127.0.0.1:8765)
              └── tools registered as mcp__agentmcp__<rawName>
```

The **exact tool count** is whatever `mcp_server.py` returns from `tools/list` — this README does
not pin a number.

## Prerequisites

| Item | Requirement |
|---|---|
| Python | ≥ 3.10 on `PATH` (`python3` / `python` / `py`) |
| agent-mcp | repo checkout or `~/.agent-mcp/` install copy (`mcp_server.py` + `agent_mcp/`) |
| Node.js | ≥ 18 (for the cross-platform launcher `scripts/launch-agentmcp.mjs`) |
| DSH | any profile (example below uses `web`) |
| External CLIs | claude / grok / codex / … logged in as needed (agent-mcp never rewrites CLI config) |

`mcp_server.py` has **zero third-party Python deps**. If the agent-mcp daemon is not running yet,
the first MCP call auto-starts it (idempotent); you can also run `python3 start_agent_mcp.py` first.

## Install

Recommended — install as a DSH plugin (host plane, one shared daemon per machine):

```bash
# npm registry (after publish)
dsh plugin --profile web add dsh-plugin-agentmcp

# GitHub (subdirectory package; no npm publish required)
dsh plugin --profile web add github:37chengshan/agent-mcp/packages/dsh-plugin

# local checkout
dsh plugin --profile web add ./packages/dsh-plugin
```

Then refresh / restart DSH (HMR may hot-swap). The tool catalog should show `mcp__agentmcp__*`.

### Fallback — manual patch (no plugin system)

Copy the insert block from [`cordis.patch.yml`](./cordis.patch.yml) into
`$DSH_HOME/profiles/<name>/cordis.patch.yml` (or `$DSH_HOME/cordis.patch.yml`), **or** try once
without persisting:

```bash
dsh web --patch /absolute/path/to/agent-mcp/examples/dsh/agentmcp.cordis.yml
```

Full field reference and troubleshooting: [`docs/dsh-integration.md`](../../docs/dsh-integration.md).

## How the launcher finds things

`scripts/launch-agentmcp.mjs` (Node) picks a Python interpreter, then resolves `mcp_server.py` in
this order — **no hardcoded user home paths**:

1. `AGENT_MCP_MCP_SERVER` (explicit absolute path)
2. Walk up from this package dir (monorepo root / vendored sibling `agent-mcp/mcp_server.py`)
3. `~/.agent-mcp/mcp_server.py` (install.py user copy)
4. Clear stderr + exit 1 (DSH keeps the session alive because `failOnStartupError: false`)

Override the state dir / port via plugin `env` if needed: `AGENT_MCP_HOME`, `AGENT_MCP_PORT`.

## Verify

```bash
# 1) daemon health (optional — MCP auto-starts it)
python3 start_agent_mcp.py
curl -s http://127.0.0.1:8765/health

# 2) syntax-check the launcher (Node ≥ 18)
node --check packages/dsh-plugin/scripts/launch-agentmcp.mjs

# 3) real DSH session: tool catalog shows mcp__agentmcp__*
# 4) call mcp__agentmcp__estimate_complexity → level + rationale (local, no spawn)
# 5) call mcp__agentmcp__spawn_agent then loop mcp__agentmcp__wait_agent (timeout 25)
#    until terminated; check FINAL_ANSWER summary + usage
# 6) restart daemon → list_agents still finds previous agents
```

Optional deep handshake against the same MCP SDK DSH uses:
`examples/dsh/verify_handshake.mjs` (see file header; needs `DSH_SDK_DIR`).

## Uninstall

```bash
dsh plugin --profile web remove dsh-plugin-agentmcp
```

If you used the manual patch fallback, delete the `- id: mcp-agentmcp` insert block from
`cordis.patch.yml` and refresh DSH.

## Layout

```
packages/dsh-plugin/
├── package.json          # dsh.bundle.patch → ./cordis.patch.yml
├── cordis.patch.yml      # inserts @deepseek-ai/dsh-mcp-client (serverName: agentmcp)
├── scripts/
│   └── launch-agentmcp.mjs   # cross-platform python + mcp_server.py resolver
├── assets/
│   └── agentmcp-mark-256.png
├── README.md
└── README.zh.md
```

Single source of truth for the insert block is **this package**; `examples/dsh/agentmcp.cordis.yml`
is a manual-fallback excerpt only.

## License

MIT © agent-mcp contributors · repository: <https://github.com/37chengshan/agent-mcp>
