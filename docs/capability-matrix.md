# 四 CLI 能力矩阵（实测记录）

> 更新原则：每项标 ✅=已实测 / ⏳=待实测。实现 Task 0 时逐项确认。

| 能力 | claude (2.1.220) | grok (0.2.118) | opencode (1.14.51) | omp(pi) |
|---|---|---|---|---|
| 事件流格式 | `-p --output-format stream-json --verbose` ✅ | `--single --output-format json/streaming-messages-json` ✅ | `run --format json` ⏳ | ⏳（headless 模式未知） |
| result 结构 | `stop_reason` / `session_id` / `total_cost_usd` / `usage`(input/cache_creation/cache_read/output) / `modelUsage`(按模型拆分含 costUSD) ✅ | `stopReason` / `sessionId` / `requestId` / `usage`(input/cache_read/cache_creation/output/reasoning/total) / `modelUsage`(按模型) / `num_turns` ✅ —— 与 claude **同构** | ⏳ | ⏳ |
| 流式输入（运行中注入） | `--input-format stream-json` ✅ | ⏳ | SDK SSE ✅ / CLI ⏳ | ⏳ |
| resume / 会话续接 | `--resume session_id` ✅ | `--resume <session_id>`（-c 续最近会话）✅ | ⏳ | ⏳ |
| 权限模式 | plan / acceptEdits / `--dangerously-skip-permissions` ✅ | plan / acceptEdits / bypassPermissions + `--always-approve` ✅ | `-m plan` / allow 规则 / `-y` ⏳ | ⏳ |
| 首启耗时 | 快（~3s）✅ | 慢（首次模型发现 >120s，之后快）✅ | ⏳ | ⏳ |
| 子代理控制 | `--agents <json>` / `--no-subagents` 未知 | `--agent <name>` / `--agents <json>` ✅ | 内置 subagent | ⏳ |
| Windows 二进制 | npm shim ⏳ | ⏳ | npm shim ⏳ | ⏳ |

## 关键结论

1. **claude 与 grok 的 result/usage 结构同构**（stopReason/sessionId/usage 四字段 + modelUsage 按模型拆分）→ 一个解析器覆盖两 CLI（适配器层各自归一化即可）
2. grok 的 usage 含 `reasoning_tokens`（claude 无）——归一化时忽略未知字段
3. grok 首次初始化慢（模型发现）：spawn 时 timeout 预算需预留（>120s），或常驻预热
4. 三主载体注册：codex config.toml ✅（已知）；claude .mcp.json ✅（已知）；omp `~/.omp/agent/` MCP client 配置 ⏳（日志已确认支持 MCP 加载）
