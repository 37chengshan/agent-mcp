#!/usr/bin/env python3
"""Agent MCP 薄层（stdio，零依赖，无状态）。

三主载体（codex/claude/omp）注册同一 MCP server；clientInfo.name 识别 host，
会话隔离（session_id = host + uuid，首次 tools/call 生成后透传）。

tools/call 全部映射到常驻 daemon 的 HTTP POST 端点（X-Auth-Token 认证）；
daemon 未起时 ensure_daemon() 原子拉起（探测 /health → 生成 token → spawn → 轮询）。
所有失败以结构化 {status:"error", summary, root_cause_hint?, next_actions} 返回，不抛异常。
"""
from __future__ import annotations

import hashlib
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

SERVER_VERSION = "2.1.0"
LEGACY_PROTOCOL_VERSION = "2025-03-26"
MODERN_PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_PROTOCOL_VERSIONS = [MODERN_PROTOCOL_VERSION, LEGACY_PROTOCOL_VERSION]
PROTOCOL_VERSION = LEGACY_PROTOCOL_VERSION
DAEMON_HOST = "127.0.0.1"
DAEMON_PORT = int(os.environ.get("AGENT_MCP_PORT", "8765"))


def state_dir_from_env() -> Path:
    """AGENT_MCP_HOME 优先；兼容 CODEX_HOME；缺省 ~/.codex。与 daemon_main 同口径。"""
    base = (os.environ.get("AGENT_MCP_HOME")
            or os.environ.get("CODEX_HOME")
            or Path.home() / ".codex")
    return Path(base) / "agent-mcp"


STATE_DIR = state_dir_from_env()
DAEMON_JSON = STATE_DIR / "daemon.json"
DAEMON_SCRIPT = Path(__file__).resolve().parent / "agent_mcp" / "daemon_main.py"
_PROBE_ATTEMPTS = 10
_PROBE_INTERVAL = 0.5
_HTTP_TIMEOUT = 60  # 常规请求基础超时；wait_agent 按请求时长叠加（见 call_tool）
# wait_agent 单次阻塞上限：默认 600s（10 分钟），与 daemon 侧 AGENT_MCP_MAX_WAIT 同口径
MAX_WAIT_SECONDS = float(os.environ.get("AGENT_MCP_MAX_WAIT", "600"))

_HOST = "unknown"
_SESSION_ID: str | None = None
_DAEMON: tuple[str, str] | None = None  # (base_url, token) 缓存

_DAEMON_PATHS = {
    "spawn_agent": "/api/agents/spawn",
    "send_message": "/api/agents/send_message",
    "steer_agent": "/api/agents/steer",
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
        "description": "创建任务 agent 并启动 CLI 子进程（槽位满则排队，返回 status=queued）。"
                       "target_cli 为 claude/grok/opencode/omp/atomcode；context 注入父摘要；"
                       "resume 透传 CLI session id（AtomCode 不支持稳定 session-id resume）。"
                       "返回 agent_id 用于后续监控。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_cli": {"type": "string",
                               "enum": ["claude", "grok", "opencode", "omp", "atomcode"],
                               "description": "执行任务的 CLI。"},
                "prompt": {"type": "string", "description": "任务提示词。"},
                "task_name": {"type": "string", "description": "分层名称，如 /root/task1。"},
                "cwd": {"type": "string", "description": "工作目录（必填，daemon 校验）。"},
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
            "required": ["target_cli", "prompt", "cwd"],
            "additionalProperties": False,
        },
        "annotations": {"destructiveHint": True},
    },
    {
        "name": "send_message",
        "description": "投递消息到 daemon 消息队列：运行中返回 delivered，终止后返回 undelivered；"
                       "永不触发执行——只有 followup_task 会把消息合并进新 run。",
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
        "name": "steer_agent",
        "title": "Steer running agent",
        "description": "中途插话：若 agent 正在运行，先终止当前 run，再在同一节点立即开始下一 turn；"
                       "支持稳定 session id 的 CLI 自动恢复原会话。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "integer", "description": "要插话的 agent。"},
                "message": {"type": "string", "description": "新的方向或补充要求。"},
            },
            "required": ["agent_id", "message"],
            "additionalProperties": False,
        },
        "annotations": {"destructiveHint": True},
    },
    {
        "name": "followup_task",
        "description": "唯一触发新 turn 的入口：合并该 agent 的挂起消息与 prompt 重新 spawn"
                       "（复用同一 agent 节点）。运行中返回 queued，当前 run 结束后自动串联；"
                       "interrupt=true 先终止再立即重派。返回 merged_messages 计合并条数。",
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
                       f"timeout 可自定义（上限 {MAX_WAIT_SECONDS:.0f} 秒）。terminated 返回最新输出摘要（截断）；"
                       "error 返回错误信息；超时返回当前状态 + hint 轮询指引。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "integer", "description": "spawn_agent 返回的 agent_id。"},
                "timeout": {"type": "integer", "minimum": 1,
                            "maximum": int(MAX_WAIT_SECONDS), "default": 30,
                            "description": f"阻塞秒数（≤{MAX_WAIT_SECONDS:.0f}，默认 30）。"},
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
        "description": "agent 的实时活动流（规范化事件按 seq 排序）。since_seq 用于增量拉取，"
                       "返回 events + next_seq。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "integer"},
                "since_seq": {"type": "integer", "minimum": 0, "default": 0,
                              "description": "只返回 seq 更大的事件。"},
            },
            "required": ["agent_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_token_usage",
        "description": "token 统计（派发侧估算，estimated=true）：agent_id 指定单 agent；"
                       "缺省聚合会话（session_id 过滤）或全局。返回四字段 tokens + cost_usd。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "integer", "description": "指定单 agent 的 usage。"},
                "session_id": {"type": "string", "description": "缺省 agent_id 时按会话过滤。"},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
]


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def result(request_id: Any, payload: dict[str, Any], *, is_error: bool = False,
           modern: bool = False) -> dict[str, Any]:
    value: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": is_error,
    }
    if modern:
        value["resultType"] = "complete"
        value["structuredContent"] = payload
        value["_meta"] = {"io.modelcontextprotocol/serverInfo": {
            "name": "agent-mcp", "version": SERVER_VERSION}}
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _request_protocol(request: dict[str, Any]) -> str | None:
    params = request.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    if not isinstance(meta, dict):
        return None
    version = meta.get("io.modelcontextprotocol/protocolVersion")
    return str(version) if version else None


