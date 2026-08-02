#!/usr/bin/env python3
"""Agent MCP 薄层（stdio，零依赖，无状态）。

三主载体（codex/claude/omp）注册同一 MCP server；clientInfo.name 识别 host，
会话隔离（session_id = host + uuid，首次 tools/call 生成后透传）。

tools/call 全部映射到常驻 daemon 的 HTTP POST 端点（X-Auth-Token 认证）；
daemon 未起时 ensure_daemon() 原子拉起（探测 /health → 生成 token → spawn → 轮询）。
所有失败以结构化 {status:"error", summary, root_cause_hint?, next_actions} 返回，不抛异常。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

SERVER_VERSION = "2.0.0"
PROTOCOL_VERSION = "2025-03-26"
DAEMON_HOST = "127.0.0.1"
DAEMON_PORT = int(os.environ.get("AGENT_MCP_PORT", "8765"))
STATE_DIR = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "agent-mcp"
DAEMON_JSON = STATE_DIR / "daemon.json"
DAEMON_SCRIPT = Path(__file__).resolve().parent / "agent_mcp" / "daemon_main.py"
_PROBE_ATTEMPTS = 10
_PROBE_INTERVAL = 0.5
_HTTP_TIMEOUT = 60  # wait_agent 最长阻塞 30s，留足余量

_HOST = "unknown"
_SESSION_ID: str | None = None
_DAEMON: tuple[str, str] | None = None  # (base_url, token) 缓存

_DAEMON_PATHS = {
    "spawn_agent": "/api/agents/spawn",
    "send_message": "/api/agents/send_message",
    "followup_task": "/api/agents/followup",
    "wait_agent": "/api/agents/wait",
    "interrupt_agent": "/api/agents/interrupt",
    "list_agents": "/api/agents/list",
    "get_agent_activity": "/api/agents/activity",
    "get_token_usage": "/api/usage",
}

TOOLS = [
    {
        "name": "spawn_agent",
        "description": "创建任务 agent 并启动 CLI 子进程（槽位满则排队）。"
                       "target_cli 为 claude/grok/opencode/omp；context 注入父摘要；"
                       "resume 透传 CLI session id。返回 agent_id 用于后续监控。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_cli": {"type": "string", "enum": ["claude", "grok", "opencode", "omp"],
                               "description": "执行任务的 CLI。"},
                "prompt": {"type": "string", "description": "任务提示词。"},
                "task_name": {"type": "string", "description": "分层名称，如 /root/task1。"},
                "cwd": {"type": "string", "description": "工作目录。"},
                "permission_mode": {"type": "string", "enum": ["plan", "acceptEdits", "fullAccess"],
                                    "default": "plan", "description": "CLI 权限模式。"},
                "model": {"type": "string", "description": "CLI 使用的模型。"},
                "context": {"type": "string", "description": "父摘要文本，注入 prompt 前。"},
                "resume": {"type": "string", "description": "要恢复的 CLI session id。"},
                "max_turns": {"type": "integer", "minimum": 1, "maximum": 50},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 1800},
                "parent_agent_id": {"type": "integer", "description": "父 agent（同会话）。"},
                "session_id": {"type": "string", "description": "会话隔离键；缺省用宿主会话。"},
            },
            "required": ["target_cli", "prompt"],
            "additionalProperties": False,
        },
        "annotations": {"destructiveHint": True},
    },
    {
        "name": "send_message",
        "description": "投递消息到 daemon 消息队列：运行中挂起，终止后标 undelivered；"
                       "永不触发执行——只有 followup_task 会把挂起消息合并进新 run。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "integer", "description": "spawn_agent 返回的 agent_id。"},
                "message": {"type": "string", "description": "要投递的消息。"},
            },
            "required": ["agent_id", "message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "followup_task",
        "description": "唯一触发新 turn 的入口：合并该 agent 的挂起消息与 prompt 重新 spawn"
                       "（parent_id = 原 agent 的 parent）。运行中返回 error 提示先 wait/interrupt；"
                       "interrupt=true 先终止再重派。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "integer", "description": "要 followup 的 agent。"},
                "prompt": {"type": "string", "description": "新任务提示词。"},
                "interrupt": {"type": "boolean", "default": False,
                              "description": "先终止运行中的 agent 再重派。"},
            },
            "required": ["agent_id", "prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "wait_agent",
        "description": "短阻塞等待 agent 进入终止态（terminated/error/cancelled/incomplete），"
                       "最多 30 秒；返回状态 + 最新消息摘要（截断），不返回全文。超时返回当前状态。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "integer", "description": "spawn_agent 返回的 agent_id。"},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 30, "default": 30,
                            "description": "阻塞秒数（≤30）。"},
            },
            "required": ["agent_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "interrupt_agent",
        "description": "终止 agent 的进程树（SIGTERM→SIGKILL）并标记 cancelled（stop_reason=interrupted）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "integer", "description": "要中断的 agent。"},
            },
            "required": ["agent_id"],
            "additionalProperties": False,
        },
        "annotations": {"destructiveHint": True},
    },
    {
        "name": "list_agents",
        "description": "列出 agent 树：id/parent_id/task_name/cli/model/status/stop_reason/updated_at。"
                       "缺省返回当前宿主会话。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "会话过滤；缺省当前会话。"},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_agent_activity",
        "description": "agent 的实时活动流（事件按 seq 分页）+ 消息流分页。since_seq 用于增量拉取。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "integer"},
                "since_seq": {"type": "integer", "minimum": 0, "default": 0,
                              "description": "只返回 seq 更大的事件。"},
                "page": {"type": "integer", "minimum": 0, "default": 0},
                "size": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            },
            "required": ["agent_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_token_usage",
        "description": "token 统计（派发侧估算）：scope=agent（单任务）/subtree（含后代）/"
                       "global（会话或全部）。返回按 agent+model 拆分与 totals。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "integer", "description": "scope=agent|subtree 时必填。"},
                "scope": {"type": "string", "enum": ["agent", "subtree", "global"],
                          "default": "agent"},
                "session_id": {"type": "string", "description": "scope=global 时按会话过滤。"},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
]


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def result(request_id: Any, payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": is_error,
        },
    }


def rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def host_from_client_info(info: dict[str, Any] | None) -> str:
    """clientInfo.name → host（codex/claude/omp/unknown），子串匹配。"""
    name = str((info or {}).get("name") or "").lower()
    if "codex" in name:
        return "codex"
    if "claude" in name:
        return "claude"
    if "omp" in name:
        return "omp"
    return "unknown"


def _probe(base: str) -> bool:
    try:
        with urllib.request.urlopen(base + "/health", timeout=1) as resp:
            return resp.status == 200
    except Exception:
        return False


def _read_token() -> str:
    try:
        token = json.loads(DAEMON_JSON.read_text(encoding="utf-8")).get("token", "")
        return token if isinstance(token, str) and token else ""
    except (OSError, json.JSONDecodeError):
        return ""


def _ensure_token_file() -> str:
    """daemon.json 缺 token 时生成（0600）；daemon 启动时复用同一文件。"""
    token = _read_token()
    if not token:
        token = uuid.uuid4().hex
        DAEMON_JSON.parent.mkdir(parents=True, exist_ok=True)
        DAEMON_JSON.write_text(json.dumps({"token": token}), encoding="utf-8")
        if os.name != "nt":
            os.chmod(DAEMON_JSON, 0o600)
    return token


def _spawn_detached(command: list[str], *, env: dict[str, str] | None = None) -> None:
    """跨平台分离启动 daemon（薄层零依赖，不复用 agent_mcp.dispatch 的 psutil 路径）。"""
    kwargs: dict[str, Any] = dict(env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                   | getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(command, **kwargs)


def ensure_daemon() -> tuple[str, str]:
    """原子拉起：探测 /health（10×0.5s）→ 无则补 token 文件 → spawn_detached(daemon_main)
    → 轮询 /health。锁文件残留校验在 daemon_main 内部，薄层不重复。返回 (base_url, token)。"""
    base = f"http://{DAEMON_HOST}:{DAEMON_PORT}"
    token = _read_token()
    spawned = False
    for _ in range(_PROBE_ATTEMPTS):
        if _probe(base):
            return base, token
        if not spawned:
            token = _ensure_token_file()
            _spawn_detached([sys.executable, str(DAEMON_SCRIPT),
                             "--port", str(DAEMON_PORT), "--state-dir", str(STATE_DIR)])
            spawned = True
        time.sleep(_PROBE_INTERVAL)
    raise RuntimeError(f"agent-mcp daemon failed to start on {base} within "
                       f"{_PROBE_ATTEMPTS * _PROBE_INTERVAL:.0f}s")


def _session_id() -> str:
    global _SESSION_ID
    if _SESSION_ID is None:
        _SESSION_ID = f"{_HOST}-{uuid.uuid4().hex}"
    return _SESSION_ID


def _post_once(base: str, token: str, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """单次 HTTP POST；连接失败返回 None（触发重拉），HTTP 错误转结构化错误。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=body, method="POST",
        headers={"X-Auth-Token": token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        return {"status": "error", "summary": f"daemon returned HTTP {exc.code}",
                "root_cause_hint": detail or None,
                "next_actions": ["check the daemon log and auth token"]}
    except (urllib.error.URLError, OSError, TimeoutError):
        return None


def _daemon_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """调用 daemon；连接失败先失效缓存重新拉起，再重试一次。"""
    global _DAEMON
    if _DAEMON is None:
        _DAEMON = ensure_daemon()
    base, token = _DAEMON
    out = _post_once(base, token, path, payload)
    if out is not None:
        return out
    _DAEMON = None  # daemon 可能已退出，重新拉起
    try:
        base, token = ensure_daemon()
    except RuntimeError as exc:
        return {"status": "error", "summary": "agent-mcp daemon is not reachable",
                "root_cause_hint": str(exc),
                "next_actions": ["start the daemon manually: "
                                 "python agent_mcp/daemon_main.py"]}
    out = _post_once(base, token, path, payload)
    if out is None:
        return {"status": "error", "summary": "agent-mcp daemon unreachable after relaunch",
                "root_cause_hint": "daemon started but connection failed",
                "next_actions": ["check for port conflicts on the daemon port"]}
    return out


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    path = _DAEMON_PATHS.get(name)
    if path is None:
        raise ValueError(f"unknown tool: {name}")
    payload = dict(arguments)
    payload.setdefault("session_id", _session_id())
    return _daemon_post(path, payload)


def handle(request: dict[str, Any], *, emit=send) -> None:
    request_id = request.get("id")
    method = request.get("method")
    if method == "initialize":
        global _HOST
        params = request.get("params")
        if not isinstance(params, dict):
            params = {}
        _HOST = host_from_client_info(params.get("clientInfo"))
        emit({"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": {"name": "agent-mcp", "version": SERVER_VERSION},
            "capabilities": {"tools": {"listChanged": False}}},
        })
    elif method == "tools/list":
        emit({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = request.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("arguments", {}), dict):
            emit(rpc_error(request_id, -32602, "Invalid tool arguments"))
            return
        name = params.get("name")
        arguments = params.get("arguments", {})
        if name not in _DAEMON_PATHS:
            emit(rpc_error(request_id, -32602, "Unknown tool"))
            return
        payload = call_tool(name, arguments)
        emit(result(request_id, payload, is_error=payload.get("status") == "error"))
    elif request_id is not None:
        emit(rpc_error(request_id, -32601, f"Method not found: {method}"))


def main() -> int:
    for line in sys.stdin:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            handle(parsed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
