# Agent MCP

**跨 CLI 的多 Agent 编排基础设施** —— 在一个 MCP 协议内，把 claude / grok / opencode / omp / atomcode 五个 CLI 统一为可派发、可监控、可续接、可终止的子 Agent 工作池，让主 Agent 只做拆解与汇合，执行与容错交给 Agent MCP。

> 多 Agent 编排比单线程多耗 3–10× tokens（Anthropic 实测）。Agent MCP 的价值不是"多开几个 Agent"，而是把拆解的收益锁住、把协调的开销压到最低：**复杂度分级门**决定"要不要拆"，**任务级超时 / 队列 / 续接 / 降档**兜住"拆了怎么办"。

---

## ✨ 特性

| 能力 | 说明 |
|---|---|
| 🧩 **五 CLI 统一派发** | `spawn_agent` 一个入口派发 claude / grok / opencode / omp / atomcode；适配器层各自归一化事件流、usage 与 session，上层无感 |
| 🚦 **复杂度分级门** | `estimate_complexity` 本地直算（零 token、不 spawn），按 S/M/L 判级决定是否进入编排——**默认直接做，按需才拆**，杜绝过拆 |
| ⏱️ **任务级超时** | `timeout_seconds`（1–1800s）到时终止整个进程树并标记 `incomplete/timeout`，可 resume 续跑；不等死、不悬空 |
| 🔁 **可续接可插话** | `resume` 透传 CLI session id；`steer_agent` 中途插话、`followup_task` 合并挂起消息重派；同一 agent 节点复用，上下文不丢 |
| 📦 **排队与并发** | 槽位满自动 `queued`，当前 run 结束后自动串联；无数据依赖的子任务可并行派发 |
| 🎯 **验证回投** | `verify_command` + `max_fix_attempts`：daemon 自跑验证，失败自动同 session 回投修复，只把最终结果交回主 Agent |
| 💰 **成本控制** | `token_budget` 超额自动降档 model 重跑；`cache_ttl` 读密集结果秒级缓存（TTL 内 0 token）；`summary_chars` / `context_mode` 裁剪回传体积 |
| 🔐 **会话隔离** | session_id 是所有权边界：宿主注入的稳定会话标识派生，同一对话重开 MCP 连接旧 agent 仍可用，跨会话不可互操作 |
| 📊 **实时监控页** | 单文件、零外部依赖的只读 Web UI（SSE 直播事件流 + 对话图 + 明暗主题），daemon 随手起，`GET /` 实测 5ms |
| 🛠️ **一键安装** | `install.py` 同时注册 codex / claude / omp 三主载体，装 skill 与 SessionStart hook；写配置前自动备份、`--rollback` 可回滚、`--dry-run` 只预览 |

---

## 🏗️ 架构

```
┌──────────────────────────────┐        ┌──────────────────────────────┐
│  主 Agent (codex / claude /  │        │   监控页 (单文件只读 Web UI)  │
│          omp ...)            │        │    http://127.0.0.1:8765/    │
└──────────────┬───────────────┘        └──────────────▲───────────────┘
               │ MCP stdio (零依赖薄层)                  │ SSE 事件流
               ▼                                        │
┌──────────────────────────────┐        ┌──────────────────────────────┐
│   mcp_server.py (无状态)      │        │   daemon_main.py (常驻 daemon) │
│   · 9 个编排工具              │  HTTP  │   · 槽位 / 排队 / 心跳 / 看护   │
│   · host 识别 + 会话隔离      │ ─────► │   · 验证回投 / 降档 / 缓存      │
│   · 无 daemon 时原子拉起      │  X-Auth │   · SQLite 状态机持久化         │
└──────────────────────────────┘   Token └──────────────┬───────────────┘
                                                        │ subprocess
                    ┌───────────────┬────────┬──────────┼──────────┐
                    ▼               ▼        ▼          ▼          ▼
              ┌──────────┐  ┌──────────┐ ┌────────┐ ┌────────┐ ┌──────────┐
              │  claude  │  │   grok   │ │opencode│ │  omp   │ │ atomcode │
              │  worker  │  │  worker  │ │ worker │ │ worker │ │  worker  │
              └──────────┘  └──────────┘ └────────┘ └────────┘ └──────────┘
                 统一事件流归一化（agent.spawned → running → message/usage → terminated）
```

完整架构图见 [docs/architecture.svg](docs/architecture.svg)，编排流程见 [docs/workflow.svg](docs/workflow.svg)。

---

## 🚀 快速开始

