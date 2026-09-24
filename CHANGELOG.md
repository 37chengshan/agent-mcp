# Changelog

本文件记录 agent-mcp 的用户可见变更。版本号自 v3.0 起以 `agent_mcp/__init__.py`
的 `__version__` 为单一来源（此前 README 的 v1.0.0、协议层 SERVER_VERSION 2.2.0、
内部代号 v0.x 三套口径并存，已在 v3.0 统一）。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

主题：深度缺陷清偿 + 控制台交互/动效 + 安全加固 + DSH 插件市场上架包。

### Added
- **DSH bundle 插件** `packages/dsh-plugin`（`dsh-plugin-agentmcp`）：官方 `dsh.bundle.patch` 形态，`dsh plugin add` 一键装，`mcp__agentmcp__*` 工具面；跨平台 launcher（`scripts/launch-agentmcp.mjs`）；契约测试 `tests/test_dsh_bundle.py`；文档主路径改为 plugin add（手写 patch 为回退）。

### Security
- 密钥文件 `O_CREAT|0600` 创建（消除 umask 0644 窗口）；`state_dir` 0700；日志/策略 0600。
- token 不再进入浏览器 argv：`--open` 经 0600 `bootstrap.html` 交付；`?token=` 仅 SSE。
- 变更类操作强制非空 `session_id`；mailbox/consensus 所有权校验；list 默认会话隔离。
- worker `env` 走 0600 文件并剥离 `LD_PRELOAD`/`PYTHON*`/`NODE_OPTIONS` 等危险键。
- `verify_command` **默认拒绝**（须 `AGENT_MCP_VERIFY_ALLOW_PREFIXES`）。
- custom-cli 拒绝世界可写目录 / 绝对路径 bins / 覆盖内置名（需显式 trust 标志）。
- v4 `request_hash` 服务端重算；`_resolve_v4_method` 白名单；CSP `script-src 'self'`；`_read_json` 全路径限长。
- Docker 挂载改 `--mount type=bind` 并拒绝路径含 `:`；network 谓词与文档对齐；git 参数补 `--`。
- `sanitize_env` 改为白名单合并 + 扩展 denylist（`LD_*`/`PYTHON*`/`GIT_*`/`JAVA_TOOL_OPTIONS` 等）。
- `verify` allowlist 路径边界匹配（`py` 不再匹配 `python3`）；worker `*.env.json` 用后即删；SSE 响应补 CSP。

### Fixed
- journal：`created` 标志防并发双执行；孤儿 `recorded` → `uncertain` 可恢复（P7）。
- MCP 变更类工具 5s 内容哈希去重（防客户端重试双 spawn）。
- Goal `goal_pump` 兼容真实 `{"agents":[...]}` 形状（此前生产续播静默失效）。
- refine/harness **create** 拒绝 `base-system-prompt`（Invariant 7）。
- Goal 状态转移表：拒绝 `completed→active`；`needs_advisor` 不再误标 TERMINAL。
- cron DOW 改为标准 Sunday=0。
- Schema 补齐生产消费字段并剥离未知键；`context_mode` 支持 `tail`/`none`。
- 适配器丢弃 `permission_mode` 时返回 `permission_mode_warning`；PrimeAgentRPC 默认 DEGRADED。
- Web：usage 趋势字段映射、预算环真实值、mailbox 路由、沙箱卡诚实「未接线」、`PANEL_V` 统一。

### Changed
- **产品定位文案**：核心思想改为「让 Agent 跑在最适配的底座上 · 打破 Agent 之间的隔离」（不再用「工作池」表述）。
- README 使用**静态透明 Logo**；动态可视化改为**架构匹配/协作演示图**（`architecture-match-bridge.gif`），非图标动效。
- 控制台动效：面板/行/状态/图表/Toast，并尊重 `prefers-reduced-motion`。
- README 重排 + 品牌资产 + 安全加固摘要；测试徽章 617。

## [4.0.0a1] - 2026-09-16

v4.0 第一个里程碑（路线图见
[docs/plans/2026-08-28-v4-roadmap.md](docs/plans/2026-08-28-v4-roadmap.md)）。
主题：Agent Control Plane——Run 唯一执行单位 + 可信协议层 + 控制台重做。

