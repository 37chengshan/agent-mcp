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

