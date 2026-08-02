import json
import threading
import urllib.error
import urllib.request

import pytest

from agent_mcp.daemon_http import DaemonHTTPServer, EventBroadcaster


def test_broadcaster_connect_limit_and_close():
    b = EventBroadcaster(max_clients=2)
    c1 = b.connect()
    c2 = b.connect()
    assert c1 is not None and c2 is not None
    assert b.connect() is None  # 超限
    b.close(c1)
    assert b.connect() is not None


def test_broadcaster_publish_and_heartbeat():
    b = EventBroadcaster(max_clients=2)
    c = b.connect()
    b.publish({"type": "agent.message", "agent_id": 1}, seq=1)
    b.heartbeat_all()
    joined = "".join(c["buffer"])
    assert "id: 1" in joined and "agent.message" in joined and ": ping" in joined


def _make_server(tmp_path):
    from agent_mcp.db import DB
    srv = DaemonHTTPServer(("127.0.0.1", 0), tmp_path, token="t",
                           db=DB(tmp_path / "test.db"), dispatcher=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv


def test_health_endpoint(tmp_path):
    srv = _make_server(tmp_path)
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/health")
        assert resp.status == 200
        body = json.loads(resp.read())
        assert body["ok"] is True
    finally:
        srv.shutdown()


def test_bad_host_rejected(tmp_path):
    # 显式无代理 opener：本机系统代理会拦截 evil Host 头的连接，干扰测试
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    srv = _make_server(tmp_path)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/health",
            headers={"Host": "evil.example.com"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            opener.open(req)
        assert exc.value.code == 400
    finally:
        srv.shutdown()


def test_post_without_token_unauthorized(tmp_path):
    srv = _make_server(tmp_path)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/api/agents/spawn",
            data=b"{}")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req)
        assert exc.value.code == 401
    finally:
        srv.shutdown()


def test_post_with_token_dispatcher_not_ready(tmp_path):
    srv = _make_server(tmp_path)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/api/agents/spawn",
            data=b"{}", headers={"X-Auth-Token": "t"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req)
        assert exc.value.code == 503
        body = json.loads(exc.value.read())
        assert body["error"] == "dispatcher not ready"
    finally:
        srv.shutdown()


def test_snapshot_returns_agents_events_usage(tmp_path):
    srv = _make_server(tmp_path)
    try:
        aid = srv.db.insert_agent(parent_id=None, session_id="snap1", task_name="t",
                                  cli="claude", model="m", cwd=str(tmp_path))
        srv.db.set_status(aid, "terminated", stop_reason="end_turn", pid=1)
        srv.db.insert_event(agent_id=aid, type="agent.message",
                            payload={"text": "hi"}, session_id="snap1")
        srv.db.upsert_usage(agent_id=aid, model="aggregate", input_tokens=10,
                            output_tokens=5, cache_creation=0, cache_read=2,
                            cost_usd=0.1)
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{srv.server_address[1]}/api/snapshot?session_id=snap1")
        body = json.loads(resp.read())
        assert [a["id"] for a in body["agents"]] == [aid]
        assert body["agents"][0]["status"] == "terminated"
        assert body["agents"][0]["stop_reason"] == "end_turn"
        assert body["events"][-1]["type"] == "agent.message"
        assert body["events"][-1]["payload"]["text"] == "hi"
        assert body["usage"]["totals"]["input_tokens"] == 10
        assert body["usage"]["per_agent"][0]["output_tokens"] == 5
        assert body["last_seq"] == body["events"][-1]["seq"]
    finally:
        srv.shutdown()


def test_snapshot_no_token_and_session_filter(tmp_path):
    srv = _make_server(tmp_path)
    try:
        srv.db.insert_agent(parent_id=None, session_id="only", task_name="a",
                            cli="claude", model=None, cwd=str(tmp_path))
        # 无 token 可访问（GET 只读）
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{srv.server_address[1]}/api/snapshot")
        body = json.loads(resp.read())
        assert [a["task_name"] for a in body["agents"]] == ["a"]
        # 指定不存在的 session → 400
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/api/snapshot?session_id=nope")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req)
        assert exc.value.code == 400
    finally:
        srv.shutdown()
