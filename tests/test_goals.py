"""P2 DoD：Goal continuation（Invariant 4：completed/预算耗尽不再续播）。"""

from __future__ import annotations

import pytest

from agent_mcp import models as m
from agent_mcp.db import DB
from agent_mcp.execution import ExecutionManager
from agent_mcp.store_v4 import StoreV4
from agent_mcp.triggers import now_iso
from tests._v4_fakes import FakeDispatcher


@pytest.fixture()
def env(tmp_path):
    db = DB(tmp_path / "t.db")
    store = StoreV4(db)
    dispatcher = FakeDispatcher()
    em = ExecutionManager(dispatcher, store)
    return {"db": db, "store": store, "dispatcher": dispatcher, "em": em}


def test_goal_continuation_loop(env):
    dispatcher, store, em = env["dispatcher"], env["store"], env["em"]
    agent_id = dispatcher.spawn({"target_cli": "omp", "prompt": "x", "cwd": "/tmp"})["agent_id"]
    gid = store.goal_create(session_id="s1", objective="ship it", agent_id=agent_id)
    dispatcher.mark_terminal(agent_id)

    runs1 = em.goal_pump(now_iso())
    assert len(runs1) == 1
    assert runs1[0]["run_kind"] == "goal_continuation"
    assert runs1[0]["trigger_ref"] == str(gid)
    assert store.goal_get(gid)["rounds"] == 1

    dispatcher.mark_terminal(agent_id)
    runs2 = em.goal_pump(now_iso())
    assert len(runs2) == 1
    assert store.goal_get(gid)["rounds"] == 2
    assert len(dispatcher.followups) == 2


def test_goal_completed_stops_continuation(env):
    dispatcher, store, em = env["dispatcher"], env["store"], env["em"]
    agent_id = dispatcher.spawn({"target_cli": "omp", "prompt": "x", "cwd": "/tmp"})["agent_id"]
    gid = store.goal_create(session_id="s1", objective="ship it", agent_id=agent_id)
    dispatcher.mark_terminal(agent_id)
    assert len(em.goal_pump(now_iso())) == 1
    store.goal_update_status(gid, m.GOAL_COMPLETED)
    dispatcher.mark_terminal(agent_id)
    assert em.goal_pump(now_iso()) == []  # Invariant 4
    assert len(dispatcher.followups) == 1
    assert store.goal_get(gid)["rounds"] == 1


def test_goal_token_budget_stops_continuation(env):
    dispatcher, store, em = env["dispatcher"], env["store"], env["em"]
    agent_id = dispatcher.spawn({"target_cli": "omp", "prompt": "x", "cwd": "/tmp"})["agent_id"]
    gid = store.goal_create(session_id="s1", objective="budgeted", agent_id=agent_id, token_budget=1)
    store.goal_add_tokens(gid, 1)
    dispatcher.mark_terminal(agent_id)
    dispatcher.mark_terminal(agent_id)
    assert em.goal_pump(now_iso()) == []



def test_goal_pump_accepts_real_list_agents_shape(tmp_path):
    """G1: list_agents 返回 {"agents": [...]} 与 list 均可；绝不 TypeError 吞掉。"""
    from agent_mcp.db import DB
    from agent_mcp.execution import ExecutionManager
    from agent_mcp.store_v4 import StoreV4
    from agent_mcp import models as m

    class ShapeDispatcher:
        def __init__(self, shape):
            self.shape = shape
            self.followups = []

        def list_agents(self, _body=None):
            return self.shape

        def followup(self, params):
            self.followups.append(params)
            return {"agent_id": params["agent_id"], "status": "running"}

    db = DB(tmp_path / "g.db")
    store = StoreV4(db)
    # 真实形状 dict
    disp = ShapeDispatcher({"agents": [{"id": 1, "status": "terminated"}]})
    em = ExecutionManager(disp, store)
    aid = db.insert_agent(parent_id=None, session_id="s1", task_name="t", cli="claude")
    db.set_status(aid, "terminated", stop_reason="end_turn")
    gid = store.goal_create(session_id="s1", objective="go", agent_id=aid)
    created = em.goal_pump()
    assert len(created) == 1
    assert disp.followups and disp.followups[0]["agent_id"] == aid
    # list 形状（兼容）
    disp2 = ShapeDispatcher([{"id": aid, "status": "terminated"}])
    em2 = ExecutionManager(disp2, store)
    # 该 goal 已 bump round 仍 active，可继续
    created2 = em2.goal_pump()
    assert isinstance(created2, list)
