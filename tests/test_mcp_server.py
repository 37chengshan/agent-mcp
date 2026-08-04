"""T10 MCP 薄层测试：JSON-RPC 协议层 + host 识别 + 原子拉起 + 工具面映射。

不依赖真实 daemon 进程：ensure_daemon/_daemon_post/_post_once 全部 monkeypatch。
"""
import json
from pathlib import Path

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
    assert msg["result"]["serverInfo"] == {"name": "agent-mcp", "version": "2.1.0"}
    assert msg["result"]["protocolVersion"] == "2025-03-26"
    assert mcp_server._HOST == "codex"


def test_tools_list_has_nine_tools_in_order():
    names = [t["name"] for t in mcp_server.TOOLS]
    assert names == ["spawn_agent", "send_message", "steer_agent", "followup_task",
                     "wait_agent", "interrupt_agent", "list_agents",
                     "get_agent_activity", "get_token_usage"]


def test_spawn_schema_requires_cwd():
    """与 daemon Dispatcher 对齐：cwd 必填（缺失时 daemon 返回 400）。"""
    schema = next(t for t in mcp_server.TOOLS if t["name"] == "spawn_agent")["inputSchema"]
    assert "cwd" in schema["required"]
    assert set(schema["required"]) == {"target_cli", "prompt", "cwd"}

def test_spawn_schema_lists_atomcode_task_cli():
    schema = next(tool for tool in mcp_server.TOOLS if tool["name"] == "spawn_agent")["inputSchema"]
    assert schema["properties"]["target_cli"]["enum"] == [
        "claude", "grok", "opencode", "omp", "atomcode"
    ]


def test_wait_agent_schema_timeout_custom_cap():
    """wait_agent 单次等待上限可自定义：schema maximum 跟随 MAX_WAIT_SECONDS（>30），默认仍 30。"""
    tool = next(t for t in mcp_server.TOOLS if t["name"] == "wait_agent")
    schema = tool["inputSchema"]
    prop = schema["properties"]["timeout"]
    assert prop["minimum"] == 1
    assert prop["default"] == 30
    assert prop["maximum"] == int(mcp_server.MAX_WAIT_SECONDS)
    assert prop["maximum"] > 30  # 不再硬编码 30s 上限
    cap = f"{mcp_server.MAX_WAIT_SECONDS:.0f}"
    assert cap in tool["description"] or cap in prop["description"]


def test_server_discover_advertises_dual_era_and_task_extension():
    out = []
    mcp_server.handle({"jsonrpc": "2.0", "id": "d1", "method": "server/discover",
                       "params": {"_meta": {
                           "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                           "io.modelcontextprotocol/clientInfo": {"name": "codex", "version": "1"},
                           "io.modelcontextprotocol/clientCapabilities": {"extensions": {
                               "io.modelcontextprotocol/tasks": {}}}}}}, emit=out.append)
    result = out[0]["result"]
    assert result["resultType"] == "complete"
    assert result["supportedVersions"] == ["2026-07-28", "2025-03-26"]
    assert "io.modelcontextprotocol/tasks" in result["capabilities"]["extensions"]
    assert result["ttlMs"] > 0 and result["cacheScope"] == "public"


def test_modern_tools_list_has_cache_and_result_type():
    out = []
    mcp_server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/list",
                       "params": {"_meta": {
                           "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                           "io.modelcontextprotocol/clientCapabilities": {}}}}, emit=out.append)
    result = out[0]["result"]
    assert result["resultType"] == "complete"
    assert result["ttlMs"] > 0 and result["cacheScope"] == "public"


def test_modern_request_rejects_unsupported_protocol_version():
    out = []
    mcp_server.handle({"jsonrpc": "2.0", "id": 10, "method": "tools/list",
                       "params": {"_meta": {
                           "io.modelcontextprotocol/protocolVersion": "1900-01-01",
                           "io.modelcontextprotocol/clientCapabilities": {}}}}, emit=out.append)
    assert out[0]["error"]["code"] == -32022
    assert out[0]["error"]["data"]["supported"] == ["2026-07-28", "2025-03-26"]


def test_steer_agent_tool_maps_to_daemon():
    assert "steer_agent" in [tool["name"] for tool in mcp_server.TOOLS]
    assert mcp_server._DAEMON_PATHS["steer_agent"] == "/api/agents/steer"


def _modern_task_meta():
    return {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {"extensions": {
                "io.modelcontextprotocol/tasks": {}}}}


def test_modern_spawn_with_tasks_capability_returns_flat_durable_task(monkeypatch):
    monkeypatch.setattr(mcp_server, "call_tool", lambda name, args: {
        "agent_id": 7, "status": "running", "pid": 123,
        "created_at": "2026-08-04T10:00:00+00:00",
        "updated_at": "2026-08-04T10:00:01+00:00"})
    out = []
    mcp_server.handle({"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                       "params": {"name": "spawn_agent", "arguments": {
                           "target_cli": "claude", "prompt": "x", "cwd": "/tmp"},
                           "_meta": _modern_task_meta()}}, emit=out.append)
    task = out[0]["result"]
    assert task["resultType"] == "task"
    assert "task" not in task
    assert task["taskId"] == "agent:7"
    assert task["status"] == "working"
    assert task["createdAt"] == "2026-08-04T10:00:00+00:00"
    assert task["lastUpdatedAt"] == "2026-08-04T10:00:01+00:00"


