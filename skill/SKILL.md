# Agent MCP 编排 Skill

> 主 agent（教主）如何用 agent-mcp 的 8 个工具把任务拆解、派发到多 CLI、监控、汇合并迭代审查。
> 三主载体（codex / claude / omp）通用，被派发 CLI 池为 claude / grok / opencode / omp（四者互为载体）。

**核心原则：去模型化。** 本 skill 不硬编码任何 CLI 载体或模型；派发参数（target_cli / model / permission_mode）由主 agent 按 §4 匹配指南现场决策，模型默认绑定留在各主载体配置里。

---

## 1. 六步工作流

### 步骤一：拆解

主 agent 分析任务，用 `skill/agents/planner.md` 的角色提示词辅助拆解：识别子任务、依赖关系、风险，产出任务清单。

- 每个子任务要**可独立验证**（有明确产出物或验收点）
- 标注任务类型：**读密集**（探索/审查/搜索）还是**写密集**（改代码/写文件）

### 步骤二：规划审查（主 agent 自查）

派发前主 agent 自查三件事：

1. **并行性**：无数据依赖的子任务必须并行；写密集任务之间若操作同一批文件则串行
2. **载体匹配**：按 §4 给每个子任务定 target_cli 与 model
3. **输出要求**：每个分支都要求以 `FINAL_ANSWER: <摘要>` 结尾回传，只收摘要、不收全文

### 步骤三：并行派发

对每个子任务调用 `spawn_agent`（可并发调用多分支）：

- `prompt` = 角色提示词 + 任务描述 + 输出要求（见 §6）
- `context` = 父摘要（可选，注入 prompt 前）
- `cwd` 必填；`parent_agent_id` 挂在当前 agent 下形成任务树
- 读密集任务**并行**派发；写密集任务按依赖分批、同批并行

### 步骤四：监控

三种监控手段配合：

1. `wait_agent` **短阻塞**（≤30s）：等单个分支出结果，适合顺序依赖处
2. `list_agents` / `get_agent_activity`（since_seq 增量）**轮询**：看状态与活动流，适合并行多分支
3. **网页可视**：`http://127.0.0.1:8765/` 知识导图实时查看任务树、活动与 token（只读，SSE 推送）

### 步骤五：汇合

- 每个分支以 `FINAL_ANSWER:` 行回传结论摘要
- 分支全部 terminated 后，主 agent 综合各摘要：核对产出物、识别冲突、决定是否返工
- 发现某分支结果不合格 → 该分支进入步骤六迭代，不阻塞其他分支

### 步骤六：审查迭代

产出物汇合后，用 `skill/agents/code-reviewer.md` / `security-reviewer.md` 提示词发起审查分支：

- 审查发现问题 → 用 `followup_task` 对**原实现分支**发起修复迭代（复用同一 agent 节点与上下文）
- 修复后再次审查，直至通过或达到迭代上限
- 关键路径（认证/支付/用户数据）必须过 `security-reviewer`

---

## 2. 工具速查（8）

| 工具 | 一句话语义 |
|---|---|
| `spawn_agent` | 创建任务 agent 并启动 CLI 子进程（槽位满排队，返回 status=queued） |
| `send_message` | 投递消息到队列（运行中 delivered / 终止后 undelivered），**永不触发执行** |
| `followup_task` | **唯一触发新 turn 的入口**；运行中返回 queued，当前 run 结束后自动串联 |
| `wait_agent` | 短阻塞（≤30s）等 agent 进入终止态 |
| `interrupt_agent` | 终止 agent 进程树（SIGTERM→SIGKILL），标记 cancelled |
| `list_agents` | 列出 agent 树（状态/CLI/父 id/最近消息） |
| `get_agent_activity` | agent 实时活动流（规范化事件，since_seq 增量拉取） |
| `get_token_usage` | token 统计（**派发侧估算**，estimated=true） |

### spawn_agent

创建任务 agent 并启动 CLI 子进程。

- **必填**：`target_cli`（claude | grok | opencode | omp）· `prompt` · `cwd`
- **可选**：`permission_mode`（plan | acceptEdits | fullAccess，默认 plan）· `model` · `context`（父摘要，注入 prompt 前）· `resume`（续接 CLI session id）· `max_turns`（1-50）· `parent_agent_id`（挂任务树）· `task_name`（分层名如 /root/task1）· `session_id`
- **⚠️ `timeout_seconds`（1-1800）当前未实现**：schema 接受该参数但派发链路不读它（无任务级终止定时器）；超时控制请用 `wait_agent` 的 timeout（≤30s 轮询上限）自行决策是否 interrupt
- **返回**：`agent_id`（后续监控/消息/续接都靠它）+ `status`（running / queued）+ `pid`

