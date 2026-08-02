# 四 CLI 能力矩阵（实测记录）

> 更新原则：每项标 ✅=已实测 / ⏳=待实测。实现 Task 0 时逐项确认。

| 能力 | claude (2.1.220) | grok (0.2.118) | opencode (1.14.51) | omp(pi) |
|---|---|---|---|---|
| 事件流格式 | `-p --output-format stream-json --verbose` ✅ | `--single --output-format json/streaming-messages-json` ✅ | `run --format json` ✅（事件带 `type` + `sessionID` 字段；默认 provider key 401 失效，需指定 opencodex 模型） | `-p --mode=json` ✅（`/Users/cc/.bun/bin/omp` v17.2.4） |
| result 结构 | `stop_reason` / `session_id` / `total_cost_usd` / `usage`(input/cache_creation/cache_read/output) / `modelUsage`(按模型拆分含 costUSD) ✅ | `stopReason` / `sessionId` / `requestId` / `usage`(input/cache_read/cache_creation/output/reasoning/total) / `modelUsage`(按模型) / `num_turns` ✅ —— 与 claude **同构** | 事件流（message/tool/error/done），usage 字段待实测 | `session`(id)/`agent_start`/`turn_start`/`message_start`(assistant 含 usage{cost.total} + stopReason + model + responseId)/`message_update`(text_delta 增量)/`message_end` ✅ |
| 流式输入（运行中注入） | `--input-format stream-json` ✅ | ⏳ | SDK SSE ✅ / CLI ⏳ | `--mode=rpc` 双向 ✅（rpc-ui 亦可） |
| resume / 会话续接 | `--resume session_id` ✅ | `--resume <session_id>`（-c 续最近会话）✅ | ⏳ | `--profile` 隔离 / session id（待实测 resume flag） |
| 权限模式 | plan / acceptEdits / `--dangerously-skip-permissions` ✅ | plan / acceptEdits / bypassPermissions + `--always-approve` ✅ | `-m plan` / allow 规则 / `-y` ⏳ | `--plan-yolo` / `--allow-home`（权限机制待实测） |
| 首启耗时 | 快（~3s）✅ | 慢（首次模型发现 >120s，之后快）✅ | ⏳ | 快（~5s）✅ |
| 子代理控制 | `--agents <json>` / `--no-subagents` 未知 | `--agent <name>` / `--agents <json>` ✅ | 内置 subagent | `--smol/--slow/--plan` 模型角色 + 未知 |
| 模型角色 | `--model` 单模型 | `-m` 单模型 | `--model` 单模型 | `--model/--smol/--slow/--plan` 四角色（env PI_*），默认 deepseek-v4-pro via opencodex ✅ |
| Windows 二进制 | npm shim ⏳ | ⏳ | npm shim ⏳ | bun 安装 ⏳ |

## 关键结论

1. **claude 与 grok 的 result/usage 结构同构**（stopReason/sessionId/usage 四字段 + modelUsage 按模型拆分）→ 一个解析器覆盖两 CLI（适配器层各自归一化即可）
2. **omp 的事件流最完整**：`message_start` 自带 usage（含 cost.total 成本）+ stopReason + model + `message_update.text_delta` 原生增量 → 打字机预览零成本；`--mode=rpc` 支持双向注入
3. grok 的 usage 含 `reasoning_tokens`（claude 无）；omp 的 cache 字段是 cacheRead/cacheWrite（非 cache_read/cache_creation）——归一化时各自映射，忽略未知字段
4. grok 首次初始化慢（模型发现）：spawn 时 timeout 预算需预留（>120s），或常驻预热
5. 三主载体注册：codex config.toml ✅（已知）；claude .mcp.json ✅（已知）；omp `~/.omp/agent/` MCP client 配置 ⏳（日志已确认支持 MCP 加载）
6. omp 二进制在 `~/.bun/bin/omp`（bun 安装）——Windows 安装路径待实测
