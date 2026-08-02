"""T10 MCP 薄层测试：JSON-RPC 协议层 + host 识别 + 原子拉起 + 工具面映射。

不依赖真实 daemon 进程：ensure_daemon/_daemon_post/_post_once 全部 monkeypatch。
"""
import json

import pytest

import mcp_server


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """重置模块级会话变量，避免测试间串扰。"""
    monkeypatch.setattr(mcp_server, "_SESSION_ID", None)
    monkeypatch.setattr(mcp_server, "_HOST", "unknown")
    monkeypatch.setattr(mcp_server, "_DAEMON", None)


# ---- initialize / tools/list / host ----

def test_initialize_returns_server_info():
    out = []
    mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"clientInfo": {"name": "codex"}}}, emit=out.append)
    msg = out[0]
    assert msg["id"] == 1
    assert msg["result"]["serverInfo"] == {"name": "agent-mcp", "version": "2.0.0"}
    assert msg["result"]["protocolVersion"] == "2025-03-26"
    assert mcp_server._HOST == "codex"


def test_tools_list_has_eight_tools_in_order():
    names = [t["name"] for t in mcp_server.TOOLS]
    assert names == ["spawn_agent", "send_message", "followup_task", "wait_agent",
                     "interrupt_agent", "list_agents", "get_agent_activity",
                     "get_token_usage"]


def test_host_from_client_info():
    assert mcp_server.host_from_client_info({"name": "codex"}) == "codex"
    assert mcp_server.host_from_client_info({"name": "claude-ai"}) == "claude"
    assert mcp_server.host_from_client_info({"name": "omp"}) == "omp"
    assert mcp_server.host_from_client_info({"name": "some-other-app"}) == "unknown"
    assert mcp_server.host_from_client_info(None) == "unknown"


# ---- tools/call 映射与会话 ----

def test_tools_call_maps_to_daemon_path_and_injects_session(monkeypatch):
    captured = {}

    def fake_post(path, payload):
        captured["path"] = path
        captured["payload"] = payload
        return {"status": "running", "agent_id": 7}

    monkeypatch.setattr(mcp_server, "_daemon_post", fake_post)
    out = []
    mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                       "params": {"name": "spawn_agent",
                                  "arguments": {"target_cli": "claude", "prompt": "hi"}}},
                      emit=out.append)
    msg = out[0]
    assert msg["id"] == 2
    assert captured["path"] == "/api/agents/spawn"
    assert captured["payload"]["session_id"].startswith("unknown-")
    body = json.loads(msg["result"]["content"][0]["text"])
    assert body["status"] == "running"


def test_session_id_persists_across_calls(monkeypatch):
    captured = []

    def fake_post(path, payload):
        captured.append(payload)
        return {"status": "ok"}

    monkeypatch.setattr(mcp_server, "_daemon_post", fake_post)
    mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"clientInfo": {"name": "codex"}}}, emit=lambda m: None)
    mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                       "params": {"name": "spawn_agent",
                                  "arguments": {"target_cli": "claude", "prompt": "a"}}},
                      emit=lambda m: None)
    mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "wait_agent", "arguments": {"agent_id": 1}}},
                      emit=lambda m: None)
    assert captured[0]["session_id"].startswith("codex-")
    assert captured[0]["session_id"] == captured[1]["session_id"]


def test_daemon_structured_error_marks_is_error(monkeypatch):
    monkeypatch.setattr(mcp_server, "_daemon_post",
                        lambda path, payload: {"status": "error",
                                               "summary": "daemon returned HTTP 401",
                                               "root_cause_hint": "bad token",
                                               "next_actions": ["check token"]})
    out = []
    mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                       "params": {"name": "interrupt_agent",
                                  "arguments": {"agent_id": 1}}}, emit=out.append)
    msg = out[0]
    assert msg["result"]["isError"] is True
    body = json.loads(msg["result"]["content"][0]["text"])
    assert body["status"] == "error"
    assert body["next_actions"]


def test_unknown_tool_rpc_error():
    out = []
    mcp_server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                       "params": {"name": "nope", "arguments": {}}}, emit=out.append)
    msg = out[0]
    assert "error" in msg
    assert msg["error"]["code"] == -32602


# ---- ensure_daemon 原子拉起 ----