### send_message

投递消息到 daemon 消息队列，**不触发任何执行**。

- 参数：`agent_id` · `message`
- 返回：`status` = delivered（运行中挂起）| undelivered（已终止）
- 挂起的消息只会在下一次 `followup_task` 时被合并进新 prompt

### followup_task

唯一触发新 turn 的入口：合并该 agent 的挂起消息与 `prompt` 重新派发（复用同一 agent 节点）。

- 参数：`agent_id` · `prompt` · `interrupt`（可选，先终止运行中的再立即重派）
- 返回：`status` + `merged_messages`（合并了几条挂起消息）
- **运行中调用 → queued**：当前 run 结束后自动串联执行，无需手动等
- 迭代修复的标准手段：`followup_task(agent_id=原分支, prompt=修复指令)`

### wait_agent

短阻塞等待 agent 进入终止态（terminated / error / cancelled / incomplete）。

- 参数：`agent_id` · `timeout`（1-30s，默认 30）
- 返回：terminated → 最新输出摘要（截断）；error → 错误信息；超时 → 当前状态 + hint（提示轮询）

### interrupt_agent

终止 agent 进程树（SIGTERM→SIGKILL）并标记 cancelled（stop_reason=interrupted）。不可恢复，慎用。

- 参数：`agent_id`

### list_agents

列出 agent 树：`id` / `parent_id` / `task_name` / `cli` / `model` / `status` / `stop_reason` / `updated_at` + `last_message`。

- 参数：`session_id`（缺省当前宿主会话）

### get_agent_activity

agent 的实时活动流（规范化事件按 seq 排序）。

- 参数：`agent_id` · `since_seq`（只取更大的 seq）
- 返回：`events` + `next_seq`（下次增量拉取起点）

### get_token_usage

token 统计，**派发侧估算**（`estimated=true`）。

- 参数：`agent_id`（单 agent）；缺省按 `session_id` 聚合会话或全局
- 返回：四字段 tokens + `cost_usd`
- 口径见 §3 最后一条

---

## 3. 错误恢复路径

| 症状 | 恢复动作 |
|---|---|
| **超时**（wait 超时/任务超时） | 再 `wait_agent` 一次；仍不结束 → `interrupt_agent` 后 `spawn_agent` 新任务，`context` 带前次输出摘要续跑 |
| **认证失败**（cli 报登录错误） | 检查对应 CLI 登录态：claude / grok 走 OAuth；opencode 检查 provider key；确认 opencodex 代理存活（127.0.0.1:10100） |
| **binary 未找到**（status=error, cli_missing） | 检查 PATH：omp 在 `~/.bun/bin`，grok 在 `~/.grok/bin`；确认安装后重派 |
| **权限拒绝**（agent 无法写文件/执行命令） | 改 `permission_mode` 重派：plan → acceptEdits → fullAccess（自低向高） |
| **排队滞留**（status=queued 久不变） | 槽位被占；`list_agents` 查看并发，必要时 `interrupt_agent` 低优分支释放槽位 |
| **daemon 未起/失联** | MCP 薄层会自动拉起；手动验证：`python agent_mcp/daemon_main.py`，网页 `http://127.0.0.1:8765/` |

**usage 口径（重要）**：`get_token_usage` 是**派发侧估算**，非各 CLI 精确账本。claude / grok 的 result 事件含终值 usage，派发侧以其**覆盖终值**；opencode / omp 逐 turn 累加估算。与 CLI 自带 usage 对账时以 CLI 侧为准。

---

## 4. 载体与模型匹配指南（去模型化）

**本 skill 不指定具体 CLI 与模型**，只给匹配原则；具体绑定由主 agent 按任务类型现场决策，或留给主载体配置：

