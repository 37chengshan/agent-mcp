"""v4 对象模型：Run 状态机 / ExecutionPolicy / Goal / Schedule / Journal 幂等 / Harness 守卫。

纯逻辑层（无 IO），供 store_v4.py（持久层）、execution.py / triggers.py（P2）与
refine.py（P3）共用。所有 Invariant 判定函数集中在此，配套测试见
tests/test_invariants.py。

定位约束：本模块只表达 Control Plane 的对象与规则，不出现任何 Runtime 执行逻辑。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ==================== Run（唯一执行单位）状态机 ====================

RUN_PENDING = "PENDING"
RUN_ADMITTED = "ADMITTED"
RUN_RUNNING = "RUNNING"
RUN_WAITING = "WAITING"
RUN_COMPLETED = "COMPLETED"
RUN_FAILED = "FAILED"
RUN_CANCELLED = "CANCELLED"
RUN_INTERRUPTED = "INTERRUPTED"
RUN_INCOMPLETE = "INCOMPLETE"

ALL_RUN_STATUSES = frozenset({
    RUN_PENDING, RUN_ADMITTED, RUN_RUNNING, RUN_WAITING, RUN_COMPLETED,
    RUN_FAILED, RUN_CANCELLED, RUN_INTERRUPTED, RUN_INCOMPLETE,
})

RUN_TERMINAL = frozenset(
    {RUN_COMPLETED, RUN_FAILED, RUN_CANCELLED, RUN_INTERRUPTED, RUN_INCOMPLETE}
)

RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    RUN_PENDING: frozenset({RUN_ADMITTED, RUN_CANCELLED, RUN_FAILED}),
    RUN_ADMITTED: frozenset({RUN_RUNNING, RUN_CANCELLED, RUN_FAILED}),
    RUN_RUNNING: frozenset(
        {RUN_WAITING, RUN_COMPLETED, RUN_FAILED, RUN_CANCELLED, RUN_INTERRUPTED, RUN_INCOMPLETE}
    ),
    RUN_WAITING: frozenset({RUN_RUNNING, RUN_CANCELLED, RUN_FAILED}),
}


def run_transition(current: str, target: str) -> str:
    """Run 状态机唯一合法转移。终端态不可再转移（Invariant：唯一执行单位）。"""
    if current in RUN_TERMINAL:
        raise ValueError(f"terminal run cannot transition: {current} -> {target}")
    allowed = RUN_TRANSITIONS.get(current)
    if allowed is None or target not in allowed:
        raise ValueError(f"invalid run transition: {current} -> {target}")
    return target


# MCP 兼容投影：Run.status -> 既有 agent.status / stop_reason（MCP 工具语义不变）
RUN_TO_AGENT_STATUS = {
    RUN_PENDING: "queued",
    RUN_ADMITTED: "queued",
    RUN_RUNNING: "running",
    RUN_WAITING: "needs_advisor",
    RUN_COMPLETED: "terminated",
    RUN_FAILED: "error",
    RUN_CANCELLED: "cancelled",
    RUN_INTERRUPTED: "cancelled",
    RUN_INCOMPLETE: "incomplete",
}

RUN_TO_STOP_REASON = {
    RUN_COMPLETED: "end_turn",
    RUN_FAILED: "error_exit",
    RUN_CANCELLED: "cancelled",
    RUN_INTERRUPTED: "interrupted",
    RUN_INCOMPLETE: "budget_exhausted",
}


def run_to_agent_status(run_status: str) -> str:
    if run_status not in ALL_RUN_STATUSES:
        raise ValueError(f"unknown run status: {run_status}")
    return RUN_TO_AGENT_STATUS[run_status]


# ==================== ExecutionPolicy（只约束 Run，纯声明） ====================


@dataclass(frozen=True)
class AutonomousPolicy:
    """一次 Run 的连续执行策略。任一预算耗尽即停止（INCOMPLETE，非成功）。

    Invariant 5：Autonomous 永远不能突破 continuation/turn/token/time 任一预算。
    """

    max_continuations: int | None = None
    max_turns: int | None = None
    max_tokens: int | None = None
    max_seconds: int | None = None
    gates: tuple[str, ...] = ()

    def can_continue(
        self,
        *,
        continuations: int = 0,
        turns: int = 0,
        tokens: int = 0,
        elapsed_seconds: int = 0,
    ) -> bool:
        """只有所有已设预算都未耗尽才允许继续（min() 生效）。"""
        checks = [
            self.max_continuations is None or continuations < self.max_continuations,
            self.max_turns is None or turns < self.max_turns,
            self.max_tokens is None or tokens < self.max_tokens,
            self.max_seconds is None or elapsed_seconds < self.max_seconds,
        ]
        return all(checks)

    def remaining(
        self,
        *,
        continuations: int = 0,
        turns: int = 0,
        tokens: int = 0,
        elapsed_seconds: int = 0,
    ) -> dict[str, int]:
        """剩余预算（-1 表示该维度未设预算，视为无限）。"""
        return {
            "continuations": self._rem(self.max_continuations, continuations),
            "turns": self._rem(self.max_turns, turns),
            "tokens": self._rem(self.max_tokens, tokens),
            "seconds": self._rem(self.max_seconds, elapsed_seconds),
        }

    @staticmethod
    def _rem(limit: int | None, spent: int) -> int:
        if limit is None:
            return -1  # 未设预算
        return max(0, limit - spent)

    def to_json(self) -> dict[str, Any]:
        return {
            "max_continuations": self.max_continuations,
            "max_turns": self.max_turns,
            "max_tokens": self.max_tokens,
            "max_seconds": self.max_seconds,
            "gates": list(self.gates),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> AutonomousPolicy:
        data = data or {}
        gates = data.get("gates") or []
        return cls(
            max_continuations=data.get("max_continuations"),
            max_turns=data.get("max_turns"),
            max_tokens=data.get("max_tokens"),
            max_seconds=data.get("max_seconds"),
            gates=tuple(gates),
        )


@dataclass(frozen=True)
class RetryPolicy:
    """验证回投策略：gate 失败有界重试；工作区指纹未变不重跑同一失败 gate。"""

    max_attempts: int = 3
    gate: str | None = None  # verify_command
    gate_skip_idle_ws: bool = True

    def can_retry(self, attempts: int) -> bool:
        return attempts < self.max_attempts

    def to_json(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "gate": self.gate,
            "gate_skip_idle_ws": self.gate_skip_idle_ws,
        }


# ==================== Trigger / Goal 规则 ====================

GOAL_ACTIVE = "active"
GOAL_PAUSED = "paused"
GOAL_COMPLETED = "completed"
GOAL_STATUSES = frozenset({GOAL_ACTIVE, GOAL_PAUSED, GOAL_COMPLETED})

# Goal 状态机：completed 为终态，拒绝 completed→active 等非法迁移
GOAL_TRANSITIONS: dict[str, frozenset[str]] = {
    GOAL_ACTIVE: frozenset({GOAL_PAUSED, GOAL_COMPLETED}),
    GOAL_PAUSED: frozenset({GOAL_ACTIVE, GOAL_COMPLETED}),
    GOAL_COMPLETED: frozenset(),
}


def goal_transition(current: str, target: str) -> str:
    """Goal 状态机唯一合法转移；completed 终态不可再激活（Invariant 4）。"""
    if current not in GOAL_STATUSES:
        raise ValueError(f"invalid goal status: {current}")
    if target not in GOAL_STATUSES:
        raise ValueError(f"invalid goal status: {target}")
    allowed = GOAL_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise ValueError(f"invalid goal transition: {current} -> {target}")
    return target


def goal_should_continue(
    goal: dict[str, Any],
    *,
    rounds: int = 0,
    tokens_used: int = 0,
    elapsed_seconds: int = 0,
) -> bool:
    """Invariant 4：Goal completed 后不得继续自动 continuation。

    只允许 active 且未超预算的目标产生续播。
    """
    if goal.get("status") != GOAL_ACTIVE:
        return False
    if goal.get("completed_at"):
        return False
    token_budget = goal.get("token_budget")
    time_budget = goal.get("time_budget_seconds")
    if token_budget is not None and tokens_used >= int(token_budget):
        return False
    if time_budget is not None and elapsed_seconds >= int(time_budget):
        return False
    return True


# ==================== Schedule tick（Invariant 3 依赖的纯规则） ====================


class IntervalSpec:
    """支持 '30m'/'1h' 等（秒）与 5 字段 cron（'m h dom mon dow'，*/n 步长、a-b 区间）。"""

    _SIMPLE_RE = re.compile(r"^(\d+)([smhd])$")
    _UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

    @staticmethod
    def parse(expr: str | None) -> tuple[str, Any] | None:
        if not expr:
            return None
        m = IntervalSpec._SIMPLE_RE.match(expr.strip())
        if m:
            return ("simple", int(m.group(1)) * IntervalSpec._UNITS[m.group(2)])
        parts = expr.strip().split()
        if len(parts) == 5:
            return ("cron", parts)
        return None

    @staticmethod
    def next_after(expr: str | None, ref: datetime | None = None) -> str | None:
        """返回下一次触发的 ISO 时间；无法计算（one_shot 已消费等）返回 None。"""
        ref = ref or _utcnow()
        parsed = IntervalSpec.parse(expr)
        if parsed is None:
            return None
        kind, value = parsed
        if kind == "simple":
            return (ref + timedelta(seconds=value)).isoformat()
        return IntervalSpec._next_cron(value, ref)

    @staticmethod
    def _next_cron(fields: list[str], ref: datetime) -> str | None:
        minute_f, hour_f, dom_f, mon_f, dow_f = fields
        try:
            minutes = IntervalSpec._expand(minute_f, 0, 59)
            hours = IntervalSpec._expand(hour_f, 0, 23)
            doms = IntervalSpec._expand(dom_f, 1, 31)
            mons = IntervalSpec._expand(mon_f, 1, 12)
            dows = IntervalSpec._expand(dow_f, 0, 7)  # 标准 cron 允许 0-7（0/7=Sunday）
            dows = frozenset(0 if d == 7 else d for d in dows)
        except (ValueError, KeyError):
            return None
        cur = ref.replace(second=0, microsecond=0)
        for _ in range(366 * 24 * 60):  # 有界搜索（一年）
            # 标准 cron DOW：Sunday=0 … Saturday=6；datetime.weekday() 是 Monday=0
            cron_dow = (cur.weekday() + 1) % 7
            if (
                cur.month in mons
                and cur.day in doms
                and cron_dow in dows
                and cur.hour in hours
                and cur.minute in minutes
            ):
                if cur > ref:
                    return cur.isoformat()
            cur += timedelta(minutes=1)
        return None

    @staticmethod
    def _expand(field: str, lo: int, hi: int) -> frozenset[int]:
        values: set[int] = set()
        for part in str(field).split(","):
            if part == "*":
                values.update(range(lo, hi + 1))
            elif "/" in part:
                base, step_s = part.split("/", 1)
                step = int(step_s)
                if base == "*":
                    values.update(range(lo, hi + 1, step))
                else:
                    values.update(range(int(base), hi + 1, step))
            else:
                if "-" in part:
                    a, b = part.split("-", 1)
                    values.update(range(int(a), int(b) + 1))
                else:
                    values.add(int(part))
        if not values:
            raise ValueError(f"empty cron field: {field}")
        return frozenset(values)


# ==================== Journal 幂等判定（Invariant 1/2） ====================

JOURNAL_NEW = "new"
JOURNAL_REPLAY = "replay"
JOURNAL_CONFLICT = "conflict"


def request_hash(method: str, params: dict[str, Any] | None) -> str:
    """命令的规范化 request_hash：method + 排序后的 params JSON。"""
    canonical = json.dumps(params or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256((method + "\x00" + canonical).encode("utf-8")).hexdigest()


def journal_classify(existing: dict[str, Any] | None, request_hash_value: str) -> str:
    """幂等判定：
    - 无既有条目 → new（允许执行一次）；
    - 同 hash → replay（返回已记录结果，绝不重放）；
    - 异 hash → conflict（command_id_conflict，绝不执行）。
    """
    if existing is None:
        return JOURNAL_NEW
    if existing.get("request_hash") == request_hash_value:
        return JOURNAL_REPLAY
    return JOURNAL_CONFLICT


# ==================== Harness 守卫（Invariant 7/8） ====================

HARNESS_KINDS = frozenset({"prompt", "memory", "skill", "subagent_spec"})
# 保留字：base system prompt 永不可被 refine 修改
BASE_PROMPT_ID = "base-system-prompt"


def assert_harness_kind(kind: str) -> None:
    if kind not in HARNESS_KINDS:
        raise ValueError(f"invalid harness kind: {kind!r}; allowed={sorted(HARNESS_KINDS)}")


def assert_refine_target_allowed(item: dict[str, Any] | None, *, op: str,
                                 target_id: str | None = None) -> None:
    """Invariant 7：refine 只允许操作 harness 表内非 base 条目；op 白名单。
    target_id 显式传入时（create 无 item）同样拒绝 base-system-prompt。"""
    if op not in ("create", "update", "delete"):
        raise ValueError(f"invalid refine op: {op!r}")
    resolved = target_id or (item.get("id") if item is not None else None)
    if resolved == BASE_PROMPT_ID:
        raise ValueError("base system prompt is immutable")
    if item is not None:
        assert_harness_kind(str(item.get("kind", "")))


# ==================== 能力契约（Invariant 6） ====================

CAP_SUPPORTED = "SUPPORTED"
CAP_UNSUPPORTED = "UNSUPPORTED"
CAP_DEGRADED = "DEGRADED"
CAP_STATES = frozenset({CAP_SUPPORTED, CAP_UNSUPPORTED, CAP_DEGRADED})

# 适配器能力清单（P8 契约；每项必须来自真实协议协商，禁止产品宣称即能力）
ADAPTER_CAPABILITIES = (
    "spawn", "resume", "steer", "follow_up", "observe",
    "schedule", "heartbeat", "goal", "autonomous", "agent_message",
)


class AdapterCapability:
    """Backend Adapter 能力契约：三态判定 + 调用守卫。"""

    def __init__(self, states: dict[str, str] | None = None):
        self._states: dict[str, str] = {}
        for name in ADAPTER_CAPABILITIES:
            self._states[name] = states.get(name, CAP_UNSUPPORTED) if states else CAP_UNSUPPORTED
        for name, state in (states or {}).items():
            if state not in CAP_STATES:
                raise ValueError(f"invalid capability state for {name}: {state!r}")

    def get(self, name: str) -> str:
        if name not in self._states:
            raise ValueError(f"unknown capability: {name!r}")
        return self._states[name]

    def assert_supported(self, name: str) -> None:
        """Invariant 6：adapter 不得调用未声明 capability。"""
        state = self.get(name)
        if state not in (CAP_SUPPORTED, CAP_DEGRADED):
            raise PermissionError(f"capability {name!r} is {state}")

    def to_dict(self) -> dict[str, str]:
        return dict(self._states)


# ==================== 失败指纹（refine/autonomous 抑制） ====================


def failure_fingerprint(session_id: str, error_sig: str) -> str:
    """同一失败指纹不得无限驱动系统自我修改（same-failure suppression）。"""
    raw = f"{session_id}\x00{error_sig.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def session_fingerprint(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()