def test_tasks_get_is_complete_flat_task(monkeypatch):
    monkeypatch.setattr(mcp_server, "call_tool", lambda name, args: {
        "agent_id": 7, "status": "running",
        "created_at": "2026-08-04T10:00:00+00:00",
        "updated_at": "2026-08-04T10:00:01+00:00"})
    out = []
    mcp_server.handle({"jsonrpc": "2.0", "id": 12, "method": "tasks/get",
                       "params": {"taskId": "agent:7", "_meta": _modern_task_meta()}},
                      emit=out.append)
    task = out[0]["result"]
    assert task["resultType"] == "complete"
    assert "task" not in task
    assert task["taskId"] == "agent:7"


def test_tasks_update_accepts_arbitrary_input_and_cancel_interrupts(monkeypatch):
    calls = []
    monkeypatch.setattr(mcp_server, "call_tool",
                        lambda name, args: calls.append((name, args)) or {"status": "running"})
    for method, params in [
        ("tasks/update", {"taskId": "agent:7", "inputResponses": {
            "steer": {"action": "accept", "content": {"input": "改做 B"}}},
            "_meta": _modern_task_meta()}),
        ("tasks/cancel", {"taskId": "agent:7", "_meta": _modern_task_meta()}),
    ]:
        out = []
        mcp_server.handle({"jsonrpc": "2.0", "id": method, "method": method,
                           "params": params}, emit=out.append)
        assert out[0]["result"]["resultType"] == "complete"
    assert calls == [("steer_agent", {"agent_id": 7, "message": "改做 B"}),
                     ("interrupt_agent", {"agent_id": 7})]


@pytest.mark.parametrize("method", ["tasks/get", "tasks/update", "tasks/cancel"])
def test_task_methods_require_negotiated_capability(method):
    out = []
    mcp_server.handle({"jsonrpc": "2.0", "id": method, "method": method,
                       "params": {"taskId": "agent:7"}}, emit=out.append)
    assert out[0]["error"]["code"] == -32023


@pytest.mark.parametrize("method", ["tasks/get", "tasks/update", "tasks/cancel"])
def test_task_methods_propagate_daemon_errors(monkeypatch, method):
    monkeypatch.setattr(mcp_server, "call_tool", lambda name, args: {
        "status": "error", "http_status": 400, "summary": "agent 999 not found"})
    params = {"taskId": "agent:999", "_meta": _modern_task_meta()}
    if method == "tasks/update":
        params["inputResponses"] = {"x": {"action": "accept", "content": "retry"}}
    out = []
    mcp_server.handle({"jsonrpc": "2.0", "id": method, "method": method,
                       "params": params}, emit=out.append)
    assert out[0]["error"]["code"] == -32602
    assert "not found" in out[0]["error"]["message"]


def test_call_tool_wait_agent_overrides_http_timeout(monkeypatch):
    """wait_agent 按请求 timeout 叠加 HTTP 层超时（避免 daemon 等待时请求被掐断）。"""
    captured = {}

    def fake_post(path, payload, http_timeout=None):
        captured["path"] = path
        captured["payload"] = payload
        captured["http_timeout"] = http_timeout
        return {"status": "running"}

    monkeypatch.setattr(mcp_server, "_daemon_post", fake_post)
    mcp_server.call_tool("wait_agent", {"agent_id": 7, "timeout": 120})
    assert captured["path"] == "/api/agents/wait"
    assert captured["payload"]["timeout"] == 120
    assert captured["http_timeout"] == mcp_server._HTTP_TIMEOUT + 120
    # 超上限时钳制到 MAX_WAIT_SECONDS
    mcp_server.call_tool("wait_agent", {"agent_id": 8, "timeout": 99999})
    assert captured["http_timeout"] == mcp_server._HTTP_TIMEOUT + mcp_server.MAX_WAIT_SECONDS
    # 非 wait 工具不叠加
    mcp_server.call_tool("list_agents", {})
    assert captured["http_timeout"] is None


def test_host_from_client_info():
    assert mcp_server.host_from_client_info({"name": "codex"}) == "codex"
    assert mcp_server.host_from_client_info({"name": "claude-ai"}) == "claude"
    assert mcp_server.host_from_client_info({"name": "omp"}) == "omp"
    assert mcp_server.host_from_client_info({"name": "some-other-app"}) == "unknown"
    assert mcp_server.host_from_client_info(None) == "unknown"


# ---- tools/call 映射与会话 ----

def test_tools_call_maps_to_daemon_path_and_injects_session(monkeypatch):
    captured = {}

    def fake_post(path, payload, http_timeout=None):
        captured["path"] = path
        captured["payload"] = payload
        captured["http_timeout"] = http_timeout
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

    def fake_post(path, payload, http_timeout=None):
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
                        lambda path, payload, http_timeout=None: {"status": "error",
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

    def fake_post_once(base, token, path, payload, http_timeout=None):
        calls.append((base, token))
        return None if len(calls) == 1 else {"status": "ok"}

    monkeypatch.setattr(mcp_server, "_post_once", fake_post_once)
    monkeypatch.setattr(mcp_server, "ensure_daemon", lambda: ("http://x", "t"))
    out = mcp_server._daemon_post("/api/agents/spawn", {})
    assert out == {"status": "ok"}
    assert len(calls) == 2


def test_daemon_post_http_error_structured(monkeypatch):
    monkeypatch.setattr(mcp_server, "_post_once",
                        lambda base, token, path, payload, http_timeout=None:
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


def test_state_dir_prefers_agent_mcp_home_over_codex_home(monkeypatch):
    monkeypatch.delenv("AGENT_MCP_HOME", raising=False)
    monkeypatch.setenv("CODEX_HOME", "/tmp/codexhome")
    assert mcp_server.state_dir_from_env() == Path("/tmp/codexhome") / "agent-mcp"
    monkeypatch.setenv("AGENT_MCP_HOME", "/tmp/amh")
    assert mcp_server.state_dir_from_env() == Path("/tmp/amh") / "agent-mcp"