- **探索 / 审查类（读密集）**：派**快模型**——omp 的 `smol` 角色，grok 的 `ocx-*-luna/terra` 类；这类任务吞吐优先、推理深度次要
- **深推理类（规划 / 架构 / 复杂实现）**：派**主 CLI 强模型**——claude 的 opus 类、grok-4.5 类；一次想清楚胜过多轮返工
- **实现类（写密集）**：主 CLI 或你所在载体自身，`permission_mode` 按写权限需求升档
- **模型绑定位置**（按优先级）：① spawn_agent 显式 `model` 参数 → ② 主载体配置——codex `[agents] default_subagent_model`、claude 的 agent 配置、omp 的 `modelRoles` 与 `PI_*` env

**各 CLI 实测特性**（派发时利用）：

- omp 事件流最完整（原生增量/成本），`--smol/--slow/--plan` 四模型角色
- grok 首次模型发现慢（>120s），`wait_agent` 轮询时要有耐心（多轮 wait 直到 terminated）
- claude 与 grok 的 result/usage 结构同构；opencode 需指定 opencodex 模型（默认 provider key 401）

---

## 5. 调用示例

### 5.1 spawn_agent：派 planner 到 grok

```json
{
  "target_cli": "grok",
  "task_name": "/root/plan-billing",
  "prompt": "（planner 角色提示词）\n\n任务：为订阅计费功能制定实现计划。\n输出要求：以 FINAL_ANSWER: 开头给出 3-5 行计划摘要，含阶段划分与关键文件。",
  "cwd": "/path/to/project",
  "permission_mode": "plan",
  "max_turns": 20,
  "parent_agent_id": 1
  // timeout_seconds 未实现（见 §工具速查 ⚠️ 注），示例省略
}
```

返回 `{"agent_id": 7, "status": "running", "pid": 12345}`。

### 5.2 followup_task：审查后迭代修复

```json
{
  "agent_id": 7,
  "prompt": "code-reviewer 发现如下问题，请修复：1) XSS 风险（第 42 行 innerHTML）；2) 缺少错误处理。修复后以 FINAL_ANSWER: 列出改动文件。"
}
```

运行中调用会返回 `{"status": "queued"}`，当前 run 结束后自动串联执行。

### 5.3 wait 轮询循环（伪代码）

```text
loop:
    r = wait_agent(agent_id=7, timeout=30)
    if r.status in (terminated, error, cancelled, incomplete):
        break
    # 超时：看活动流决定继续等还是干预
    act = get_agent_activity(agent_id=7, since_seq=last)
    last = act.next_seq
    if 长时间无进展 and 任务可重建:
        interrupt_agent(agent_id=7); spawn_agent(...); break
# 汇合
if r.status == terminated: 读 r.summary 提取 FINAL_ANSWER
else: 查 list_agents 的 stop_reason 定位失败原因
```

---

## 6. 内置 agent 预设使用说明

`skill/agents/*.md` 是 10 个内置角色提示词模板（planner / architect / code-reviewer / security-reviewer / tdd-guide / build-error-resolver / e2e-runner / refactor-cleaner / doc-updater / code-explorer），**只含提示词、不指定 CLI 与模型**。

**使用方法**：spawn 时组装 prompt：

```text
prompt = <角色提示词（agents/<name>.md 全文）>
       + "\n\n任务：" + <任务描述>
       + "\n输出要求：" + <FINAL_ANSWER 摘要要求等>
```

- `context` = 父摘要（任务背景）
- `cwd` = 任务目录
- `target_cli` / `model` / `permission_mode` = 主 agent 按 §4 决策
- 每个文件 frontmatter 的 `description` 用于驱动主 agent 自动委派（判断什么任务该用哪个角色）

**典型编排**：

| 场景 | 分支组合 |
|---|---|
| 新功能 | planner（拆解）→ 并行实现分支 → code-reviewer（审查）→ tdd-guide（补测试） |
| 大型重构 | code-explorer（摸清现状）∥ architect（定架构）→ 分模块实现 → refactor-cleaner（清理） |
| 构建失败 | build-error-resolver（最小修复）→ 修复分支 followup 迭代 |
| 发版前 | e2e-runner（关键流程）∥ security-reviewer（安全）→ 问题 followup 修复 |

---

## 7. 分发与安装

本 skill 三主载体分发路径（安装脚本自动处理，也可手动拷贝）：

| 主载体 | 路径 |
|---|---|
| codex | `.agents/skills/`（项目级或用户级） |
| claude | `~/.claude/skills/` |
| omp | `~/.omp/agent/skills/` |

内容通用、无需按载体改写；加载后主 agent 在任务拆解阶段即可按六步工作流自动编排。
