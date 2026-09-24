# dsh-plugin-agentmcp

<p align="center">
  <img src="assets/agentmcp-mark-256.png" width="96" height="96" alt="agentmcp mark">
</p>

<p align="center">
  <strong>面向 <a href="https://github.com/37chengshan/agent-mcp">agent-mcp</a> 的 DeepSeek Harness（DSH）bundle 插件</strong><br>
  让 Agent 跑在最适配的底座上 · 打破隔离 · 一条 <code>dsh plugin add</code> 接入 <code>mcp__agentmcp__*</code>
</p>

[English](./README.md) · 中文

## 这是什么

可 `npm publish` 的 DSH **bundle 插件**。它不重写 agent-mcp，只携带一份 `cordis.patch.yml`，
通过官方 `@deepseek-ai/dsh-mcp-client` 以 **stdio** 接入 agent-mcp 的 MCP 薄层（`mcp_server.py`）：

```
DSH 会话 ── spawn mcp_server.py（stdio JSON-RPC）──► agent-mcp daemon（http://127.0.0.1:8765）
              └── 工具注册为 mcp__agentmcp__<rawName>
```

**工具数量以 `mcp_server.py` 的 `tools/list` 响应为准**——本文档不单独维护固定数字。

## 前置条件

| 项 | 要求 |
|---|---|
| Python | ≥ 3.10，且在 `PATH` 中（`python3` / `python` / `py` 任一） |
| agent-mcp | 仓库源码或 `~/.agent-mcp/` 安装副本（`mcp_server.py` + `agent_mcp/`） |
| Node.js | ≥ 18（启动器 `scripts/launch-agentmcp.mjs` 需要） |
| DSH | 任意 profile（下文示例 `web`） |
| 外部 CLI | claude / grok / codex 等按需已登录（agent-mcp 不改写任何 CLI 配置） |

`mcp_server.py` **零第三方 Python 依赖**。daemon 未起时首次 MCP 调用会自动原子拉起（幂等）；
也可先手动 `python3 start_agent_mcp.py`。

## 安装

推荐——作为 DSH 插件安装（host 平面，全机共享同一组工具与同一 daemon）：

```bash
# npm registry（发布后）
dsh plugin --profile web add dsh-plugin-agentmcp

# GitHub 直装（子目录包，无需 npm publish）
dsh plugin --profile web add github:37chengshan/agent-mcp/packages/dsh-plugin

# 本地仓库路径
dsh plugin --profile web add ./packages/dsh-plugin
```

随后刷新 / 重启 DSH（HMR 可热替换）。工具目录应出现 `mcp__agentmcp__*`。

### 回退——手工 patch（不用插件系统）

把 [`cordis.patch.yml`](./cordis.patch.yml) 中的 insert 块合并进
`$DSH_HOME/profiles/<name>/cordis.patch.yml`（或 `$DSH_HOME/cordis.patch.yml`，**先读后追加**），
或临时试用、不改持久配置：

```bash
dsh web --patch /绝对路径/agent-mcp/examples/dsh/agentmcp.cordis.yml
```

完整字段说明与故障排查见 [`docs/dsh-integration.md`](../../docs/dsh-integration.md)。

## 启动器如何解析路径

`scripts/launch-agentmcp.mjs`（Node）先挑 Python 解释器，再按序定位 `mcp_server.py`——
**不写死任何用户主路径**：

1. 环境变量 `AGENT_MCP_MCP_SERVER`（显式绝对路径）
2. 从本包目录向上走（monorepo 仓库根 / vendored 邻接副本 `agent-mcp/mcp_server.py`）
3. `~/.agent-mcp/mcp_server.py`（install.py 用户安装副本）
4. stderr 清晰报错并 exit 1（`failOnStartupError: false` 时 DSH 不阻塞会话）

需要时可在插件 `env` 里覆盖状态目录 / 端口：`AGENT_MCP_HOME`、`AGENT_MCP_PORT`。

## 验证

```bash
# 1) daemon 健康检查（可省——MCP 首调自动拉起）
python3 start_agent_mcp.py
curl -s http://127.0.0.1:8765/health

# 2) 启动器语法自检（Node ≥ 18）
node --check packages/dsh-plugin/scripts/launch-agentmcp.mjs

# 3) 真实 DSH 会话：工具目录出现 mcp__agentmcp__*
# 4) 调用 mcp__agentmcp__estimate_complexity → level + rationale（本地直算、零 spawn）
# 5) 调用 mcp__agentmcp__spawn_agent，再循环 mcp__agentmcp__wait_agent（timeout 25）
#    至 terminated，核对 FINAL_ANSWER 摘要与 usage
# 6) 重启 daemon → list_agents 仍可查到旧 agent
```

可选深度握手（与 DSH 同款 MCP SDK）：`examples/dsh/verify_handshake.mjs`（见文件头，需 `DSH_SDK_DIR`）。

## 卸载

```bash
dsh plugin --profile web remove dsh-plugin-agentmcp
```

若走的是手工 patch 回退，从 `cordis.patch.yml` 删除 `- id: mcp-agentmcp` 的 insert 块后刷新 DSH。

## 目录结构

```
packages/dsh-plugin/
├── package.json          # dsh.bundle.patch → ./cordis.patch.yml
├── cordis.patch.yml      # 插入 @deepseek-ai/dsh-mcp-client（serverName: agentmcp）
├── scripts/
│   └── launch-agentmcp.mjs   # 跨平台 python + mcp_server.py 解析启动器
├── assets/
│   └── agentmcp-mark-256.png
├── README.md
└── README.zh.md
```

insert 块的**单一事实来源是本包**；`examples/dsh/agentmcp.cordis.yml` 仅作手工 fallback 摘录。

## 许可证

MIT © agent-mcp contributors · 仓库：<https://github.com/37chengshan/agent-mcp>
