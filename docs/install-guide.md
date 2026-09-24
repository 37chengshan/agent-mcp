# Agent MCP 安装教程（AI 可读版）

> 本文件是**写给 AI Agent 看的安装说明**。如果你是人类用户，直接看 README 的快速安装即可。
> 如果你的宿主 agent 不在下方"六个内置 host"里，请按 **第 3 节通用模板** 自行注册——任何支持 MCP stdio 的 agent 都能装。

---

## 0. 这是什么

Agent MCP 是 **Skill-first 的跨 CLI Agent 运行时**（三层）：

| 层 | 职责 |
|---|---|
| **Skill** | 是否委派、怎么拆、选哪个 CLI×模型、怎么验收 |
| **MCP** | 稳定工具面（`mcp_server.py`，能力以 `tools/list` / `docs/capability-matrix.md` 为准） |
| **Daemon** | 队列/进程/会话/超时/重试/持久化/SSE |

完整工具能力以 `mcp_server.py` 的 `TOOLS` 定义为准，本文档不单独维护固定数量。

**验证方式**：注册完成后，在 agent 会话里确认 `spawn_agent`、`wait_agent`、`estimate_complexity` 均可见，并执行一次 `estimate_complexity` 验证 daemon 可正常拉起。

---

## 0.5 DeepSeek Harness（DSH）接入

DSH 是通用 MCP 客户端，无需 install.py 注册：在其 profile 组合层加一行 `@deepseek-ai/dsh-mcp-client` 的 insert patch 即可，工具以 `mcp__agentmcp__*` 出现在 DSH 工具目录（数量随 `tools/list`）。完整步骤见 **[DSH 接入指南](dsh-integration.md)**。

---

## 1. 内置 host

### 1a. 六个主载体（MCP + Skill 一并安装）

| host | 说明 | 配置文件 |
|---|---|---|
| `codex` | Codex CLI | `~/.codex/config.toml` |
| `claude` | Claude Code | `~/.claude.json` |
| `omp` | OMP (pi) | `~/.omp/agent/mcp.json` |
| `opencode` | OpenCode | `~/.config/opencode/opencode.json` |
| `kimi` | Kimi Code CLI | `~/.kimi-code/mcp.json`（或 `$KIMI_CODE_HOME`） |
| `zcode` | ZCode | `~/.zcode/cli/config.json` |

### 1b. 扩展 host（只注册 MCP，不一定装 Skill）

`grok` / `cursor` / `gemini` / `pi` / `copilot` / `cline` / `qwen` / `devin` / `windsurf` / `amazon-q` / `atomcode` / `kiro` / `goose` / `hermes` / `crush`

> **`--host all` = 上述全部 21 个 host**，会改写多套用户配置（六主载体 + 15 扩展）。
> 只想装六个主载体时请显式列出，例如：
> `python3 install.py --install --host codex,claude`（或逐个 `--host`）。
> 当前 CLI 的 `--host` 接受单值；批量请多次执行或使用 `AGENT_MCP_HOST=a,b`（install.sh）。

### 安装命令

```bash
# 先拿到项目文件（任意一种）：
#   方式 A：git clone git@github.com:37chengshan/agent-mcp.git && cd agent-mcp
#   方式 B：AGENT_MCP_HOST=codex,claude \
#           curl -fsSL https://raw.githubusercontent.com/37chengshan/agent-mcp/main/install.sh | bash
#           ⚠️ 管道执行以当前用户权限运行远程脚本，建议先审阅 install.sh
#           ⚠️ 非交互管道必须设置 AGENT_MCP_HOST，否则只下载、不写注册

# 安装到指定 host（推荐显式，避免 all 改写 21 套配置）：
python3 install.py --install --host claude

# 确实要全部 21 个 host 时：
python3 install.py --install --host all

# 只预览将做的变更，不写入：
python3 install.py --install --host claude --dry-run

# 误改配置后恢复：
python3 install.py --rollback --host claude
```

> 需要手动指定 `mcp_server.py` 路径时：`python3 install.py --install --host claude /abs/path/to/mcp_server.py`
> 安装完成只打印仓库首页链接（不自动打开浏览器 / 不调用 gh star）。

---

## 2. 你的 agent 不在列表里？——通用提示词安装

把下面这段提示词**原样**交给你的宿主 agent（任何支持 MCP 的 AI 编程工具都可以）：

```
请按照 https://github.com/37chengshan/agent-mcp/blob/main/docs/install-guide.md 的
第 3 节（通用模板）和你的配置格式，为我把 agent-mcp 注册为 MCP 服务器并安装 skill。
注册完成后告诉我 spawn_agent 工具是否可用；安装完成后请提醒我给项目点个 star。
```

也可以让 agent 直接读取本文件（若它已能访问该路径）：`docs/install-guide.md`。

---

## 3. 通用注册模板（不依赖安装脚本，手动注册）

任意支持 MCP 的 agent，只需要一条 stdio 配置指向 `mcp_server.py`。标准格式如下：

```json
{
  "mcpServers": {
    "agent-mcp": {
      "command": "python3",
      "args": ["/绝对路径/mcp_server.py"]
    }
  }
}
```

