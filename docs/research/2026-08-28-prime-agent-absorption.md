# prime-agent 研读报告（agent-mcp v4 吸收基线）

> 日期：2026-08-28 · 对象：https://github.com/PrimeIntellect-ai/prime-agent（MIT License）
> 方式：本会话浅克隆至 /tmp/prime-agent 深度研读（README/AGENTS.md/architecture.md/daemon.md/agent-connection.md/rlm-runtime.md/long-running-agents.md/rlm.md/acp.md + prime-agent-runtime/src/rlm/*）。
> 立场：**仅设计吸取**。不整体搬代码；任何复用（如 repl 协议文档/片段）须保留 MIT 版权声明并补 LICENSE。

## 1. 项目画像

Prime Agent = 基于 pi（earendil-works）的开源 coding & research agent，TypeScript monorepo（packages/agent、ai、coding-agent、tui）+ Python prime-agent-runtime（rlm 包）。核心卖点：自改进 RLM（Recursive Language Model）harness——持久 Python REPL 是模型的内置工具；rlm(...) 程序化子 Agent 调用；/refine 更新 continual harness 状态（prompts/memories/skills/subagent specs，本地默认/全局显式，快照可回滚，base prompt 不可变）；daemon-backed 后台会话、直接 agent-to-agent 消息、goals/heartbeats/schedules/autonomous、RPC/JSON/ACP headless 模式、installer 带 SHA-256 与 doctor/update。

## 2. 架构要点（与 agent-mcp 的对照）

### 2.1 Daemon Architecture（daemon.md）
- supervisor（公共 socket/attach/路由/worker 健康/命令日志/协调更新）与 worker（每个 root session tree 一进程，owner=worker；TUI 关闭不杀 worker）分离。
- session lease：进程安全的路径级 lease，防并发写同一 transcript；并发打开返回 session_already_active + 拥有者 id。
- 调度：每 worker 一个 scheduler；scheduled jobs 按 session 持久化；due tick 先 claim 再投递（crash 不重放不确定 prompt）；错过 tick 合并不堆积。
- Public Daemon Protocol v4：versioned envelope（stable client/command id）、capability negotiation、generation-aware 事件游标 {generation, sequence}、attach resume cursor + 快照（begin/chunk/end、512KiB chunk、>4MiB 落盘缓存）、幂等命令日志（重复完成返回已记录结果；不确定不重放；客户端 ack 压缩日志）、backpressure attachment-local、两阶段协调更新、worker 代际令牌防旧 supervisor 指挥。
- 职责边界：supervisor 不执行 providers/tools/compaction/bash/kernels；catalog 子进程只扫保存会话。

### 2.2 Agent Connection（agent-connection.md）
- 客户端边界：UI（渲染/键盘/本地偏好）不拥有执行；DaemonAgentConnection 持 latest snapshot + last cursor + 快照组装 + 重连。
- 重连：命令 clientId+commandId；事件 {generation, sequence}；attach 接受 resume cursor；重连同 identity；恢复后 session_resynced；**快照是持久恢复基线，replay 仅是优化**。
- 幂等：mutating 命令记录先于派发；重复完成命令返回存储结果；缺持久结果的命令报 uncertain 不重放；ack 压缩日志。
- 扩展 UI 边界：仅可序列化 UI 请求跨边界；可执行回调绝不跨连接（local extensions 是信任代码）。

### 2.3 RLM Runtime（rlm-runtime.md / rlm.md / prime-agent-runtime/src/rlm/）
- 每个会话一个持久 Python REPL kernel（lazy 创建；managed venv；newline-JSON over stdio）。
- 协议：request（execute/interrupt/host_reply/snapshot/restore/list_names/shutdown）+ event（ready/stdout/stderr/result/display/host_request/error/done）；输出按 cell id 归因（asyncio task 继承 spawn cell id）。
- host_request 双向桥：cell 里 await rlm(run) → kernel 发 host_request → host 首诊（深度/模型/registry）→ 立即回 RLMSpawnHandle（admission 即返回，不含答案）；答案经 agent_message 或文件回传。
- 子 Agent：parent-scoped registry（跨 kernel 重启/compaction/restore 存活）；深度上限默认 2；usage 归因折叠到父 turn（child_usage_attributed entry）；子会话文件（session-artifacts/<root>/sub-xxxxxxxx/）。
- 信任边界：kernel 进程隔离生命周期，不是安全沙箱；凭据由 host 解析，认证存储不跨入 kernel。

### 2.4 Continual Harness（rlm/harness.py + /refine）
- HarnessEntry：id/kind（prompt/memory/skill/subagent）/title/content/path/scope(local|global)/reference/arguments/metadata/source/version/时间戳。
- /refine：对当前轨迹的专用 review，应用小规模 create/update/delete；before/after 快照支持 rollback；base system prompt 不可变；局部状态在 session artifact dir，global 在 ~/.prime/agent/harness/；文件后改自动重载避免 kernel 与 host 互相覆盖。

### 2.5 Long-Running（long-running-agents.md）
- resident worker（detach 不杀）；prime-agent list/attach/rename/stop/status/doctor/shutdown --force。
- agent-to-agent：agent_message.send（receiver_role=parent/sibling/child；mode auto/steer/follow_up；receipt delivered/queued；广播仅 family roster；daemon 强制 size/rate/queue 限制）。
- 三类调度面：用户 /heartbeat；agent rlm_heartbeat（多条、可管理）；general schedule（one-time/cron，per-session 持久化，claim-before-delivery）。
- Persistent goals：/goal create|status|pause|resume|complete（设置 token 预算）；goal 状态记录 usage/elapsed/continuation count；仅 goal.complete() 标记成功；创建 goal 是显式行为。
- Autonomous：有界宿主策略（continuations/turns/tokens/wall-clock 预算 + quality gates）；gate 失败返回受限输出再尝试；**工作区未变不重跑同一失败 gate**；goals 与 autonomous 互补（goal 存目标与进度；autonomous 决定是否注入下轮）。
- Compaction：溢出时总结旧消息保留近期；kernel 跨 compaction 存活；compaction 不是完成信号。

## 3. 吸收映射（→ agent-mcp v4 落点）

| prime-agent 概念 | agent-mcp v4 落点 | 归属层 |
|---|---|---|
| Persistent goals | goals 表 + goal_* 工具 + ExecutionManager 续播（只产 Intent） | Control Plane |
| Schedule/Heartbeat（claim-before-delivery） | schedules 表 + schedule_* 工具 + tick claim 原子化 | Control Plane |
| Bounded autonomous（预算 + gate 指纹） | AutonomousPolicy 快照于 Run；工作区指纹 gate | Control Plane |
| Continual harness + /refine | harness_items/revisions + refine 三段式管道（reviewer 经 ExecutionManager） | Control Plane |
| agent-to-agent messaging（mode/delivery 语义） | mailbox 现有 + 投递模式对齐（auto/steer/follow_up） | Control Plane |
| Daemon 可靠性（幂等日志/代际游标/快照/协商/backpressure） | Control Protocol：/api/v4/command + journal + {generation,seq} + snapshot + /api/protocol | Control Plane |
| RPC / JSON headless | PrimeAgentRPCAdapter（LF JSONL） | Backend Adapter |
| ACP mode | PrimeAgentACPAdapter（gate） | Backend Adapter |
| RLM kernel stdio 协议 | **不吸收**（v4.1+ RFC 候选；避免 Control Plane 扩张成 Runtime） | 排除 |
| TUI/provider/auth/models | 不吸收（跨厂商定位正交） | 排除 |

## 4. 许可证与引用

prime-agent 为 MIT License。agent-mcp 仅借鉴架构/协议设计（本文档所列）；若后续实现中复用其 repl 协议文档文本、字段命名或代码片段，须在对应文件中保留 MIT 版权声明，并在仓库根补 LICENSE（当前仓库无 LICENSE 文件）。

## 5. 后续注视项

- MCP 2026-07-28 Tasks 官方扩展的实际宿主消费进展（跟踪 v3.1 ACP 准入判定）。
- prime-agent RPC/ACP 协议细节以用户环境版本为准（adapter 能力三态必须来自真实协商）。
- daemon protocol benchmark（daemon-multiclient-bench）方法可借鉴到 agent-mcp P1 验证。