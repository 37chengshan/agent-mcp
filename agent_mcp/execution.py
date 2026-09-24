"""v4 Execution Manager：Run 是唯一执行单位（roadmap §7）。

职责：admission → worker bind → advance → 终态/恢复。
Trigger（goal/schedule/heartbeat/manual/retry/autonomous）只产 Intent，
最终都经 create_run 进入本管理器。Autonomous = Execution Policy（预算+gate），
只约束 Run，绝不自行实现 agent loop（P2/P3 原则）。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable

from . import models as m
from .store_v4 import StoreV4
from .triggers import GoalIntent, next_tick_after, now_iso


def usage_with_children(db: Any, store: StoreV4, agent_id: int) -> dict[str, Any]:
    """B4 归因：agent + 全部子孙 Run 绑定的 agent usage 聚合（不重复计自身）。

    用法：get_token_usage(include_children=True) 时替代单 agent 合计。
    """
    seen: set[int] = {agent_id}
    changed = True
    while changed:
        changed = False
        for run in store.runs_all():
            if run.get("agent_id") is None or run.get("parent_run_id") is None:
                continue
            parent_run = store.run_get(run["parent_run_id"])
            if parent_run is None:
                continue
            parent_agent = parent_run.get("agent_id")
            if parent_agent is not None and int(parent_agent) in seen and int(run["agent_id"]) not in seen:
                seen.add(int(run["agent_id"]))
                changed = True
    totals: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0,
                              "cache_creation": 0, "cache_read": 0, "cost_usd": 0.0}
    for aid in seen:
        u = db.usage_total(aid)
        for k in totals:
            totals[k] = totals.get(k, 0) + u.get(k, 0)
    totals["estimated"] = True
    totals["agents"] = sorted(seen)
    return totals


class ExecutionManager:
    """Run 生命周期调度器。所有触发最终都 produce → create_run → bind → advance。"""

    def __init__(self, dispatcher: Any, store: StoreV4):
        self.dispatcher = dispatcher
        self.store = store

    # ---- Run 创建 ----
    def create_run(
        self,
        *,
        run_kind: str,
        trigger_type: str | None = None,
        trigger_ref: str | int | None = None,
        policy: m.AutonomousPolicy | None = None,
        origin_command_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> dict[str, Any]:
        policy_json = json.dumps(policy.to_json(), ensure_ascii=False) if policy else None
        return self.store.run_create(
            run_kind=run_kind, trigger_type=trigger_type, trigger_ref=trigger_ref,
            policy_json=policy_json, origin_command_id=origin_command_id,
            parent_run_id=parent_run_id,
        )

    def _bind_and_mark(self, run_id: str, agent_id: int, requested_status: str) -> dict[str, Any]:
        """PENDING → ADMITTED →（queued 则止步）→ RUNNING。状态机强制两步。"""
        self.store.run_bind_agent(run_id, agent_id)
        self.store.run_transition(run_id, m.RUN_ADMITTED)
        if requested_status != "queued":
            self.store.run_transition(run_id, m.RUN_RUNNING)
        return self.store.run_get(run_id)

    def _spawn_bind(self, run_id: str, params: dict[str, Any]) -> int:
        res = self.dispatcher.spawn(params)
        agent_id = int(res.get("agent_id"))
        self._bind_and_mark(run_id, agent_id, res.get("status") or "running")
        return agent_id

    # ---- Manual Follow-up spawn（spawn_agent 的 Run 化入口） ----
    def manual_dispatch(
        self,
        *,
        params: dict[str, Any],
        policy: m.AutonomousPolicy | None = None,
        origin_command_id: str | None = None,
        parent_run_id: str | None = None,
        run_kind: str = "manual",
    ) -> dict[str, Any]:
        run = self.create_run(
            run_kind=run_kind, trigger_type="manual", policy=policy,
            origin_command_id=origin_command_id, parent_run_id=parent_run_id,
        )
        try:
            agent_id = self._spawn_bind(run["id"], params)
        except ValueError as exc:
            self.store.run_transition(run["id"], m.RUN_FAILED, stop_reason=str(exc)[:200])
            raise
        return self.store.run_get(run["id"])

    # ---- Schedule / Heartbeat pump（Invariant 3：同 tick 至多一个 Run） ----
    def schedule_pump(self, now: str | None = None) -> list[dict[str, Any]]:
        created: list[dict[str, Any]] = []
        for sched in self.store.schedules_due(now or now_iso()):
            claim_id = uuid.uuid4().hex
            if not self.store.schedule_claim(int(sched["id"]), claim_id):
                continue  # 已被 claim：不产生第二个 Run
            run = self.create_run(run_kind="schedule_tick", trigger_type="schedule",
                                  trigger_ref=sched["id"])
            try:
                if sched.get("agent_id"):
                    res = self.dispatcher.followup({
                        "agent_id": sched["agent_id"],
                        "prompt": sched["prompt"],
                        "session_id": sched["session_id"],
                    })
                else:
                    raise ValueError("schedule requires agent_id (kind=%s)" % (sched.get("kind") or ""))
            except Exception as exc:  # noqa: BLE001
                self.store.schedule_release_claim(int(sched["id"]))
                self.store.run_transition(run["id"], m.RUN_FAILED, stop_reason=str(exc)[:200])
                created.append(self.store.run_get(run["id"]))
                continue
            next_tick = next_tick_after(self.store, sched, now)
            self.store.schedule_advance(int(sched["id"]), next_tick)
            self._bind_and_mark(run["id"], int(res.get("agent_id") or sched["agent_id"]),
                                res.get("status") or "running")
            created.append(self.store.run_get(run["id"]))
        return created

    # ---- Goal pump（Invariant 4：completed 不续播；预算耗尽不续播） ----
    def goal_pump(self, now: str | None = None) -> list[dict[str, Any]]:
        now = now or now_iso()
        try:
            raw = self.dispatcher.list_agents({})
        except Exception:  # noqa: BLE001
            return []
        # G1: 兼容 {"agents": [...]} 与 list 两种返回形状；绝不 TypeError 吞掉
        if isinstance(raw, dict):
            agent_list = raw.get("agents") or []
        elif isinstance(raw, list):
            agent_list = raw
        else:
            return []
        try:
            agents = {int(a["id"]): a for a in agent_list if isinstance(a, dict) and a.get("id") is not None}
        except (TypeError, ValueError, KeyError):
            return []
        created: list[dict[str, Any]] = []
        for goal in self.store.goals_active():
            agent_id = goal.get("agent_id")
            if agent_id is None:
                continue
            agent = agents.get(int(agent_id))
            if agent is None:
                continue
            if agent.get("status") in ("running", "queued", "needs_advisor"):
                continue
            if not GoalIntent.should_continue(self.store, goal, now=now):
                continue
            run = self.create_run(run_kind="goal_continuation", trigger_type="goal",
                                  trigger_ref=goal["id"])
            try:
                res = self.dispatcher.followup({
                    "agent_id": agent_id,
                    "prompt": goal["objective"],
                    "session_id": goal["session_id"],
                })
            except Exception as exc:  # noqa: BLE001
                self.store.run_transition(run["id"], m.RUN_FAILED, stop_reason=str(exc)[:200])
                continue
            self.store.goal_bump_round(int(goal["id"]))
            self._bind_and_mark(run["id"], int(agent_id), res.get("status") or "running")
            created.append(self.store.run_get(run["id"]))
        return created

    # ---- Bounded Autonomous（Execution Policy；Invariant 5） ----
    def autonomous_run(
        self,
        *,
        params: dict[str, Any],
        policy: m.AutonomousPolicy,
        wait_for_terminal: Callable[[int], dict[str, Any]],
        run_gate: Callable[[str, str], tuple[bool, str]],
        workspace_fp_of: Callable[[str], str | None] | None = None,
    ) -> dict[str, Any]:
        """一次 spawn + 有界 continuation。gate 失败有界回投；工作区指纹未变不重跑
        同一失败 gate；任一预算耗尽 → INCOMPLETE（非成功）。"""
        cwd = str(params.get("cwd") or ".")
        session_id = str(params.get("session_id") or "default")
        run = self.create_run(run_kind="autonomous", trigger_type="manual", policy=policy)
        agent_id = self._spawn_bind(run["id"], params)
        spent = {"continuations": 0, "turns": 0, "tokens": 0, "elapsed_seconds": 0}
        start = time.monotonic()
        last_fail_fp: str | None = None
        outcome: str | None = None
        # 安全上限：预算未设时也保证有界（turns/time 每轮递增 + 显式上限兜底）
        hard_cap = 1 + int(policy.max_continuations or 0) + int(policy.max_turns or 0) + 10
        iterations = 0

        while True:
            iterations += 1
            if iterations > hard_cap:
                outcome = "incomplete"  # 防御性兜底：绝不无限自我循环
                break
            terminal = wait_for_terminal(agent_id)
            if terminal.get("status") in ("error", "cancelled", "interrupted"):
                self.store.run_transition(run["id"], m.RUN_FAILED,
                                          stop_reason=str(terminal.get("stop_reason") or "agent_failed"))
                return self.store.run_get(run["id"])
            spent["turns"] += 1
            spent["elapsed_seconds"] = int(time.monotonic() - start)
            if (policy.max_turns is not None and spent["turns"] > policy.max_turns) or (
                policy.max_seconds is not None and spent["elapsed_seconds"] > policy.max_seconds
            ):
                outcome = "incomplete"  # turns/time 预算耗尽（Invariant 5）
                break
            if not policy.gates:
                outcome = "completed"
                break
            gate_failed = False
            for gate in policy.gates:
                fp = workspace_fp_of(gate) if workspace_fp_of else None
                if fp is not None and fp == last_fail_fp:
                    # 工作区未变：不重跑同一失败 gate（same-failure suppression）
                    outcome = "incomplete"
                    break
                ok, out_text = run_gate(gate, cwd)
                if not ok:
                    gate_failed = True
                    last_fail_fp = fp
                    spent["continuations"] += 1
                    self.store.run_bump_attempts(run["id"])
                    break
                last_fail_fp = None
            if outcome is not None:
                break
            if gate_failed:
                # max_continuations 限的是"续播次数"；首轮执行不受其限制
                if policy.max_continuations is not None and spent["continuations"] > policy.max_continuations:
                    outcome = "incomplete"  # continuation 预算耗尽（Invariant 5）
                    break
                self.dispatcher.followup({
                    "agent_id": agent_id,
                    "prompt": "gate failed (%s); keep working: %s" % (gate, str(out_text)[:500]),
                    "session_id": session_id,
                    "interrupt": True,
                })
                continue
            outcome = "completed"
            break

        if outcome == "completed":
            self.store.run_transition(run["id"], m.RUN_COMPLETED)
        elif outcome == "incomplete":
            self.store.run_transition(run["id"], m.RUN_INCOMPLETE, stop_reason="budget_exhausted")
        elif not policy.can_continue(**spent):
            self.store.run_transition(run["id"], m.RUN_INCOMPLETE, stop_reason="budget_exhausted")
        else:
            self.store.run_transition(run["id"], m.RUN_COMPLETED)
        return self.store.run_get(run["id"])