def test_ensure_daemon_probe_alive(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server, "STATE_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "DAEMON_JSON", tmp_path / "daemon.json")
    monkeypatch.setattr(mcp_server, "DAEMON_PORT", 8765)
    spawned = []

    def fake_spawn(cmd, **kw):
        spawned.append(cmd)

    monkeypatch.setattr(mcp_server, "_probe", lambda base: True)
    monkeypatch.setattr(mcp_server, "_spawn_detached", fake_spawn)
    base, token = mcp_server.ensure_daemon()
    assert base == "http://127.0.0.1:8765"
    assert spawned == []  # 已存活，不拉起


def test_ensure_daemon_spawns_when_down_and_writes_token(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    spawned = []
    monkeypatch.setattr(mcp_server, "STATE_DIR", state_dir)
    monkeypatch.setattr(mcp_server, "DAEMON_JSON", state_dir / "daemon.json")
    monkeypatch.setattr(mcp_server, "DAEMON_SCRIPT", tmp_path / "daemon_main.py")
    monkeypatch.setattr(mcp_server, "DAEMON_PORT", 8765)
    probes = iter([False, False, True, True])  # 首次探测失败触发 spawn，随后存活
    monkeypatch.setattr(mcp_server, "_probe", lambda base: next(probes))

    def fake_spawn(cmd, **kw):
        spawned.append(cmd)

    monkeypatch.setattr(mcp_server, "_spawn_detached", fake_spawn)
    base, token = mcp_server.ensure_daemon()
    assert base == "http://127.0.0.1:8765"
    assert len(spawned) == 1
    cmd = spawned[0]
    assert any("daemon_main.py" in c for c in cmd)
    assert "--port" in cmd and "8765" in cmd
    assert (state_dir / "daemon.json").is_file()
    assert token == json.loads((state_dir / "daemon.json").read_text())["token"]


def test_ensure_daemon_fails_after_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server, "STATE_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "DAEMON_JSON", tmp_path / "daemon.json")
    monkeypatch.setattr(mcp_server, "DAEMON_SCRIPT", tmp_path / "daemon_main.py")
    spawned = []

    def fake_spawn(cmd, **kw):
        spawned.append(cmd)

    monkeypatch.setattr(mcp_server, "_probe", lambda base: False)
    monkeypatch.setattr(mcp_server, "_spawn_detached", fake_spawn)
    monkeypatch.setattr(mcp_server.time, "sleep", lambda s: None)  # 加速
    with pytest.raises(RuntimeError, match="failed to start"):
        mcp_server.ensure_daemon()
    assert len(spawned) == 1  # 只拉起一次


def test_ensure_daemon_reuses_existing_token(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "daemon.json").write_text(json.dumps({"token": "keepme"}))
    monkeypatch.setattr(mcp_server, "STATE_DIR", state_dir)
    monkeypatch.setattr(mcp_server, "DAEMON_JSON", state_dir / "daemon.json")
    monkeypatch.setattr(mcp_server, "_probe", lambda base: True)
    _, token = mcp_server.ensure_daemon()
    assert token == "keepme"


# ---- _daemon_post 重试与错误 ----

def test_daemon_post_retries_after_connection_failure(monkeypatch):
    calls = []

    def fake_post_once(base, token, path, payload):
        calls.append((base, token))
        return None if len(calls) == 1 else {"status": "ok"}

    monkeypatch.setattr(mcp_server, "_post_once", fake_post_once)
    monkeypatch.setattr(mcp_server, "ensure_daemon", lambda: ("http://x", "t"))
    out = mcp_server._daemon_post("/api/agents/spawn", {})
    assert out == {"status": "ok"}
    assert len(calls) == 2


def test_daemon_post_http_error_structured(monkeypatch):
    monkeypatch.setattr(mcp_server, "_post_once",
                        lambda base, token, path, payload:
                        {"status": "error", "summary": "daemon returned HTTP 401",
                         "next_actions": ["check the daemon log and auth token"]})
    monkeypatch.setattr(mcp_server, "ensure_daemon", lambda: ("http://x", "t"))
    out = mcp_server._daemon_post("/api/agents/spawn", {})
    assert out["status"] == "error"


def test_main_reads_stdin_lines(monkeypatch, capsys):
    lines = iter([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        "not-json\n",
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ])
    monkeypatch.setattr(mcp_server.sys, "stdin", lines)
    assert mcp_server.main() == 0
    out = capsys.readouterr().out
    assert '"serverInfo"' in out
    assert '"tools"' in out
    assert out.count("\n") == 2  # 非 JSON 行被跳过，只响应两条