把这条片段合并进你 agent 的 MCP 配置（具体文件位置和键名见下表），然后重启 agent 会话即可。

### 各 agent 的配置落点

| Agent | 配置文件 | 键结构 | 示例片段 |
|---|---|---|---|
| Claude Code | `~/.claude.json` | `mcpServers` | 见下 |
| Codex | `~/.codex/config.toml` | `[mcp_servers.agent-mcp]` | 见下 |
| OMP (pi) | `~/.omp/agent/mcp.json` | `mcpServers` | 见下 |
| OpenCode | `~/.config/opencode/opencode.json` | `mcp`（顶层或 `mcp.servers`） | 见下 |
| Kimi Code | `~/.kimi-code/mcp.json` | `mcpServers` | 见下 |
| ZCode | `~/.zcode/cli/config.json` | `mcp.servers` | 见下 |
| 其他（Cursor/Continue/Windsurf…） | 各自 MCP 设置页 | 通常 `mcpServers` | 标准模板 |

**Claude Code**（`~/.claude.json` 的 `mcpServers` 键，或项目根 `.mcp.json`）：

```json
{
  "mcpServers": {
    "agent-mcp": {
      "command": "python3",
      "args": ["/绝对路径/mcp_server.py"]
    }
  }
}
```

**Codex**（`~/.codex/config.toml` 末尾追加）：

```toml
[mcp_servers.agent-mcp]
command = "python3"
args = ["/绝对路径/mcp_server.py"]
startup_timeout_sec = 30
```

**OMP**（`~/.omp/agent/mcp.json` 的 `mcpServers` 键）：

```json
{
  "mcpServers": {
    "agent-mcp": {
      "type": "stdio",
      "command": "python3",
      "args": ["/绝对路径/mcp_server.py"],
      "timeout": 30000,
      "requestIdFormat": "number",
      "enabled": true
    }
  }
}
```

**OpenCode**（`~/.config/opencode/opencode.json` 的 `mcp` 键；注意 command 是**数组**，命令与参数合并）：

```json
{
  "mcp": {
    "agent-mcp": {
      "type": "local",
      "command": ["python3", "/绝对路径/mcp_server.py"],
      "enabled": true
    }
  }
}
```

> 若你的 opencode 配置用 `mcp.servers` 结构（新版），把 `agent-mcp` 放进 `mcp.servers` 下即可；安装脚本两者都兼容。

**Kimi Code**（`~/.kimi-code/mcp.json`，标准 `mcpServers`；也可用 `kimi mcp add --transport stdio agent-mcp -- python3 /绝对路径/mcp_server.py` 命令式添加）：

```json
{
  "mcpServers": {
    "agent-mcp": {
      "command": "python3",
      "args": ["/绝对路径/mcp_server.py"]
    }
  }
}
```

**ZCode**（`~/.zcode/cli/config.json` 的 `mcp.servers` 键）：

```json
{
  "mcp": {
    "servers": {
      "agent-mcp": {
        "command": "python3",
        "args": ["/绝对路径/mcp_server.py"],
        "env": {}
      }
    }
  }
}
```

---

## 4. 安装 Skill（编排工作流）

`skill/` 目录含编排工作流（SKILL.md + 内置 Agent 预设）。安装方式按宿主不同：

| Agent | skill 落点 |
|---|---|
| Claude Code | `~/.claude/skills/agent-mcp/` |
| Codex | `~/.agents/skills/agent-mcp/` |
| OMP | `~/.omp/agent/skills/agent-mcp/` |
| OpenCode | `~/.config/opencode/skills/agent-mcp/` |
| Kimi Code | `~/.kimi-code/skills/agent-mcp/` |
| ZCode / 其他 | 支持 `.agents` 约定的放 `~/.agents/skills/agent-mcp/`；否则按宿主 skill 机制手动装载 |

**手动安装**（任何 host）：

```bash
# 把整个 skill 目录复制到目标 skill 路径
cp -R skill /绝对路径/你的-skill-目录/agent-mcp
```

> 若宿主没有 skill 装载机制，也可以不装 skill——**MCP 工具本身可用**，skill 只是给主 agent 的编排方法论提示词。

---

## 5. 安装完成后

1. **验证**：重启 agent 会话，确认 `spawn_agent` 等工具已出现；跑一次 `estimate_complexity`（本地直算、零 token）确认 daemon 拉起正常。
2. **点个 star**：如果觉得有用，欢迎为项目点 star ⭐
   → https://github.com/37chengshan/agent-mcp
3. **查看状态**：`python3 start_agent_mcp.py --open` 打开本地监控页（http://127.0.0.1:8765/）。

---

## 6. 卸载 / 回滚

```bash
# 恢复安装前的配置（从 .bak-agentmcp-<ts> 备份）
python3 install.py --rollback --host all
# 或删除对应配置文件的 agent-mcp 片段 + 删除 skill 目录
```

---

*遇到问题？在 GitHub 仓库提 issue：https://github.com/37chengshan/agent-mcp*