### Added
- **P0 核心对象模型**：`store_v4`（runs/journal/goals/schedules/harness*/refinement/tasks/meta）+ `models.Run` 状态机 + Invariants 测试骨架。
- **P1 Control Plane Reliability**：`/api/v4/command` 幂等 journal、`/api/v4/ack` 水位压缩、`{generation,seq}` 事件游标、`/api/protocol` 能力协商。
- **P2 Execution Manager**：`execution.py` + `triggers.py`；Goal/Schedule 心泵（tick claim 防重放）；Autonomous 预算 min()；usage 子树归因。
- **P3 Harness + Refine**：revision-based 知识层（rollback/OCC/base prompt 守卫）+ 三段式 refine 管道（preview/commit/rollback）。
- **P4/P5 Prime Agent 适配器**：`prime-agent-rpc` / `prime-agent-acp`（能力三态诚实声明，真实冒烟 ⏳）。
- **用户可见 Goal/Schedule/Run 工具**（本轮 +7）：`list_runs` / `goal_create|list|update` / `schedule_create|list|cancel`；spawn/followup 落 Run；agent 终态映射 Run 状态（含 WAITING 桥接）。工具总数 19→**34**（另含 harness×5 + refine×3）。
- **控制台 v4**：左栏会话文件夹分类、顶栏 Agent 切换、自上而下节点图 + 流动边 + 节点/画布拖拽缩放、编排控制面板；删除回放。
- **安装链**：`start_agent_mcp.py --doctor` JSON 健康体检；install.sh 可选 SHA-256；star 链接改为仓库首页且**不再自动打开浏览器**。

### Changed
- 版本单一来源升至 `4.0.0a1`；新增 `LICENSE`（MIT）。
- 安装完成只打印 star 链接，禁止 `gh repo star` / `open` 副作用（防定时任务弹页）。

## [3.0.0a1] - 2026-08-24

v3.0 第一个里程碑（路线图见
[docs/plans/2026-08-24-v3-roadmap.md](docs/plans/2026-08-24-v3-roadmap.md)）。
主题：可信地基——把"声明=实证"重新变成事实。

### Added（第二批，同日）
- **A4 注册一致性守卫**：tests/test_registry_consistency.py 锁死 TOOLS × _DAEMON_PATHS × _API_METHODS × Dispatcher 四方映射（新增工具漏改即 CI 红）；静默异常吞噬数量基线（存量 21 处为有意 best-effort，只准减不准增）。
- **A5 生命周期闭环**：queued 任务参数落库（agents.pending_params），重启经 _rehydrate_queue 自动恢复派发；daemon 重启后仍存活的 worker 据落库 worker_info 被「认领」（重挂 psutil watcher，退出后正常收尸）；_events 加 512 上限回收；policy 引擎热路径去同步写盘（脏标记 + 心跳周期刷盘）。新增 agents.pending_params/worker_info 两列（旧库自动迁移）。
- **A6 安全收紧**：/api/snapshot 与 /events、/api/events 纳入 token 校验（header 或 ?token=）；GET / 不再向未授权请求注入明文 token（修复本机任意进程可提取凭据的泄露——监控页请经 start_agent_mcp --open 的 #token= 链接打开）；mailbox/consensus 校验 from_agent_id 为本会话真实成员；verify_command 改 shell=False + shlex 切词，新增 AGENT_MCP_VERIFY_ALLOW_PREFIXES 可选白名单。
- **B1 选型推荐体系数据面**：docs/research/harness-model-benchmarks-2026-08-24.md 双源分列快照（Artificial Analysis as_of 2026-08-19 + Design Arena ELO as_of 2026-08-24，canonical_slug 门控通过）；scripts/harness_profile.py 一键生成本地实测画像报表。
- **B2 适配器契约**：BaseAdapter.usage_semantics 声明字段（omp/opencode=cumulative，其余 authoritative；GenericAdapter 可配置并校验）；daemon 结算防"尾随零清账"守卫；docs/custom-cli-examples/ 示例库；capability-matrix 顶部实测状态声明。
- **B3 跨底座降档链（opt-in）**：spawn_agent 可选 downgrade_chain（≤5 步 {cli,model}）；followup_task 可选 target_cli/model 显式跨底座续跑（DB 归属同步）。未配置时行为与 v2 完全一致。
- **B4 diff-based 审查**：wait 终态结果附带 file_diffs 清单（≤20 项），编排层把变更清单拼入 reviewer 提示词。
- **B5 协议闭环**：tasks/get 将 needs_advisor 映射为协议级 input_required 并透出决策问题；ACP PoC 按准入判据判定顺延 v3.1（判定记录见路线图 B5 节）。
- 新增测试文件：test_registry_consistency / test_lifecycle_recovery / test_security_hardening / test_b2_usage_contract / test_b3_downgrade_chain / test_b4_diff_review / test_b5_tasks_input_required。

