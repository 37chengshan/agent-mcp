"""v4 StoreV4 持久层单测：goals / schedules / harness / tasks（MCP Task ≠ Run）CRUD 与守卫。"""

from __future__ import annotations

import json

import pytest

from agent_mcp import models as m
from agent_mcp.db import DB
from agent_mcp.store_v4 import StoreV4


@pytest.fixture()
def store(tmp_path):
    return StoreV4(DB(tmp_path / "t.db"))


# ---------- runs ----------


def test_run_crud_and_binding(store):
    run = store.run_create(run_kind="goal_continuation", trigger_type="goal", trigger_ref=7, parent_run_id="root")
    assert run["status"] == m.RUN_PENDING
    assert run["trigger_ref"] == "7"
    store.run_bind_agent(run["id"], 42)
    assert store.run_get(run["id"])["agent_id"] == 42
    assert store.runs_for_agent(42)[0]["id"] == run["id"]
    assert store.run_bump_attempts(run["id"]) == 2
    assert store.runs_in_status(m.RUN_PENDING)  # status 默认 pending


# ---------- goals ----------


def test_goal_crud(store):
    gid = store.goal_create(session_id="s1", objective="ship", token_budget=100, time_budget_seconds=600)
    goal = store.goal_get(gid)
    assert goal["status"] == m.GOAL_ACTIVE
    store.goal_bump_round(gid)
    assert store.goal_get(gid)["rounds"] == 1
    store.goal_add_tokens(gid, 5)
    assert store.goal_get(gid)["tokens_used"] == 5
    assert len(store.goals_by_session("s1", active_only=True)) == 1
    store.goal_update_status(gid, m.GOAL_PAUSED)
    assert store.goals_by_session("s1", active_only=True) == []


# ---------- schedules ----------


def test_schedule_due_and_cancel(store):
    sid = store.schedule_create(
        session_id="s1", prompt="check", kind="heartbeat", interval_expr="30m", next_tick="2026-01-01T00:00:00+00:00"
    )
    due = store.schedules_due("2026-01-01T00:00:01+00:00")
    assert any(s["id"] == sid for s in due)
    store.schedule_cancel(sid)
    assert store.schedules_due("2026-01-01T00:00:01+00:00") == []


# ---------- harness ----------


def test_harness_optimistic_concurrency(store):
    store.harness_create(item_id="m1", kind="memory", title="lesson", content="v1")
    store.harness_update(item_id="m1", expected_version=1, content="v2")
    with pytest.raises(ValueError):
        store.harness_update(item_id="m1", expected_version=1, content="stale write")  # OCC 冲突
    assert store.harness_get("m1")["content"] == "v2"


def test_harness_delete_and_rollback_restore(store):
    store.harness_create(item_id="h", kind="skill", title="s", content="v1")
    store.harness_update(item_id="h", expected_version=1, content="v2")
    store.harness_delete(item_id="h", expected_version=2)
    assert store.harness_get("h") is None
    restored = store.harness_rollback("h", to_revision=2)  # 回滚到 rev2 快照（v1 状态）
    assert restored["content"] == "v1"
    assert restored["kind"] == "skill"


# ---------- MCP Task ≠ Run（映射层） ----------


def test_task_maps_to_run_but_is_distinct(store):
    run = store.run_create(run_kind="spawn", trigger_type="manual")
    store.task_upsert(task_id="task-1", run_id=run["id"], agent_id=3, status="working")
    task = store.task_get("task-1")
    assert task["run_id"] == run["id"]
    assert task["status"] == "working"
    assert store.task_by_run(run["id"])["task_id"] == "task-1"
    # Task 状态与 Run 状态互相独立（一个 Task handle 对应一个 Run；不混为一个对象）
    store.run_transition(run["id"], m.RUN_ADMITTED)
    store.run_transition(run["id"], m.RUN_RUNNING)
    assert store.task_get("task-1")["status"] == "working"  # Task 状态不被 Run 转移覆盖