def _modern_meta() -> dict[str, Any]:
    return {"io.modelcontextprotocol/serverInfo": {
        "name": "agent-mcp", "version": SERVER_VERSION}}


def _modern_discover() -> dict[str, Any]:
    return {
        "resultType": "complete",
        "supportedVersions": SUPPORTED_PROTOCOL_VERSIONS,
        "capabilities": {"tools": {"listChanged": False}, "extensions": {
            "io.modelcontextprotocol/tasks": {}}},
        "_meta": _modern_meta(),
        "instructions": "派发和恢复长任务；steer_agent 用于中途改向，followup_task 用于继续会话。",
        "ttlMs": 300_000,
        "cacheScope": "public",
    }


def _unsupported_version(request_id: Any, requested: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {
        "code": -32022, "message": "Unsupported protocol version",
        "data": {"supported": SUPPORTED_PROTOCOL_VERSIONS, "requested": requested}}}



def _tasks_supported(request: dict[str, Any]) -> bool:
    params = request.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    caps = meta.get("io.modelcontextprotocol/clientCapabilities") if isinstance(meta, dict) else None
    exts = caps.get("extensions") if isinstance(caps, dict) else None
    return isinstance(exts, dict) and "io.modelcontextprotocol/tasks" in exts


def _agent_id_from_task(params: dict[str, Any]) -> int:
    task_id = str(params.get("taskId") or "")
    if not task_id.startswith("agent:"):
        raise ValueError("invalid taskId")
    return int(task_id.split(":", 1)[1])


def _task_status(payload: dict[str, Any]) -> str:
    status = payload.get("status")
    return {"running": "working", "queued": "working", "terminated": "completed",
            "error": "failed", "cancelled": "cancelled", "incomplete": "failed"}.get(status, "working")


def _task_result(payload: dict[str, Any], *, result_type: str = "task") -> dict[str, Any]:
    agent_id = int(payload["agent_id"])
    created = str(payload.get("created_at") or payload.get("updated_at") or "")
    updated = str(payload.get("updated_at") or created)
    task: dict[str, Any] = {
        "resultType": result_type,
        "taskId": f"agent:{agent_id}",
        "status": _task_status(payload),
        "createdAt": created,
        "lastUpdatedAt": updated,
        "ttlMs": 604_800_000,
        "pollIntervalMs": 1000,
        "_meta": _modern_meta(),
    }
    if task["status"] == "completed":
        task["result"] = {"resultType": "complete", "content": [{"type": "text",
            "text": json.dumps(payload, ensure_ascii=False)}], "isError": False}
    elif task["status"] == "failed":
        task["error"] = {"code": -32010, "message": payload.get("message") or
                         payload.get("stop_reason") or "agent failed", "data": payload}
    return task


def _task_error(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    summary = str(payload.get("summary") or payload.get("message") or "daemon request failed")
    code = -32602 if int(payload.get("http_status") or 500) in (400, 404) else -32603
    return rpc_error(request_id, code, summary)


def _accepted_input_text(input_responses: Any) -> str:
    if not isinstance(input_responses, dict):
        return ""
    for response in input_responses.values():
        if not isinstance(response, dict) or response.get("action") != "accept":
            continue
        content = response.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, dict):
            for key in ("message", "input", "text", "value"):
                value = content.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            if content:
                return json.dumps(content, ensure_ascii=False)
    return ""

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
    token = _read_token()
    try:
        with urllib.request.urlopen(base + "/health", timeout=1) as resp:
            if resp.status != 200:
                return False
            body = json.loads(resp.read().decode("utf-8"))
            if body.get("service") != "agent-mcp-daemon":
                return False
            fingerprint = body.get("token_sha256")
            expected = hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""
            return bool(expected and fingerprint == expected)
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


