"""P2 DoD：usage 归因（child_usage_attributed 语义；include_children）。"""

from __future__ import annotations

import pytest

from agent_mcp import models as m
from agent_mcp.db import DB
from agent_mcp.execution import ExecutionManager, usage_with_children
from agent_mcp.store_v4 import StoreV4
from tests._v4_fakes import FakeDispatcher


@pytest.fixture()
def env(tmp_path):
    db = DB(tmp_path / "t.db")
    store = StoreV4(db)
    dispatcher = FakeDispatcher()
    em = ExecutionManager(dispatcher, store)
    return {"db": db, "store": store, "dispatcher": dispatcher, "em": em}


def test_usage_attribution_includes_children(env):
    db, store, em = env["db"], env["store"], env["em"]
    root = em.manual_dispatch(
        params={"target_cli": "omp", "prompt": "root", "cwd": "/tmp", "session_id": "s1"}
    )
    root_agent = int(root["agent_id"])

    child = em.create_run(run_kind="goal_continuation", trigger_type="goal",
                          trigger_ref=1, parent_run_id=root["id"])
    child_agent = root_agent + 1
    store.run_bind_agent(child["id"], child_agent)
    store.run_transition(child["id"], m.RUN_ADMITTED)
    store.run_transition(child["id"], m.RUN_RUNNING)

    db.upsert_usage(agent_id=root_agent, model="m", input_tokens=100, output_tokens=10,
                    cache_creation=0, cache_read=0, cost_usd=0.01)
    db.upsert_usage(agent_id=child_agent, model="m", input_tokens=30, output_tokens=5,
                    cache_creation=0, cache_read=0, cost_usd=0.005)

    totals = usage_with_children(db, store, root_agent)
    assert totals["input_tokens"] == 130
    assert totals["output_tokens"] == 15
    assert totals["agents"] == sorted({root_agent, child_agent})


def test_usage_single_agent_unchanged(env):
    db, store, em = env["db"], env["store"], env["em"]
    run = em.manual_dispatch(
        params={"target_cli": "omp", "prompt": "x", "cwd": "/tmp", "session_id": "s1"}
    )
    aid = int(run["agent_id"])
    db.upsert_usage(agent_id=aid, model="m", input_tokens=10, output_tokens=1,
                    cache_creation=0, cache_read=0, cost_usd=0.001)
    totals = usage_with_children(db, store, aid)
    assert totals["input_tokens"] == 10
    assert totals["agents"] == [aid]

