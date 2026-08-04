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

# 脚本直接启动（python agent_mcp/daemon_main.py 或 spawn_detached 拉起）时，
# sys.path[0] 是脚本目录而非项目根，需手动补项目根才能 import agent_mcp 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_mcp.cli_adapters import ResumeUnsupportedError, get_adapter
from agent_mcp.daemon_http import DaemonHTTPServer, EventBroadcaster, HEARTBEAT_SECONDS
from agent_mcp.db import DB
from agent_mcp.dispatch import (SlotScheduler, is_pid_running, spawn_cli_worker,
                                terminate_process_tree)
from agent_mcp.state_machine import transition

DEFAULT_PORT = 8765
MAX_PROMPT_CHARS = 200_000
MAX_CONTEXT_CHARS = 200_000
MAX_MESSAGE_CHARS = 20_000
# wait_agent 单次阻塞上限：默认 600s（10 分钟），可用环境变量 AGENT_MCP_MAX_WAIT 调整
MAX_WAIT_SECONDS = float(os.environ.get("AGENT_MCP_MAX_WAIT", "600"))


def default_state_dir() -> Path:
    """AGENT_MCP_HOME 优先；兼容 CODEX_HOME；缺省 ~/.codex。"""
    base = (os.environ.get("AGENT_MCP_HOME")
            or os.environ.get("CODEX_HOME")
            or Path.home() / ".codex")
    return Path(base) / "agent-mcp"


DEFAULT_STATE_DIR = default_state_dir()
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