```bash
# 1. 克隆
git clone git@github.com:37chengshan/agent-mcp.git && cd agent-mcp

# 2. 一键注册三主载体（codex / claude / omp）+ 安装编排 skill
python3 install.py --install --host all --server "$PWD/mcp_server.py"

# 3. 幂等启动 daemon（已运行则跳过），--open 打开监控页
python3 start_agent_mcp.py --open
```

> `--dry-run` 先看将写入的配置；`--legacy-map` 查看旧 grok-cli 9 工具 → 新工具迁移表；误改配置用 `--rollback` 从备份恢复。
> daemon 端口 / 状态目录可调：`AGENT_MCP_PORT=8765`、`AGENT_MCP_HOME=~/.codex`（默认）或 `CODEX_HOME`。

---

## 🛠️ 工具总览（MCP）

| 工具 | 用途 |
|---|---|
| `estimate_complexity` | 本地判级 S/M/L + 是否委派建议（零 token、不 spawn） |
| `spawn_agent` | 派发新 agent（立即返回 agent_id + status；槽位满返回 queued） |
| `send_message` | 投递消息到队列，不触发执行 |
| `steer_agent` | 中途插话：先终止当前 run，再在同一节点立即开始下一 turn |
| `followup_task` | 唯一触发新 turn 的入口：合并挂起消息重新 spawn（可 interrupt） |
| `wait_agent` | 短阻塞等待终止态（默认 25s / ≤600s），返回摘要 + 存活证据 hint |
| `interrupt_agent` | 终止运行中的 agent（终止进程树） |
| `list_agents` | 列出任务树 agent（可含其他会话，找回旧 agent 状态） |
| `get_agent_activity` | 事件流水（spawned/running/message/usage/terminated…） |
| `get_token_usage` | 累计 token / 成本对账 |

---

## 🧑‍💻 编排 Skill（开箱即用）

`skill/` 随安装分发到各主载体，提供**六步工作流** + 10 个内置 Agent 预设：

- **编排五步**：拆解规划 → 判定并行（认知局部性优先）→ MCP 式派发 → 监控（wait 循环，不轮询）→ 汇合自审
- **复杂度分级门**：S/M/L 判级 + 不委派清单（命中即禁止 spawn）
- **内置 Agent**：planner / architect / tdd-guide / code-reviewer / security-reviewer / build-error-resolver / e2e-runner / doc-updater / refactor-cleaner / code-explorer
- **任务简报六要素**：目标 / 工作范围 / 边界 / 自审级别 / 输出契约（`FINAL_ANSWER: <摘要>`）/ 卡住升级（BLOCKED / NEEDS_CONTEXT / NEEDS_DECISION）
- **本地等待脚本**：`skill/scripts/wait_agent.py <agent_id>` 一次跑完阻塞到终态，省掉频繁 MCP 往返

---

## 📦 项目结构

```
mcp_server.py            # MCP 薄层：工具定义、host 识别、会话隔离、daemon 原子拉起
agent_mcp/
  daemon_main.py         # 常驻 daemon：Dispatcher、槽位/排队/心跳/看护、验证回投、SSE
  cli_adapters.py        # 五 CLI 适配器（命令构造 + 事件流归一化）
  state_machine.py       # agent 状态机（starting/running/terminated/error/…）
  db.py                  # SQLite 持久化（agent/事件/usage）
  daemon_http.py         # HTTP 路由 + X-Auth-Token 认证
dispatch_worker.py       # 子进程 worker（超时终止进程树）
install.py               # 三主载体注册 + skill 安装 + SessionStart hook + 备份回滚
start_agent_mcp.py       # 幂等启动 daemon（可选打开监控页）
web/index.html           # 单文件零依赖只读监控页（SSE + 对话图 + 明暗主题）
skill/                   # 编排 skill + 10 内置 Agent + 任务简报模板
docs/                    # 验收清单 / 能力矩阵 / 设计文档
tests/                   # 20+ 测试文件（含真实 stdio 与 CLI 集成冒烟）
```

---

## ✅ 质量

- **实测**：claude / omp 全链路冒烟通过（spawn → wait → interrupt → usage）；grok / opencode 适配器层单测覆盖
- **验收**：`docs/acceptance.md` 对照设计文档逐项核对（✅ / ⚠️ / ⏳ 带证据）
- **测试**：`python3 -m pytest tests/ -q`（含 9 个集成用例端到端冒烟）
- **零依赖**：核心 + 安装 + 监控页全部 stdlib / 单文件，无外部资源

## 📚 文档

- [验收清单](docs/acceptance.md) · [四 CLI 能力矩阵](docs/capability-matrix.md)
- [设计文档](docs/plans/2026-08-03-agent-mcp-redesign-design.md) · [实现计划](docs/plans/2026-08-03-agent-mcp-implementation.md)
- [编排 Skill 全文](skill/SKILL.md)