def _post_once(base: str, token: str, path: str, payload: dict[str, Any],
               http_timeout: float | None = None) -> dict[str, Any] | None:
    """单次 HTTP POST；连接失败返回 None（触发重拉），HTTP 错误转结构化错误。
    http_timeout 缺省用 _HTTP_TIMEOUT；wait_agent 会按请求的 timeout 叠加余量。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=body, method="POST",
        headers={"X-Auth-Token": token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=http_timeout or _HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        hint = detail
        try:
            hint = json.loads(detail).get("error") or detail
        except json.JSONDecodeError:
            pass
        return {"status": "error", "summary": f"daemon returned HTTP {exc.code}: {hint}",
                "root_cause_hint": detail or None,
                "next_actions": ["check the arguments and the daemon log"]}
    except (urllib.error.URLError, OSError, TimeoutError):
        return None


def _daemon_post(path: str, payload: dict[str, Any],
                 http_timeout: float | None = None) -> dict[str, Any]:
    """调用 daemon；连接失败先失效缓存重新拉起，再重试一次。"""
    global _DAEMON
    if _DAEMON is None:
        _DAEMON = ensure_daemon()
    base, token = _DAEMON
    out = _post_once(base, token, path, payload, http_timeout=http_timeout)
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
    out = _post_once(base, token, path, payload, http_timeout=http_timeout)
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
    # wait_agent 阻塞时长可自定义（上限 MAX_WAIT_SECONDS）：HTTP 层超时同步叠加余量，
    # 避免 daemon 仍在等待时 MCP→daemon 请求先被 _HTTP_TIMEOUT 掐断。
    http_timeout: float | None = None
    if name == "wait_agent":
        wait = min(max(float(payload.get("timeout") or 30), 1), MAX_WAIT_SECONDS)
        http_timeout = _HTTP_TIMEOUT + wait
    return _daemon_post(path, payload, http_timeout=http_timeout)


def handle(request: dict[str, Any], *, emit=send) -> None:
    request_id = request.get("id")
    method = request.get("method")
    requested_version = _request_protocol(request)
    modern = requested_version is not None
    if modern and requested_version not in SUPPORTED_PROTOCOL_VERSIONS:
        emit(_unsupported_version(request_id, requested_version))
        return
    if method == "server/discover":
        emit({"jsonrpc": "2.0", "id": request_id, "result": _modern_discover()})
    elif method == "initialize":
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
        listed: dict[str, Any] = {"tools": TOOLS}
        if modern:
            listed.update({"resultType": "complete", "ttlMs": 300_000,
                           "cacheScope": "public", "_meta": _modern_meta()})
        emit({"jsonrpc": "2.0", "id": request_id, "result": listed})
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
        if modern and name == "spawn_agent" and _tasks_supported(request) \
                and payload.get("agent_id") is not None:
            emit({"jsonrpc": "2.0", "id": request_id, "result": _task_result(payload)})
        else:
            emit(result(request_id, payload, is_error=payload.get("status") == "error",
                        modern=modern))
    elif method in ("tasks/get", "tasks/update", "tasks/cancel"):
        if not modern or not _tasks_supported(request):
            emit(rpc_error(request_id, -32023, "Missing required client capability: "
                           "io.modelcontextprotocol/tasks"))
            return
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        try:
            agent_id = _agent_id_from_task(params)
        except (TypeError, ValueError):
            emit(rpc_error(request_id, -32602, "Invalid taskId"))
            return
        if method == "tasks/get":
            payload = call_tool("wait_agent", {"agent_id": agent_id, "timeout": 1})
            if payload.get("status") == "error":
                emit(_task_error(request_id, payload))
                return
            emit({"jsonrpc": "2.0", "id": request_id,
                  "result": _task_result(payload, result_type="complete")})
            return
        if method == "tasks/update":
            message = _accepted_input_text(params.get("inputResponses"))
            if not message:
                emit(rpc_error(request_id, -32602,
                               "accepted input response requires non-empty content"))
                return
            payload = call_tool("steer_agent", {"agent_id": agent_id, "message": message})
        else:
            payload = call_tool("interrupt_agent", {"agent_id": agent_id})
        if payload.get("status") == "error":
            emit(_task_error(request_id, payload))
            return
        emit({"jsonrpc": "2.0", "id": request_id, "result": {
            "resultType": "complete", "_meta": _modern_meta()}})
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