def _coerce_timeout_seconds(value: Any) -> float | None:
    """daemon 边界校验 timeout_seconds：空/None 表示禁用（返回 None），
    其余必须为数值且 >0，否则同步 ValueError（不启动 worker）。"""
    if value is None or value == "":
        return None
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        raise ValueError("timeout_seconds must be a positive number")
    if timeout <= 0:
        raise ValueError("timeout_seconds must be a positive number")
    return timeout


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
        timeout_seconds = _coerce_timeout_seconds(body.get("timeout_seconds"))
        body = {**body, "timeout_seconds": timeout_seconds}
        context = body.get("context") or ""
        if len(context) > MAX_CONTEXT_CHARS:
            raise ValueError(f"context exceeds {MAX_CONTEXT_CHARS} chars")
        if context:
            prompt = f"{context}\n\n{prompt}"
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError(f"prompt exceeds {MAX_PROMPT_CHARS} chars")
        agent_id = self.db.insert_agent(
            parent_id=body.get("parent_agent_id"),
            session_id=body.get("session_id") or "default",
            task_name=body.get("task_name") or "",
            cli=target_cli, model=body.get("model"), cwd=cwd,
            permission_mode=body.get("permission_mode") or "plan",
            command_summary=None)
        self._broadcast("agent.spawned", {"agent_id": agent_id}, agent_id)
        self._broadcast("agent.user_turn", {"text": prompt, "kind": "spawn"}, agent_id)
        # 先写 pending 再 acquire：避免补位时 pending 未就绪导致任务永久滞留
        params = (target_cli, prompt, cwd, body)
        with self._lock:
            self._pending[agent_id] = params
        if self._scheduler.acquire(str(agent_id)):
            with self._lock:
                self._pending.pop(agent_id, None)
            return self._run_worker(agent_id, *params)
        return self._agent_result(agent_id, status="queued", pid=None)

    def send_message(self, body: dict) -> dict:
        agent_id = self._require_id(body)
        message = body.get("message")
        if not message:
            raise ValueError("message is required")
        if len(message) > MAX_MESSAGE_CHARS:
            raise ValueError(f"message exceeds {MAX_MESSAGE_CHARS} chars")
        agent = self.db.get_agent(agent_id)
        if agent is None:
            raise ValueError(f"agent {agent_id} not found")
        self._require_session(body, agent)
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
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError(f"prompt exceeds {MAX_PROMPT_CHARS} chars")
        timeout_seconds = _coerce_timeout_seconds(body.get("timeout_seconds"))
        body = {**body, "timeout_seconds": timeout_seconds}
        agent = self.db.get_agent(agent_id)
        if agent is None:
            raise ValueError(f"agent {agent_id} not found")
        self._require_session(body, agent)
        if body.get("interrupt") and agent["status"] == "running":
            self.interrupt({"agent_id": agent_id, "session_id": agent["session_id"]})
            agent = self.db.get_agent(agent_id)
        pending_msgs = self.db.messages_for(agent_id)
        merged = _merge_pending(prompt, pending_msgs)
        if len(merged) > MAX_PROMPT_CHARS:
            # 合并挂起消息后超限：在写 pending/启动前拒绝
            raise ValueError(f"prompt exceeds {MAX_PROMPT_CHARS} chars")
        self._broadcast("agent.user_turn", {"text": prompt,
                                             "kind": "steer" if body.get("interrupt") else "followup"},
                        agent_id)
        resume = body.get("resume") or agent.get("cli_session_id")
        body = {**body, "resume": resume}
        params = (agent["cli"], merged, agent["cwd"], body)
        with self._lock:
            self._pending[agent_id] = params
        if self._scheduler.acquire(str(agent_id)):
            with self._lock:
                self._pending.pop(agent_id, None)
            res = self._run_worker(agent_id, *params)
            res["merged_messages"] = len(pending_msgs)
            res["resumed_session_id"] = resume
            return res
        return self._agent_result(agent_id, status="queued", pid=None,
                                  merged_messages=len(pending_msgs),
                                  resumed_session_id=resume)

    def steer(self, body: dict) -> dict:
        """中断当前 run 并在同一节点立即开始下一 turn；支持的 CLI 自动 resume。"""
        agent_id = self._require_id(body)
        message = body.get("message")
        if not message:
            raise ValueError("message is required")
        if len(message) > MAX_MESSAGE_CHARS:
            raise ValueError(f"message exceeds {MAX_MESSAGE_CHARS} chars")
        agent = self.db.get_agent(agent_id)
        if agent is None:
            raise ValueError(f"agent {agent_id} not found")
        self._require_session(body, agent)
        interrupted = agent["status"] == "running"
        result = self.followup({"agent_id": agent_id, "prompt": message,
                                "interrupt": interrupted,
                                "session_id": agent["session_id"]})
        result["interrupted"] = interrupted
        return result

    def wait(self, body: dict) -> dict:
        """短阻塞轮询（默认 30s，上限 MAX_WAIT_SECONDS 可自定义）；完成后返回摘要 + 结构化结果，超时给轮询指引。"""
        agent_id = self._require_id(body)
        timeout = min(max(float(body.get("timeout") or 30), 0.1), MAX_WAIT_SECONDS)
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                info = self._workers.get(agent_id)
            if info and _read_json(info["state_path"]).get("status") == "finished":
                summary = _tail(info["out_path"])
                self._check_worker(agent_id)
                agent = self.db.get_agent(agent_id)
                if agent is None:
                    raise ValueError(f"agent {agent_id} not found")
                self._require_session(body, agent)
                return self._wait_result(agent, summary)
            agent = self.db.get_agent(agent_id)
            if agent is None:
                raise ValueError(f"agent {agent_id} not found")
            self._require_session(body, agent)
            if agent["status"] in _TERMINAL:
                return self._wait_result(agent, "")
            if time.monotonic() >= deadline:
                return self._agent_result(
                    agent_id, status=agent["status"],
                    hint="still running; poll list_agents/activity or "
                         f"wait again (timeout <= {MAX_WAIT_SECONDS:.0f}s)")
            time.sleep(0.2)

    def interrupt(self, body: dict) -> dict:
        agent_id = self._require_id(body)
        agent = self.db.get_agent(agent_id)
        if agent is None:
            raise ValueError(f"agent {agent_id} not found")
        self._require_session(body, agent)
        if agent["status"] in _TERMINAL:
            self._scheduler.remove(str(agent_id))
            return self._agent_result(agent_id, status=agent["status"],
                                      stop_reason=agent["stop_reason"],
                                      usage_incomplete=False)
        with self._lock:
            info = self._workers.pop(agent_id, None)
            self._pending.pop(agent_id, None)
        if info and info.get("worker_pid"):
            terminate_process_tree(info["worker_pid"])
            self._release_and_promote(str(agent_id))
        else:
            self._scheduler.remove(str(agent_id))
        self._set_status(agent_id, "cancelled", stop_reason="interrupted")
        self._broadcast("agent.cancelled", {"agent_id": agent_id,
                                            "stop_reason": "interrupted"}, agent_id)
        return self._agent_result(agent_id, status="cancelled",
                                  stop_reason="interrupted", usage_incomplete=True)

    def list_agents(self, body: dict) -> dict:
        agents = self.db.agents_by_session(body.get("session_id"))
        for a in agents:
            msgs = self.db.messages_for(a["id"], size=1)
            a["last_message"] = msgs[-1]["content"] if msgs else ""
        return {"agents": agents}

    def activity(self, body: dict) -> dict:
        since_seq = int(body.get("since_seq") or 0)
        agent_id = body.get("agent_id")
        session_id = body.get("session_id")
        if agent_id is not None:
            agent = self.db.get_agent(int(agent_id))
            if agent is None:
                raise ValueError(f"agent {agent_id} not found")
            self._require_session(body, agent)
            session_id = agent["session_id"]
        events = self.db.events_since(since_seq, session_id=session_id)
        if agent_id is not None:
            events = [e for e in events if e.get("agent_id") == int(agent_id)]
        return {"events": events,
                "next_seq": events[-1]["seq"] if events else since_seq}

    def usage(self, body: dict) -> dict:
        if body.get("agent_id") is not None:
            agent = self.db.get_agent(int(body["agent_id"]))
            if agent is None:
                raise ValueError(f"agent {body['agent_id']} not found")
            self._require_session(body, agent)
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
                resume=body.get("resume"), state_dir=self.state_dir,
                timeout_seconds=body.get("timeout_seconds"))
        except ResumeUnsupportedError as exc:
            self._fail(agent_id, stop_reason="resume_unsupported", message=str(exc))
            return self._agent_result(agent_id, status="error", error=str(exc))
        except ValueError as exc:
            self._fail(agent_id, stop_reason="cli_missing", message=str(exc))
            return self._agent_result(agent_id, status="error", error=str(exc))
        with self._lock:
            self._workers[agent_id] = info
        self._set_status(agent_id, "running", pid=info["worker_pid"])
        self._broadcast("agent.running", {"agent_id": agent_id,
                                          "pid": info["worker_pid"]}, agent_id)
        return self._agent_result(agent_id, status="running", pid=info["worker_pid"])

    def _set_status(self, agent_id: int, status: str, *, stop_reason: str | None = None,
                    pid: int | None = None, cli_session_id: str | None = None) -> None:
        """状态迁移统一入口：经 state_machine 校验合法迁移。

        终态→running 仅 followup 重启（新 run）豁免；终态→error 仅
        followup 重启失败（_fail）豁免，保证 cli_missing/resume_unsupported
        事件与 error 状态可观察；其余非法迁移抛 ValueError。
        """
        agent = self.db.get_agent(agent_id)
        current = agent["status"] if agent else None
        if current and status != current:
            if current in _TERMINAL and status in ("running", "error"):
                pass  # followup 重启：状态机按单 run 建模，重启/重启失败为显式例外
            else:
                transition(current, status)
        self.db.set_status(agent_id, status, stop_reason=stop_reason, pid=pid,
                           cli_session_id=cli_session_id)

    def _fail(self, agent_id: int, *, stop_reason: str, message: str) -> None:
        self._release_and_promote(str(agent_id))
        self._set_status(agent_id, "error", stop_reason=stop_reason)
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
            timed_out = bool(state.get("timed_out"))
            summary = _tail(info["out_path"])
        agent = self.db.get_agent(agent_id)
        if agent is not None:
            self._ingest_output(agent_id, agent["cli"], info["out_path"],
                                agent["session_id"])
        if timed_out:
            # 超时 → incomplete（可 resume/重派），事件沿用 agent.terminated + stop_reason
            self._set_status(agent_id, "incomplete", stop_reason="timeout")
            self._broadcast("agent.terminated", {"agent_id": agent_id,
                                                 "stop_reason": "timeout",
                                                 "summary": summary}, agent_id)
        elif rc == 0:
            self._set_status(agent_id, "terminated", stop_reason="end_turn")
            self._broadcast("agent.terminated", {"agent_id": agent_id,
                                                 "stop_reason": "end_turn",
                                                 "summary": summary}, agent_id)
        else:
            self._set_status(agent_id, "error", stop_reason="cli_exit_nonzero")
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
            return self._agent_result(agent["id"], status="terminated",
                                      stop_reason=agent["stop_reason"], summary=summary)
        if agent["status"] == "error":
            return self._agent_result(
                agent["id"], status="error", stop_reason=agent["stop_reason"],
                message=summary.strip() or agent.get("stop_reason", ""))
        return self._agent_result(agent["id"], status=agent["status"],
                                  stop_reason=agent["stop_reason"])

    def _agent_result(self, agent_id: int, **payload: Any) -> dict[str, Any]:
        agent = self.db.get_agent(agent_id)
        result = {"agent_id": agent_id, **payload}
        if agent:
            result.update({
                "session_id": agent["session_id"],
                "created_at": agent["created_at"],
                "updated_at": agent["updated_at"],
            })
        return result

    def _last_event_payload(self, agent_id: int, type_: str) -> dict:
        for e in reversed(self.db.events_since(0)):
            if e.get("agent_id") == agent_id and e.get("type") == type_:
                return e.get("payload") or {}
        return {}

    def _ingest_output(self, agent_id: int, cli: str, out_path: Path | str,
                       session_id: str) -> None:
        """worker 完成后一次性解析 stdout 流 → 事件落库 + 广播 + usage 累计。

        parse_stream 返回 (events, usage)：普通事件落库并广播；
        agent.message_delta 只广播不落库（前端打字机）；parse_stream 产出的
        agent.terminated 由 monitor 统一迁移广播，这里仅回填其 session_id
        到 cli_session_id（resume 用）。usage 为聚合 dict 无 model 拆分，
        统一按 model="aggregate" 落库（简单为准）。
        """
        try:
            lines = Path(out_path).read_text(encoding="utf-8",
                                             errors="replace").splitlines()
            if not lines:
                return
            adapter = get_adapter(cli)
            events, usage = adapter.parse_stream(lines)
        except Exception as exc:
            print(f"[dispatcher] ingest failed for agent {agent_id}: {exc}",
                  file=sys.stderr)
            return
        for ev in events:
            typ = ev.get("type")
            payload = ev.get("payload") or {}
            if typ == "agent.terminated":
                sid = payload.get("session_id")
                if sid:
                    agent = self.db.get_agent(agent_id)
                    if agent:
                        self.db.set_status(agent_id, agent["status"],
                                           cli_session_id=str(sid))
                continue
            seq = self.db.insert_event(agent_id=agent_id, type=typ,
                                       payload=payload, session_id=session_id)
            self.broadcaster.publish({"type": typ, "agent_id": agent_id,
                                      "payload": payload, "seq": seq}, seq=seq)
        if usage:
            self.db.upsert_usage(agent_id=agent_id, model="aggregate",
                                 input_tokens=usage.get("input_tokens", 0),
                                 output_tokens=usage.get("output_tokens", 0),
                                 cache_creation=usage.get("cache_creation", 0),
                                 cache_read=usage.get("cache_read", 0),
                                 cost_usd=usage.get("cost_usd", 0.0) or 0.0)

    def _broadcast(self, type_: str, payload: dict, agent_id: int) -> None:
        agent = self.db.get_agent(agent_id)
        session_id = agent["session_id"] if agent else "default"
        seq = self.db.insert_event(agent_id=agent_id, type=type_, payload=payload,
                                   session_id=session_id)
        if seq is not None:
            self.broadcaster.publish({"type": type_, "agent_id": agent_id,
                                      "payload": payload, "seq": seq}, seq=seq)

    @staticmethod
    def _require_session(body: dict, agent: dict) -> None:
        requested = body.get("session_id")
        if requested and requested != agent.get("session_id"):
            raise ValueError(f"agent {agent['id']} does not belong to session {requested}")

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
    if os.name != "nt":
        os.chmod(state_dir, 0o700)

    lock_path = state_dir / "daemon.lock"
    lock_handle = None
    if os.name != "nt":
        # POSIX：flock 排他锁跨进程互斥（进程退出自动释放，无残留问题）
        import fcntl
        lock_handle = open(lock_path, "a+")
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("daemon already running (startup lock held)", file=sys.stderr)
            return 0
    elif lock_path.is_file():
        # Windows 无 flock：退回 pid 活性启发式（原子性有限，文档注明）
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

    lock_data = {"pid": os.getpid(), "ts": time.time()}
    if lock_handle is not None:
        lock_handle.seek(0)
        lock_handle.truncate()
        lock_handle.write(json.dumps(lock_data))
        lock_handle.flush()
    else:
        _write_private(lock_path, lock_data)
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
        if os.name == "nt":
            lock_path.unlink(missing_ok=True)  # POSIX 保留文件，flock 随进程退出自动释放
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
