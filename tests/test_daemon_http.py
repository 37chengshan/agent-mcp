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
