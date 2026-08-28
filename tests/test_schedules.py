"""P2 DoD：schedule/heartbeat tick（Invariant 3：同一 tick 至多一个 Run）。"""

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


def test_schedule_tick_creates_single_run_and_advances(env):
    dispatcher, store, em = env["dispatcher"], env["store"], env["em"]
    dispatcher._agents[1] = "terminated"
    sid = store.schedule_create(session_id="s1", prompt="tick", kind="one_shot",
                                next_tick="2020-01-01T00:00:00+00:00", agent_id=1)
    runs = em.schedule_pump(now_iso())
    assert len(runs) == 1
    run = runs[0]
    assert run["run_kind"] == "schedule_tick"
    assert run["trigger_ref"] == str(sid)
    assert run["status"] == m.RUN_RUNNING
    assert len(dispatcher.followups) == 1
    # one_shot 无 interval → 推进为 None，不再触发


def test_schedule_tick_at_most_one_run_even_on_second_pump(env):
    dispatcher, store, em = env["dispatcher"], env["store"], env["em"]
    dispatcher._agents[1] = "terminated"
    sid = store.schedule_create(session_id="s1", prompt="tick", kind="cron",
                                interval_expr="1h", next_tick="2020-01-01T00:00:00+00:00",
                                agent_id=1)
    first = em.schedule_pump(now_iso())
    second = em.schedule_pump(now_iso())
    assert len(first) == 1
    # 推进后 next_tick 在未来，第二次 pump 不再产生 Run
    assert len(second) == 0
    assert len(dispatcher.followups) == 1
    assert env["store"].run_get(first[0]["id"])["status"] == m.RUN_RUNNING


def test_schedule_claimed_tick_skipped(env):
    store, em = env["store"], env["em"]
    sid = store.schedule_create(session_id="s1", prompt="tick", kind="one_shot",
                                next_tick="2020-01-01T00:00:00+00:00", agent_id=1)
    assert store.schedule_claim(sid, "already-claimed") is True
    assert em.schedule_pump(now_iso()) == []
    assert len(env["dispatcher"].followups) == 0


def test_schedule_dispatch_failure_releases_claim(env):
    store, dispatcher, em = env["store"], env["dispatcher"], env["em"]
    dispatcher._agents[1] = "terminated"
    dispatcher.raise_on_followup = RuntimeError("boom")
    sid = store.schedule_create(session_id="s1", prompt="tick", kind="one_shot",
                                next_tick="2020-01-01T00:00:00+00:00", agent_id=1)
    runs = em.schedule_pump(now_iso())
    assert len(runs) == 1 and runs[0]["status"] == m.RUN_FAILED
    # claim 已释放：下一轮可重试（不会因为半执行被永久卡住）
    assert store.schedule_claim(sid, "retry") is True