### Fixed（P0 断线修复，均有路由级回归测试）
- **信箱/共识三工具接线断裂**：`mailbox_send`/`mailbox_fetch` 此前调用
  `MailboxManager` 上不存在的 `send_message`/`fetch_messages` 且多传 `payload`，
  `consensus_vote` 用 `team=` 而签名是 `team_id=`——三个 MCP 工具运行时必然 500。
  现已对齐真实 API；`payload` 以 JSON 信封并入 message 字段（不动表结构）；
  `mailbox_fetch` 缺 `agent_id` 时返回结构化 400 而非 TypeError。
  （agent_mcp/daemon_main.py、agent_mcp/mailbox.py）
- **审计结算链三重断裂**：`compute_workspace_diff` 少传 `root_dir`、返回 dict 被
  当 list 迭代、调用了不存在的 `db.add_audit_diff`，整块被 try/except 静默吞掉，
  file_diffs 表从未有数据。现已提取为 `_settle_workspace_audit()` 并接通
  `record_file_diff`；失败写 `agent.audit_failed` 事件并广播，不再静默。
- **容器沙箱链路不可达**：`build_container_sandbox_command` 调用传 `network=`
  （签名为 `network_disabled: bool`）、漏传 `mount_cwd` 导致宿主工作区不挂载。
  两处均已修复；daemon 新增实验开关：设置 `AGENT_MCP_SANDBOX_IMAGE` 即启用容器
  沙箱（默认关闭），`AGENT_MCP_SANDBOX_NETWORK` 控制联网（默认 none）。
- **worktree 绑错仓库**：orchestrator 创建/清理 worktree 的 git 命令未带
  `-C base_dir`，会绑到 daemon 进程 cwd 所在仓库。两处均已补上。
- **orchestrate schema 漂移**：`max_auto_refine`/`refine_prompt` 实现已支持但
  inputSchema 禁止发送（additionalProperties:false）。已放行进 schema，并把
  此前被接收却从未消费的 `refine_prompt` 真正接入精炼循环（作为修复提示词前缀）。

### Changed
- **版本单一来源**：新增 `agent_mcp.__version__ = "3.0.0a1"`；
  `SERVER_VERSION` 与 install.py 安装摘要均同源读取。测试契约同步：
  tools/list 全量从 16 → **19 工具**（mailbox_send/mailbox_fetch/consensus_vote
  在 f7a948e 已注册但计数测试漏更，属既有失败，本次修正）。
- web/index.html 事件类型清单补充 `agent.audit_failed`。

### Added
- **pyproject.toml**：显式声明 psutil 硬依赖（此前新机器安装后 daemon 直接
  ImportError）；`pip install .` / `pip install -e ".[dev]"` 可用；ruff 配置
  （首批仅 E9/F63/F7/F82 runtime-error 规则族）。
- **GitHub Actions CI**（`.github/workflows/ci.yml`）：ruff check +
  `pytest -m "not integration"`（Python 3.10/3.12 矩阵）+ 干净 venv 安装冒烟
  （装完必须能 import mcp_server 与 agent_mcp.daemon_main）。
- **CHANGELOG.md**（本文件）与 v3.0 路线图
  （[docs/plans/2026-08-24-v3-roadmap.md](docs/plans/2026-08-24-v3-roadmap.md)）。
- 新增路由级集成测试 `tests/test_p0_routes_integration.py`（13 用例）。

## [2.2.0] - 2026-08-14

内部契约版本（SERVER_VERSION），对应 MCP 2026-07-28 协议对齐 + DSH 接入：

- 协议层对齐 MCP 2026-07-28 规范（无状态核心、逐请求能力协商、`server/discover`、
  tasks 扩展、structuredContent），兼容 2025-11-25 / 2025-03-26 客户端。
- DeepSeek Harness（DSH）stdio 直连接入文档与验证。

## [2.x] 及之前（2026-08-02 → 2026-08-13）

按内部迭代代号演进（无独立 tag）：

- **v0.1**（08-03）：核心冲刺——daemon/Dispatcher/SQLite 状态机/SSE/Web UI/
  4 个 CLI 适配器/install 三模板/MCP server 8 工具。
- **v0.2**（08-12）：新增 codex/kimi/copilot/pi/zcode/cline 适配器至 11+generic、
  GenericAdapter 配置驱动接入、事件契约三端对齐、多 host installer。
- **v0.3**（08-13）：DAG 编排 orchestrate_task、PolicyEngine 策略治理、沙箱映射层
  （注：映射表当时未接入执行链）、Web 三面板、21-host installer、+7 测试文件。
- f7a948e（08-23）：mailbox/audit/sandbox 内存隔离模块与面板（本次 3.0.0a1
  修复其接线断裂问题）。
