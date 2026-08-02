from __future__ import annotations
import argparse
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from agent_mcp.daemon_http import DaemonHTTPServer, EventBroadcaster, HEARTBEAT_SECONDS
from agent_mcp.db import DB
from agent_mcp.dispatch import (SlotScheduler, is_pid_running, spawn_cli_worker,
                                terminate_process_tree)

DEFAULT_PORT = 8765
DEFAULT_STATE_DIR = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "agent-mcp"
DEFAULT_WEB_ROOT = Path(__file__).resolve().parent.parent / "web"

_TERMINAL = ("terminated", "error", "cancelled", "incomplete")


def _write_private(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)


def _load_or_create_token(state_dir: Path) -> str:
    """读取或生成 daemon token（0600 daemon.json；跨重启保留，MCP 端无需重读）。"""
    path = state_dir / "daemon.json"
    if path.is_file():
        try:
            token = json.loads(path.read_text(encoding="utf-8")).get("token")
            if token:
                return token
        except Exception:
            pass
    token = uuid.uuid4().hex
    _write_private(path, {"token": token})
    return token


def _read_json(path: Path | str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _tail(path: Path | str, limit: int = 800) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def _merge_pending(prompt: str, pending: list[dict]) -> str:
    """followup：把挂起的 user 消息合并进新 prompt（daemon 消息队列语义）。"""
    lines = [f"<user message {i + 1}>: {m['content']}"
             for i, m in enumerate(pending) if m.get("content")]
    return prompt + ("\n\n" + "\n".join(lines) if lines else "")


class Dispatcher:
    """CLI 任务派发执行器：spawn/send_message/followup/wait/interrupt/
    list_agents/activity/usage 八操作 + 完成检测监控线程。

    spawn/followup 复用 dispatch.spawn_cli_worker（不重写 spawn 逻辑）；
    SlotScheduler 按 agent_id FIFO 限流（同 id 并发 followup 自动串联）；
    interrupt 用 terminate_process_tree；worker 完成由 state 文件轮询检测。
    """
    def __init__(self, *, db: Any, broadcaster: EventBroadcaster, state_dir: Path | str,
                 max_concurrent: int = 4, spawn_fn: Any = None,
                 monitor_interval: float = 1.0):
        self.db = db
        self.broadcaster = broadcaster
        self.state_dir = Path(state_dir)
        self._scheduler = SlotScheduler(max_concurrent=max_concurrent)
        self._spawn_fn = spawn_fn or spawn_cli_worker
        self._monitor_interval = monitor_interval
        self._workers: dict[int, dict[str, Any]] = {}   # agent_id -> spawn info
        self._pending: dict[int, tuple[str, str, str, dict]] = {}  # 排队中的 spawn 参数
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._monitor, daemon=True,
                                            name="dispatcher-monitor")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _monitor(self) -> None:
        while not self._stop.wait(self._monitor_interval):
            with self._lock:
                ids = list(self._workers)
            for agent_id in ids:
                try:
                    self._check_worker(agent_id)
                except Exception as exc:
                    print(f"[dispatcher] monitor error for agent {agent_id}: {exc}",
                          file=sys.stderr)

    # ---- 八操作 ----

    def spawn(self, body: dict) -> dict:
        target_cli = body.get("target_cli")
        prompt = body.get("prompt")
        cwd = body.get("cwd")
        if not target_cli or not prompt or not cwd:
            raise ValueError("target_cli, prompt and cwd are required")
        if body.get("context"):
            prompt = f"{body['context']}\n\n{prompt}"
        agent_id = self.db.insert_agent(
            parent_id=body.get("parent_agent_id"),
            session_id=body.get("session_id") or "default",
            task_name=body.get("task_name") or "",
            cli=target_cli, model=body.get("model"), cwd=cwd,
            permission_mode=body.get("permission_mode") or "plan",
            command_summary=None)
        self._broadcast("agent.spawned", {"agent_id": agent_id}, agent_id)
        # 先写 pending 再 acquire：避免补位时 pending 未就绪导致任务永久滞留
        params = (target_cli, prompt, cwd, body)
        with self._lock:
            self._pending[agent_id] = params
        if self._scheduler.acquire(str(agent_id)):
            with self._lock:
                self._pending.pop(agent_id, None)
            return self._run_worker(agent_id, *params)
        return {"agent_id": agent_id, "status": "queued", "pid": None}

    def send_message(self, body: dict) -> dict:
        agent_id = self._require_id(body)
        message = body.get("message")
        if not message:
            raise ValueError("message is required")
        agent = self.db.get_agent(agent_id)
        if agent is None:
            raise ValueError(f"agent {agent_id} not found")
        self.db.insert_message(agent_id=agent_id, role="user", content=message)
        status = "undelivered" if agent["status"] in _TERMINAL else "delivered"
        return {"agent_id": agent_id, "status": status}

    def followup(self, body: dict) -> dict:
        """唯一触发新 turn 的入口：合并挂起消息进 prompt 后重新 spawn。
        运行中的 agent 走槽位队列，当前 run 结束后自动串联。"""
        agent_id = self._require_id(body)
        prompt = body.get("prompt")
        if not prompt:
            raise ValueError("prompt is required")
        agent = self.db.get_agent(agent_id)
        if agent is None:
            raise ValueError(f"agent {agent_id} not found")
        if body.get("interrupt") and agent["status"] == "running":
            self.interrupt({"agent_id": agent_id})
            agent = self.db.get_agent(agent_id)
        pending_msgs = self.db.messages_for(agent_id)
        merged = _merge_pending(prompt, pending_msgs)
        params = (agent["cli"], merged, agent["cwd"], body)
        with self._lock:
            self._pending[agent_id] = params
        if self._scheduler.acquire(str(agent_id)):
            with self._lock:
                self._pending.pop(agent_id, None)
            res = self._run_worker(agent_id, *params)
            res["merged_messages"] = len(pending_msgs)
            return res
        return {"agent_id": agent_id, "status": "queued", "pid": None,
                "merged_messages": len(pending_msgs)}

    def wait(self, body: dict) -> dict:
        """短阻塞轮询（≤30s）；完成后返回摘要 + 结构化结果，超时给轮询指引。"""
        agent_id = self._require_id(body)
        timeout = min(max(float(body.get("timeout") or 30), 0.1), 30.0)
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                info = self._workers.get(agent_id)
            if info and _read_json(info["state_path"]).get("status") == "finished":
                summary = _tail(info["out_path"])
                self._check_worker(agent_id)
                agent = self.db.get_agent(agent_id)
                return self._wait_result(agent, summary)
            agent = self.db.get_agent(agent_id)
            if agent is None:
                raise ValueError(f"agent {agent_id} not found")
            if agent["status"] in _TERMINAL:
                return self._wait_result(agent, "")
            if time.monotonic() >= deadline:
                return {"agent_id": agent_id, "status": agent["status"],
                        "hint": "still running; poll list_agents/activity or "
                                "wait again (timeout <= 30s)"}
            time.sleep(0.2)

    def interrupt(self, body: dict) -> dict:
        agent_id = self._require_id(body)
        agent = self.db.get_agent(agent_id)
        if agent is None:
            raise ValueError(f"agent {agent_id} not found")
        with self._lock:
            info = self._workers.pop(agent_id, None)
            self._pending.pop(agent_id, None)
        if info and info.get("worker_pid"):
            terminate_process_tree(info["worker_pid"])
            self._release_and_promote(str(agent_id))
        else:
            self._scheduler.remove(str(agent_id))
        self.db.set_status(agent_id, "cancelled", stop_reason="interrupted")
        self._broadcast("agent.cancelled", {"agent_id": agent_id,
                                            "stop_reason": "interrupted"}, agent_id)
        return {"agent_id": agent_id, "status": "cancelled",
                "stop_reason": "interrupted", "usage_incomplete": True}

    def list_agents(self, body: dict) -> dict:
        agents = self.db.agents_by_session(body.get("session_id"))
        for a in agents:
            msgs = self.db.messages_for(a["id"], size=1)
            a["last_message"] = msgs[-1]["content"] if msgs else ""
        return {"agents": agents}

    def activity(self, body: dict) -> dict:
        since_seq = int(body.get("since_seq") or 0)
        events = self.db.events_since(since_seq)
        agent_id = body.get("agent_id")
        if agent_id is not None:
            events = [e for e in events if e.get("agent_id") == int(agent_id)]
        return {"events": events,
                "next_seq": events[-1]["seq"] if events else since_seq}

    def usage(self, body: dict) -> dict:
        if body.get("agent_id") is not None:
            totals = self.db.usage_total(int(body["agent_id"]))
        else:
            totals: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0,
                                      "cache_creation": 0, "cache_read": 0,
                                      "cost_usd": 0.0}
            for a in self.db.agents_by_session(body.get("session_id")):
                for k, v in self.db.usage_total(a["id"]).items():
                    totals[k] = totals.get(k, 0) + v
        totals["estimated"] = True
        return totals

    # ---- 内部 ----

    def _run_worker(self, agent_id: int, target_cli: str, prompt: str,
                    cwd: str, body: dict) -> dict:
        try:
            info = self._spawn_fn(
                target_cli, prompt=prompt, cwd=cwd,
                permission_mode=body.get("permission_mode") or "plan",
                model=body.get("model"), max_turns=body.get("max_turns", 8),
                resume=body.get("resume"), state_dir=self.state_dir)
        except ValueError as exc:
            self._fail(agent_id, stop_reason="cli_missing", message=str(exc))
            return {"agent_id": agent_id, "status": "error", "error": str(exc)}
        with self._lock:
            self._workers[agent_id] = info
        self.db.set_status(agent_id, "running", pid=info["worker_pid"])
        self._broadcast("agent.running", {"agent_id": agent_id,
                                          "pid": info["worker_pid"]}, agent_id)
        return {"agent_id": agent_id, "status": "running", "pid": info["worker_pid"]}

    def _fail(self, agent_id: int, *, stop_reason: str, message: str) -> None:
        self._release_and_promote(str(agent_id))
        self.db.set_status(agent_id, "error", stop_reason=stop_reason)
        self._broadcast("agent.error", {"agent_id": agent_id,
                                        "stop_reason": stop_reason,
                                        "message": message}, agent_id)

    def _check_worker(self, agent_id: int) -> None:
        """state 文件 finished → 状态迁移 + 广播 + 槽位释放补位。幂等（pop 保护）。"""
        with self._lock:
            info = self._workers.get(agent_id)
            if info is None:
                return
            state = _read_json(info["state_path"])
            if state.get("status") != "finished":
                return
            self._workers.pop(agent_id, None)
            rc = state.get("process_status", 0)
            summary = _tail(info["out_path"])
        if rc == 0:
            self.db.set_status(agent_id, "terminated", stop_reason="end_turn")
            self._broadcast("agent.terminated", {"agent_id": agent_id,
                                                 "stop_reason": "end_turn",
                                                 "summary": summary}, agent_id)
        else:
            self.db.set_status(agent_id, "error", stop_reason="cli_exit_nonzero")
            self._broadcast("agent.error", {"agent_id": agent_id,
                                            "stop_reason": "cli_exit_nonzero",
                                            "message": f"cli exited {rc}"}, agent_id)
        self._release_and_promote(str(agent_id))
        self._maybe_chain(agent_id)

    def _maybe_chain(self, agent_id: int) -> None:
        """该 agent 有排队中的 followup → 重新占槽运行（同 id run 串联）。

        SlotScheduler 对"已在 active"的 key 不入队，followup 由 Dispatcher
        自己记 pending，完成时在此补占槽；若槽位已被其他排队任务取走，
        acquire 会正常入队，由后续补位触发。
        """
        with self._lock:
            has_pending = agent_id in self._pending
        if not has_pending:
            return
        if self._scheduler.acquire(str(agent_id)):
            with self._lock:
                params = self._pending.pop(agent_id, None)
            if params is not None:
                self._run_worker(agent_id, *params)
            else:
                self._release_and_promote(str(agent_id))  # 竞态：无参可取，释放占位

    def _release_and_promote(self, key: str) -> None:
        promoted = self._scheduler.release(key)
        if promoted:
            self._start_queued(promoted)

    def _start_queued(self, key: str) -> None:
        with self._lock:
            params = self._pending.pop(int(key), None)
        if params is None:
            self._release_and_promote(key)  # 被中断的排队任务：释放占位
            return
        self._run_worker(int(key), *params)

    def _wait_result(self, agent: dict, summary: str) -> dict:
        if agent["status"] == "terminated":
            # summary 兜底：与 monitor 竞争时从已落库的 terminated 事件取
            if not summary:
                summary = self._last_event_payload(agent["id"], "agent.terminated").get("summary", "")
            return {"agent_id": agent["id"], "status": "terminated",
                    "stop_reason": agent["stop_reason"], "summary": summary}
        if agent["status"] == "error":
            return {"agent_id": agent["id"], "status": "error",
                    "stop_reason": agent["stop_reason"],
                    "message": summary.strip() or agent.get("stop_reason", "")}
        return {"agent_id": agent["id"], "status": agent["status"],
                "stop_reason": agent["stop_reason"]}

    def _last_event_payload(self, agent_id: int, type_: str) -> dict:
        for e in reversed(self.db.events_since(0)):
            if e.get("agent_id") == agent_id and e.get("type") == type_:
                return e.get("payload") or {}
        return {}

    def _broadcast(self, type_: str, payload: dict, agent_id: int) -> None:
        agent = self.db.get_agent(agent_id)
        session_id = agent["session_id"] if agent else "default"
        seq = self.db.insert_event(agent_id=agent_id, type=type_, payload=payload,
                                   session_id=session_id)
        if seq is not None:
            self.broadcaster.publish({"type": type_, "agent_id": agent_id,
                                      "payload": payload, "seq": seq}, seq=seq)

    @staticmethod
    def _require_id(body: dict) -> int:
        agent_id = body.get("agent_id")
        if not agent_id:
            raise ValueError("agent_id is required")
        return int(agent_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent MCP daemon")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--web-root", type=Path, default=DEFAULT_WEB_ROOT)
    args = parser.parse_args()

    state_dir = args.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)

    lock_path = state_dir / "daemon.lock"
    if lock_path.is_file():
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            if is_pid_running(lock.get("pid")):
                print(f"daemon already running (pid {lock['pid']})", file=sys.stderr)
                return 0
        except Exception:
            pass  # 残留/损坏锁，覆盖

    token = _load_or_create_token(state_dir)
    db = DB(state_dir / "daemon.db")
    broadcaster = EventBroadcaster()
    dispatcher = Dispatcher(db=db, broadcaster=broadcaster, state_dir=state_dir)
    srv = DaemonHTTPServer(("127.0.0.1", args.port), args.web_root, token=token,
                           db=db, dispatcher=dispatcher, broadcaster=broadcaster)

    _write_private(lock_path, {"pid": os.getpid(), "ts": time.time()})
    dispatcher.start()

    def _heartbeat() -> None:
        while True:
            time.sleep(HEARTBEAT_SECONDS)
            broadcaster.heartbeat_all()

    threading.Thread(target=_heartbeat, daemon=True).start()

    try:
        print(f"agent-mcp daemon on http://127.0.0.1:{srv.server_address[1]}", file=sys.stderr)
        srv.serve_forever()
    finally:
        dispatcher.stop()
        srv.server_close()
        lock_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
