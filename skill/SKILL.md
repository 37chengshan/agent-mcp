---
name: agent-mcp
description: Use when a task should be decomposed and dispatched across claude, grok, opencode, omp, or atomcode workers, especially for parallel execution, mid-run steering, durable follow-ups, task monitoring, or multi-CLI result synthesis.
---

# Agent MCP 编排 Skill

主 agent 负责拆解、并行度、权限、模型和验收；Agent MCP 只提供可靠派发、监控、续接与终止基础设施。不要把拆解或文件冲突决策交给 MCP。

## 1. 编排五步（主 agent 的工作）

**第一步：拆解规划**
把任务拆成可独立验证的子任务（有明确产出物或验收点），用 `skill/agents/planner.md` 辅助。每个子任务标注：**读密集**（探索/审查/搜索）还是**写密集**（改代码/写文件）。

**第二步：判定并行（关键）**
- 无数据依赖的子任务 → **并行**派发
- 写同一批文件的子任务 → **串行**（或按依赖分批、同批并行）
- 有依赖的 → 父任务先跑，产出作 `context` 传给子任务

**第三步：MCP 式派发**
对每个子任务调 `spawn_agent`（可并发调用多个）：
- `target_cli` 按 §3 匹配；`model` 现场决策（去模型化，不硬编码）
- `prompt` = 角色提示词 + 任务描述 + 输出要求（要求以 `FINAL_ANSWER: <摘要>` 结尾回传）
- `cwd` 必填；`parent_agent_id` 挂到当前任务树；`permission_mode` 默认 plan，写文件才升档

**第四步：监控**
- 并行多分支：`list_agents` / `get_agent_activity`（since_seq 增量）轮询
- 顺序依赖处：`wait_agent`（短阻塞，timeout 可自定义，默认 30s、上限 600s）
- 中途改向：`steer_agent`（运行中先终止当前 run，再在同一节点立即续接；稳定 session id 的 CLI 自动恢复原会话）
- 网页操作台由 `start_agent_mcp.py --open` 打开：横向 Conversation Graph 显示 `agent.user_turn` 输入节点与本轮 agent/子代理；写授权只经 URL fragment 传入，页面读取后立即清除。

**第五步：汇合与迭代**
- 分支以 `FINAL_ANSWER:` 回传摘要；主 agent 综合核对、识别冲突、决定返工
- 不合格分支 → `followup_task`（复用同一 agent 节点）迭代修复，不阻塞其他分支
- 关键路径（认证/支付/数据）必须过 `security-reviewer`

## 2. 工具速查（9）

| 工具 | 用途 |
|---|---|
| spawn_agent | 派发新 agent（立即返回 agent_id + status；槽位满返回 queued） |
| send_message | 投递消息到队列，不触发执行 |
| steer_agent | 中途插话：先终止当前 run，再在同一节点立即开始下一 turn；稳定 session id 的 CLI 自动恢复原会话 |
| followup_task | 唯一触发新 turn 的入口：合并挂起消息重新 spawn（复用同一 agent 节点）；运行中返回 queued，当前 run 结束后自动串联；interrupt=true 先终止再重派；返回 merged_messages |
| wait_agent | 短阻塞等 agent 终止（timeout 可自定义，默认 30s、上限 600s） |
| interrupt_agent | 终止进程树（不可恢复，慎用） |
| list_agents | 列 agent 树（状态/CLI/父 id/最近消息） |
| get_agent_activity | 实时活动流（since_seq 增量） |
| get_token_usage | token 统计（派发侧估算；AtomCode 从 `-v` 的 `[tokens] prompt=… completion=… cached=…` 解析） |

**协议**：兼容 legacy MCP 2025-03-26 与 modern 2026-07-28。modern 客户端声明
`io.modelcontextprotocol/tasks` 扩展时，spawn_agent 返回持久 task 句柄；tasks/get 轮询状态、
tasks/update 把接受的 input response 作为 steer 内容、tasks/cancel 中断任务；daemon 错误
原样映射为 JSON-RPC error，legacy 客户端继续走普通工具结果。

## 3. 派发决策（去模型化）

- **读密集**（探索/审查）→ 快模型：omp `smol`、grok luna/terra 类
- **深推理**（规划/架构）→ 强模型：claude opus 类、grok-4.5 类
- **写密集**（实现）→ 主载体自身或默认，`permission_mode` 升档
- 模型绑定优先级：spawn_agent 显式 `model` → 主载体配置（codex / claude agent 配置 / omp `modelRoles`）
- 各 CLI 特性：omp 事件流最全；AtomCode 仅作任务目标（task-only，不是安装载体）且是 one-shot（不支持稳定 session-id resume，`model` 必须传纯 API 名如 `deepseek-v4-flash`，`provider/模型` 形式与本地 catalog 键都会 403，且别显式传 provider）；grok 首次模型发现慢（>120s）；opencode 需指定 opencodex 模型

## 4. 关键约定

- 分支只回传 `FINAL_ANSWER:` 摘要，不收全文
- `cwd` 必填；任务间避免写同一批文件
- `session_id` 是所有 agent ID 操作的所有权边界；宿主调用自动注入，不能用另一会话的 agent ID
- `timeout_seconds`（spawn/followup，1–1800）是任务级超时：终止进程树并标记 `incomplete/timeout`；`wait_agent.timeout` 只是轮询阻塞上限
- usage 为派发侧估算；AtomCode 的 verbose token 行会被解析为 `agent.usage`，不会污染最终消息

## 5. 错误恢复

| 症状 | 动作 |
|---|---|
| 超时 | 任务级：spawn/followup 传 `timeout_seconds` 自动终止（incomplete/timeout，可 resume）；轮询超时 → 再 wait 一次；仍不结束 → interrupt + 重派（context 带前次摘要） |
| 认证失败 | 查登录态：claude/grok OAuth；opencode provider key；opencodex 代理 (127.0.0.1:10100) |
| AtomCode 403 model not enabled | `model` 传纯 API 名（`deepseek-v4-flash`），别带 provider 前缀/catalog 键，别显式 provider |
| binary 未找到 | 查 PATH：omp `~/.bun/bin`，grok `~/.grok/bin`，AtomCode `~/.local/bin/atomcode` |
| 权限拒绝 | `permission_mode` 升档：plan → acceptEdits → fullAccess |
| 排队滞留 | 槽位被占；interrupt 低优分支释放 |
| daemon 失联 | MCP 自动拉起；手动 `python agent_mcp/daemon_main.py`；网页 8765 |

## 6. 内置角色预设

`skill/agents/*.md` 共 10 个角色提示词（planner / architect / code-reviewer / security-reviewer / tdd-guide / build-error-resolver / e2e-runner / refactor-cleaner / doc-updater / code-explorer），**只含提示词、不指定 CLI 与模型**。spawn 时把对应文件内容拼进 `prompt`，`target_cli` / `model` / `permission_mode` 由主 agent 按 §3 决策。
