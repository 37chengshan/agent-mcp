from __future__ import annotations
import hashlib
import hmac
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
MAX_SSE_CLIENTS = 128
MAX_JSON_BYTES = 1_000_000
HEARTBEAT_SECONDS = 15.0
SNAPSHOT_EVENTS_PER_AGENT = 60
_API_METHODS = {
    "/api/agents/spawn": "spawn",
    "/api/agents/send_message": "send_message",
    "/api/agents/steer": "steer",
    "/api/agents/followup": "followup",
    "/api/agents/wait": "wait",
    "/api/agents/interrupt": "interrupt",
    "/api/agents/list": "list_agents",
    "/api/agents/activity": "activity",
    "/api/usage": "usage",
    "/api/memory/store": "memory_store",
    "/api/memory/recall": "memory_recall",
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
            client = {"id": self._next, "buffer": [], "closed": False,
                      "replayed": set()}  # 回放阶段已下发的 seq，live 阶段据此去重
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
        supplied = self.headers.get("X-Auth-Token") or ""
        if hmac.compare_digest(supplied, self.server.token):
            return True
        self.send_error(401, "unauthorized")
        return False

    def do_GET(self):
        if not self._check_host():
            return
        path = self.path.split("?")[0]
        if path == "/health":
            self._send_json(200, {
                "ok": True,
                "version": 1,
                "service": "agent-mcp-daemon",
                "token_sha256": hashlib.sha256(self.server.token.encode("utf-8")).hexdigest(),
            })
        elif path == "/api/config":
            self._send_json(200, {"max_message_chars": 20_000,
                                  "write_auth": "url-fragment"})
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
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json(400, {"error": "invalid Content-Length"})
            return
        if length > MAX_JSON_BYTES:
            # 排空请求体后再回 413：避免客户端写入尚未完成时连接被断（BrokenPipe）
            self.rfile.read(min(length, MAX_JSON_BYTES * 2))
            self._send_json(413, {"error": f"request body exceeds {MAX_JSON_BYTES} bytes"})
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
        # 每个 agent 取最近 N 条事件（而非全局前 500 条）：避免后 spawn 的 agent
        # 事件被整体截断，导致详情面板（当前工具/消息流/最近事件）空白。
        events = db.events_by_agents([a["id"] for a in agents],
                                     per_agent_limit=SNAPSHOT_EVENTS_PER_AGENT)
        # D1: gantt 字段——created_at 原样返；finished_at 缺失（running 态）用 updated_at 兜底 null。
        # D2: anomalies 预计算（daemon 侧聚合，免前端扫全事件流）。
        keep = ("id", "parent_id", "task_name", "cli", "model",
                "status", "stop_reason", "updated_at", "created_at",
                "finished_at", "session_id")
        totals = {"input_tokens": 0, "output_tokens": 0, "cache_creation": 0,
                  "cache_read": 0, "cost_usd": 0.0}
        per_agent = []
        agent_out = []
        for a in agents:
            u = db.usage_total(a["id"])
            per_agent.append({"agent_id": a["id"], **u})
            for k in totals:
                totals[k] = totals.get(k, 0) + u.get(k, 0)
            row = {k: a.get(k) for k in keep}
            # D1 兜底：running 态无 finished_at，用 updated_at null 化（前端 Gantt pulsing 判 running）
            if row.get("finished_at") is None and row.get("status") == "running":
                row["finished_at"] = None
            # D2 预计算异常 badge（免前端再扫全事件流）
            row["anomalies"] = db.agent_anomalies(a["id"])
            agent_out.append(row)
        self._send_json(200, {
            "agents": agent_out,
            "events": events,
            "usage": {"totals": totals, "per_agent": per_agent},
            "last_seq": events[-1]["seq"] if events else 0,
        })

    def _stream_events(self):
        """SSE 直播流。last_seq 查询参数 / Last-Event-ID 头 → 先回放 SQLite 事件，再进入 live。

        顺序：先 connect 再回放——connect 之后 publish 的事件都进本客户端缓冲；
        回放以连接时刻的 max_seq 为固定上界分页补发 (last_seq, boundary]，
        已回放的 seq 记入 replayed，live 阶段按该集合去重，保证不重、不丢、顺序严格，
        且回放不会无限追逐回放期间新写入的事件（boundary 之外的事件走 live 缓冲）。
        """
        query = (urllib.parse.parse_qs(self.path.split("?", 1)[1])
                 if "?" in self.path else {})
        try:
            last_seq = int((query.get("last_seq") or ["0"])[0])
        except ValueError:
            last_seq = 0
        try:
            last_seq = max(last_seq, int(self.headers.get("Last-Event-ID") or "0"))
        except ValueError:
            pass
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
            if last_seq > 0 and self.server.db is not None:
                # 固定重连上界：回放只补 (last_seq, boundary]，boundary 之后的新写入
                # 已在 live 缓冲（connect 先于 boundary 捕获），避免分页回放无限追逐持续插入。
                # 每页至多 1000 条，多页直至上界，保证尾部不静默丢失。
                boundary = self.server.db.max_seq()
                cursor = last_seq
                while cursor < boundary:
                    page = self.server.db.events_since(cursor, limit=1000)
                    if not page:
                        break
                    reached_boundary = False
                    for ev in page:
                        seq = ev.get("seq")
                        if seq is None:
                            continue
                        if seq > boundary:
                            reached_boundary = True  # 边界后的新写入，交由 live 缓冲
                            break
                        event_payload = {
                            "type": ev["type"],
                            "agent_id": ev["agent_id"],
                            "payload": ev["payload"],
                            "seq": seq,
                        }
                        payload = (
                            f"id: {seq}\nevent: {ev['type']}\ndata: "
                            f"{json.dumps(event_payload, ensure_ascii=False)}\n\n"
                        )
                        try:
                            self.wfile.write(payload.encode("utf-8"))
                            self.wfile.flush()
                        except (BrokenPipeError, OSError):
                            return  # 连接已断，finally 中 close
                        client["replayed"].add(seq)
                        cursor = seq
                    if reached_boundary or len(page) < 1000:
                        break
            while not client["closed"]:
                chunk = self.server.broadcaster.drain(client)
                if chunk:
                    if client["replayed"]:
                        chunk = self._strip_replayed(chunk, client["replayed"])
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

    @staticmethod
    def _strip_replayed(chunk: str, replayed: set[int]) -> str:
        """去掉 live 缓冲中已由回放阶段下发的 SSE 事件（按 id: seq 匹配），避免重复。"""
        if not replayed:
            return chunk
        keep = []
        for part in chunk.split("\n\n"):
            if not part:
                continue
            first = part.split("\n", 1)[0]
            if first.startswith("id: "):
                try:
                    if int(first[4:]) in replayed:
                        continue
                except ValueError:
                    pass
            keep.append(part)
        return ("\n\n".join(keep) + "\n\n") if keep else ""

    def _security_headers(self) -> None:
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _send_json(self, code: int, payload: Any):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._security_headers()
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
        self._security_headers()
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
