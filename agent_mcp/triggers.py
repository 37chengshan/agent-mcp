"""v4 Trigger / Intent 层：Goal / Schedule / Heartbeat / Manual Follow-up。

只产生 Intent，不管理 worker（roadmap §7.2）。实际执行由 ExecutionManager
统一创建 Run；本层不维护任何执行状态机。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from . import models as m
from .store_v4 import StoreV4


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def claim_tick(store: StoreV4, schedule: dict[str, Any], claim_id: str | None = None) -> bool:
    """Invariant 3：同一 tick 至多一个 Run（原子 claim；失败即跳过）。"""
    return store.schedule_claim(int(schedule["id"]), claim_id or uuid.uuid4().hex)


def next_tick_after(
    store: StoreV4, schedule: dict[str, Any], ref_iso: str | None = None
) -> str | None:
    """计算下一 tick。one_shot 无 interval → None（不再触发）；其余走 IntervalSpec。"""
    ref = datetime.fromisoformat(ref_iso or now_iso())
    return m.IntervalSpec.next_after(schedule.get("interval_expr"), ref)


class GoalIntent:
    """Goal = 持续目标（Trigger/Intent）。判断是否应续播（只读规则）。"""

    @staticmethod
    def should_continue(store: StoreV4, goal: dict[str, Any], *, now: str | None = None) -> bool:
        now = now or now_iso()
        elapsed = 0
        if goal.get("created_at"):
            try:
                created = datetime.fromisoformat(str(goal["created_at"]))
                elapsed = max(0, int((datetime.fromisoformat(now) - created).total_seconds()))
            except ValueError:
                elapsed = 0
        return m.goal_should_continue(
            goal,
            rounds=int(goal.get("rounds") or 0),
            tokens_used=int(goal.get("tokens_used") or 0),
            elapsed_seconds=elapsed,
        )


class ScheduleIntent:
    """Schedule（含 kind=heartbeat）= 未来触发条件。"""

    @staticmethod
    def due(store: StoreV4, now: str | None = None) -> list[dict[str, Any]]:
        return store.schedules_due(now or now_iso())

