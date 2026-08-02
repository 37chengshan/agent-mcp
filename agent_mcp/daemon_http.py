from __future__ import annotations
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
MAX_SSE_CLIENTS = 32
HEARTBEAT_SECONDS = 15.0
_API_METHODS = {
    "/api/agents/spawn": "spawn",
    "/api/agents/send_message": "send_message",
    "/api/agents/followup": "followup",
    "/api/agents/wait": "wait",
    "/api/agents/interrupt": "interrupt",
    "/api/agents/list": "list_agents",
    "/api/agents/activity": "activity",
    "/api/usage": "usage",
}


class EventBroadcaster:
    """SSE 统一广播：事件循环单写，非阻塞写，写失败断开，统一心跳。"""
    def __init__(self, max_clients: int = MAX_SSE_CLIENTS):
        self.max = max_clients
        self._clients: dict[int, dict[str, Any]] = {}
        self._next = 0
        self._lock = threading.Lock()

    def connect(self) -> dict[str, Any] | None:
        with self._lock:
            if len(self._clients) >= self.max:
                return None
            self._next += 1
            client = {"id": self._next, "buffer": [], "closed": False}
            self._clients[self._next] = client
            return client

    def close(self, client: dict[str, Any]) -> None:
        with self._lock:
            client["closed"] = True
            self._clients.pop(client["id"], None)

    def publish(self, event: dict[str, Any], *, seq: int | None) -> None:
        # seq=None 的事件（agent.message_delta）不落库，SSE 不带 id，
        # 断线回放只对齐落库 seq，不会与其冲突
        id_line = f"id: {seq}\n" if seq is not None else ""
        payload = (f"{id_line}event: {event['type']}\n"
                   f"data: {json.dumps(event, ensure_ascii=False)}\n\n")
        with self._lock:
            clients = list(self._clients.values())
        for client in clients:
            if not client["closed"]:
                client["buffer"].append(payload)

    def drain(self, client: dict[str, Any]) -> str | None:
        """取出并清空缓冲（与 publish/heartbeat 同锁，避免 join 期间丢事件）。"""
        with self._lock:
            if not client["buffer"]:
                return None
            chunk = "".join(client["buffer"])
            del client["buffer"][:]
            return chunk

    def heartbeat_all(self) -> None:
        with self._lock:
            clients = list(self._clients.values())
        for client in clients:
            if not client["closed"]:
                client["buffer"].append(": ping\n\n")


class DaemonHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, web_root: Path, *, token: str, db: Any,
                 dispatcher: Any, broadcaster: EventBroadcaster | None = None):
        self.web_root = Path(web_root)
        self.token = token
        self.db = db
        self.dispatcher = dispatcher
        self.broadcaster = broadcaster or EventBroadcaster()
        super().__init__(addr, Handler)
        self.server_name = "agent-mcp-daemon"


class Handler(BaseHTTPRequestHandler):
    server: DaemonHTTPServer  # type: ignore[assignment]

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    def _check_host(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        if host in ALLOWED_HOSTS:
            return True
        self.send_error(400, "bad host")
        return False

    def _check_token(self) -> bool:
        if self.headers.get("X-Auth-Token") == self.server.token:
            return True
        self.send_error(401, "unauthorized")
        return False

    def do_GET(self):
        if not self._check_host():
            return
        path = self.path.split("?")[0]
        if path == "/health":
            self._send_json(200, {"ok": True, "version": 1})
        elif path == "/api/snapshot":
            self._send_snapshot()
        elif path == "/events":
            self._stream_events()
        elif path == "/" or path == "/index.html":
            self._send_file("index.html")
        elif path.startswith("/static/"):
            self._send_file(path[len("/static/"):])
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._check_host():
            return
        if not self._check_token():
            return
        path = self.path.split("?")[0]
        method = _API_METHODS.get(path)
        if method is None:
            self.send_error(404)
            return
        if self.server.dispatcher is None:
            self._send_json(503, {"error": "dispatcher not ready"})
            return
        body = self._read_json()
        try:
            result = getattr(self.server.dispatcher, method)(body)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})
            return
        self._send_json(200, result)

    def _send_snapshot(self):
        """只读历史快照（网页刷新重建导图用）：无 token，Host 校验照旧。"""
        query = (urllib.parse.parse_qs(self.path.split("?", 1)[1])
                 if "?" in self.path else {})
        session_id = query.get("session_id", [None])[0]
        db = self.server.db
        if db is None:
            self._send_json(503, {"error": "db not ready"})
            return
        agents = db.agents_by_session(session_id)
        if session_id is not None and not agents:
            self._send_json(400, {"error": f"session {session_id} not found"})
            return
        events = db.events_since(0, session_id=session_id, limit=500)
        keep = ("id", "parent_id", "task_name", "cli", "model",
                "status", "stop_reason", "updated_at")
        totals = {"input_tokens": 0, "output_tokens": 0, "cache_creation": 0,
                  "cache_read": 0, "cost_usd": 0.0}
        per_agent = []
        for a in agents:
            u = db.usage_total(a["id"])
            per_agent.append({"agent_id": a["id"], **u})
            for k in totals:
                totals[k] = totals.get(k, 0) + u.get(k, 0)
        self._send_json(200, {
            "agents": [{k: a[k] for k in keep} for a in agents],
            "events": events,
            "usage": {"totals": totals, "per_agent": per_agent},
            "last_seq": events[-1]["seq"] if events else 0,
        })

    def _stream_events(self):
        client = self.server.broadcaster.connect()
        if client is None:
            self.send_error(503, "too many SSE clients")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while not client["closed"]:
                chunk = self.server.broadcaster.drain(client)
                if chunk:
                    try:
                        self.wfile.write(chunk.encode("utf-8"))
                        self.wfile.flush()
                    except (BrokenPipeError, OSError):
                        break
                else:
                    time.sleep(0.1)
        finally:
            self.server.broadcaster.close(client)

    def _send_json(self, code: int, payload: Any):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, name: str):
        root = self.server.web_root.resolve()
        path = (root / name).resolve()
        if not path.is_file() or root not in path.parents:
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        ctype = "text/html; charset=utf-8" if name.endswith(".html") else "application/octet-stream"
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}
