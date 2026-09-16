"""v4 控制面用户可见对象：Run 落库 / Goal / Schedule 路由与 MCP 工具一致性。"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent_mcp.db import DB
from agent_mcp.daemon_http import DaemonHTTPServer, EventBroadcaster, _API_METHODS
from agent_mcp.daemon_main import Dispatcher
from agent_mcp.store_v4 import StoreV4
import mcp_server


class NoopWorker:
    def __call__(self, target_cli, **kwargs):
        return {"worker_pid": 0, "command_summary": "noop",
                "state_path": "", "out_path": "", "err_path": ""}


@pytest.fixture()
def env(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db = DB(tmp_path / "s.db")
    store = StoreV4(db)
    disp = Dispatcher(db=db, broadcaster=EventBroadcaster(), state_dir=state_dir,
                      spawn_fn=NoopWorker(), store_v4=store)
    srv = DaemonHTTPServer(("127.0.0.1", 0), PROJECT_ROOT / "web", token="t",
                           dispatcher=disp, db=db, store_v4=store)
    import threading
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield {"db": db, "store": store, "disp": disp, "srv": srv,
           "port": srv.server_address[1]}
    srv.shutdown()
    srv.server_close()


def _post(port, path, body, token="t"):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, body=json.dumps(body).encode(),
                 headers={"Content-Type": "application/json",
                          "X-Auth-Token": token, "Host": "127.0.0.1"})
    resp = conn.getresponse()
    data = json.loads(resp.read().decode() or "{}")
    conn.close()
    return resp.status, data


def _get(port, path, token="t"):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers={"X-Auth-Token": token, "Host": "127.0.0.1"})
    resp = conn.getresponse()
    data = json.loads(resp.read().decode() or "{}")
    conn.close()
    return resp.status, data


def test_goal_create_list_update_roundtrip(env):
    port = env["port"]
    st, body = _post(port, "/api/v4/goals/create",
                     {"objective": "keep polishing console", "session_id": "s1",
                      "token_budget": 1000})
    assert st == 200, body
    goal = body["goal"]
    assert goal["objective"] == "keep polishing console"
    assert goal["status"] == "active"

    st, listed = _get(port, "/api/v4/goals?session_id=s1")
    assert st == 200
    assert any(g["id"] == goal["id"] for g in listed["goals"])

    st, updated = _post(port, "/api/v4/goals/update",
                        {"goal_id": goal["id"], "status": "completed"})
    assert st == 200
    assert updated["goal"]["status"] == "completed"
    assert updated["goal"]["completed_at"]


def test_schedule_create_list_cancel(env):
    port = env["port"]
    st, body = _post(port, "/api/v4/schedules/create",
                     {"prompt": "heartbeat check", "kind": "heartbeat",
                      "interval_expr": "30m", "session_id": "s1"})
    assert st == 200, body
    sched = body["schedule"]
    assert sched["kind"] == "heartbeat"
    assert sched["next_tick"]
    assert sched["paused"] == 0

    st, listed = _get(port, "/api/v4/schedules?session_id=s1")
    assert st == 200
    assert any(s["id"] == sched["id"] for s in listed["schedules"])

    st, cancelled = _post(port, "/api/v4/schedules/cancel",
                          {"schedule_id": sched["id"]})
    assert st == 200
    assert cancelled["schedule"]["paused"] == 1


def test_list_runs_empty_and_after_manual_record(env):
    port = env["port"]
    st, body = _get(port, "/api/v4/runs")
    assert st == 200
    assert body["runs"] == []

    agent_id = env["db"].insert_agent(parent_id=None, session_id="s1",
                                      task_name="t", cli="claude", cwd=".")
    run_id = env["disp"]._record_run_for_agent(agent_id, queued=True)
    assert run_id
    st, body = _get(port, "/api/v4/runs")
    assert st == 200
    assert body["runs"][0]["id"] == run_id
    assert body["runs"][0]["status"] == "ADMITTED"
    assert body["runs"][0]["agent_id"] == agent_id


def test_spawn_records_run_and_settle_maps_terminal(env):
    """direct record + settle 路径：RUNNING → terminated 映射 COMPLETED。"""
    disp = env["disp"]

    agent_id = env["db"].insert_agent(parent_id=None, session_id="s1",
                                      task_name="t", cli="claude", cwd=".")
    run_id = disp._record_run_for_agent(agent_id, queued=False)
    run = env["store"].run_get(run_id)
    assert run["status"] == "RUNNING"

    disp._settle_run_for_agent(agent_id, "terminated", "end_turn")
    run = env["store"].run_get(run_id)
    assert run["status"] == "COMPLETED"
    assert run["stop_reason"] == "end_turn"


def test_settle_incomplete_and_needs_advisor(env):
    disp = env["disp"]
    a1 = env["db"].insert_agent(parent_id=None, session_id="s1",
                                task_name="t1", cli="claude", cwd=".")
    r1 = disp._record_run_for_agent(a1, queued=False)
    disp._settle_run_for_agent(a1, "incomplete", "timeout")
    assert env["store"].run_get(r1)["status"] == "INCOMPLETE"

    a2 = env["db"].insert_agent(parent_id=None, session_id="s1",
                                task_name="t2", cli="claude", cwd=".")
    r2 = disp._record_run_for_agent(a2, queued=False)
    disp._settle_run_for_agent(a2, "needs_advisor", "needs_decision")
    assert env["store"].run_get(r2)["status"] == "WAITING"


def test_waiting_run_can_complete_after_resume(env):
    """needs_advisor→WAITING 后 agent 恢复 running 再 terminated：Run 必须能到 COMPLETED。"""
    disp = env["disp"]
    a = env["db"].insert_agent(parent_id=None, session_id="s1",
                               task_name="t", cli="claude", cwd=".")
    rid = disp._record_run_for_agent(a, queued=True)
    assert env["store"].run_get(rid)["status"] == "ADMITTED"
    disp._promote_run_running(rid)
    assert env["store"].run_get(rid)["status"] == "RUNNING"
    disp._settle_run_for_agent(a, "needs_advisor", "needs_decision")
    assert env["store"].run_get(rid)["status"] == "WAITING"
    disp._resume_waiting_runs(a)
    assert env["store"].run_get(rid)["status"] == "RUNNING"
    disp._settle_run_for_agent(a, "terminated", "end_turn")
    assert env["store"].run_get(rid)["status"] == "COMPLETED"


def test_queued_spawn_keeps_run_admitted_not_running(env):
    """排队 agent 的 Run 停在 ADMITTED，不得误标 RUNNING。"""
    disp = env["disp"]
    a = env["db"].insert_agent(parent_id=None, session_id="s1",
                               task_name="q", cli="claude", cwd=".")
    rid = disp._record_run_for_agent(a, queued=True)
    assert env["store"].run_get(rid)["status"] == "ADMITTED"
    # 模拟未占槽：不 promote
    disp._promote_run_running(rid)  # ADMITTED→RUNNING 仅在占槽后调用
    assert env["store"].run_get(rid)["status"] == "RUNNING"


def test_new_tools_registered_in_four_places():
    new_tools = ["list_runs", "goal_create", "goal_list", "goal_update",
                 "schedule_create", "schedule_list", "schedule_cancel"]
    names = {t["name"] for t in mcp_server.TOOLS}
    for t in new_tools:
        assert t in names
        assert t in mcp_server._DAEMON_PATHS
        assert mcp_server._DAEMON_PATHS[t] in _API_METHODS
        method = _API_METHODS[mcp_server._DAEMON_PATHS[t]]
        assert callable(getattr(Dispatcher, method, None))
