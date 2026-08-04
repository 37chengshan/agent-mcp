from __future__ import annotations
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS agents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_id INTEGER, session_id TEXT NOT NULL, task_name TEXT NOT NULL,
  cli TEXT NOT NULL, model TEXT, cwd TEXT, permission_mode TEXT,
  status TEXT NOT NULL DEFAULT 'queued', stop_reason TEXT,
  created_at TEXT, updated_at TEXT, finished_at TEXT,
  pid INTEGER, cli_session_id TEXT, command_summary TEXT
);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL, agent_id INTEGER, type TEXT NOT NULL,
  payload TEXT NOT NULL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS usage (
  agent_id INTEGER NOT NULL, model TEXT NOT NULL,
  input_tokens INTEGER, output_tokens INTEGER,
  cache_creation INTEGER, cache_read INTEGER, cost_usd REAL, ts TEXT,
  PRIMARY KEY (agent_id, model)
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id INTEGER NOT NULL, role TEXT, content TEXT, ts TEXT
);
"""

class DB:
    def __init__(self, path: Path | str, *, max_events: int = 100_000,
                 retention_days: int = 7, max_messages_per_agent: int = 500,
                 retain_interval: float = 60.0):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # 跨进程写（daemon + worker ingest）锁等待：10s 而非 sqlite3 默认 5s
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(SCHEMA)
        self.max_events = max_events
        self.retention_days = retention_days
        self.max_messages_per_agent = max_messages_per_agent
        self.retain_interval = retain_interval
        self._last_retain = 0.0
        if os.name != "nt":
            # WAL/SHM 与主库同敏感度，一并收紧为 0600（WAL 模式下由 executescript 创建）
            for p in (self.path, self.path.with_suffix(".db-wal"),
                      self.path.with_suffix(".db-shm")):
                try:
                    os.chmod(p, 0o600)
                except OSError:
                    pass

    def _utc(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def insert_agent(self, *, parent_id, session_id, task_name, cli, model=None,
                     cwd=None, permission_mode=None, command_summary=None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO agents (parent_id, session_id, task_name, cli, model, cwd,"
                " permission_mode, status, created_at, updated_at, command_summary)"
                " VALUES (?,?,?,?,?,?,?, 'queued', ?, ?, ?)",
                (parent_id, session_id, task_name, cli, model, cwd, permission_mode,
                 self._utc(), self._utc(), command_summary))
            self._conn.commit()
            return int(cur.lastrowid)

    def set_status(self, agent_id: int, status: str, *, stop_reason: str | None = None,
                   pid: int | None = None, cli_session_id: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE agents SET status=?, stop_reason=?, updated_at=?,"
                " finished_at=COALESCE(finished_at, CASE WHEN ? IN ('terminated','error','cancelled','incomplete') THEN ? END),"
                " pid=COALESCE(?, pid), cli_session_id=COALESCE(?, cli_session_id) WHERE id=?",
                (status, stop_reason, self._utc(), status, self._utc(), pid,
                 cli_session_id, agent_id))
            self._conn.commit()

    def get_agent(self, agent_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        return dict(row) if row else None

    def agents_by_session(self, session_id: str | None) -> list[dict[str, Any]]:
        if session_id is None:
            rows = self._conn.execute("SELECT * FROM agents ORDER BY id").fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM agents WHERE session_id=? ORDER BY id",
                                      (session_id,)).fetchall()
        return [dict(r) for r in rows]

    def insert_event(self, *, agent_id: int, type: str, payload: dict,
                     session_id: str) -> int | None:
        if type == "agent.message_delta":
            return None
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (session_id, agent_id, type, payload, created_at)"
                " VALUES (?,?,?,?,?)", (session_id, agent_id, type,
                                        _json_dumps(payload), self._utc()))
            self._conn.commit()
            self._maybe_retain()
            return int(cur.lastrowid)

    def events_since(self, seq: int, *, session_id: str | None = None,
                     limit: int = 1000) -> list[dict[str, Any]]:
        if session_id is None:
            rows = self._conn.execute(
                "SELECT seq, session_id, agent_id, type, payload, created_at FROM events"
                " WHERE seq>? ORDER BY seq LIMIT ?", (seq, limit)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT seq, session_id, agent_id, type, payload, created_at FROM events"
                " WHERE seq>? AND session_id=? ORDER BY seq LIMIT ?",
                (seq, session_id, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = _json_loads(d["payload"])
            except Exception:
                d["payload"] = {}
            out.append(d)
        return out

    def max_seq(self) -> int:
        """事件表当前最大 seq；无事件时返回 0（SSE 断线回放固定上界用）。"""
        row = self._conn.execute("SELECT MAX(seq) AS m FROM events").fetchone()
        return int(row["m"]) if row and row["m"] is not None else 0

    def events_by_agents(self, agent_ids: list[int], *, per_agent_limit: int = 60) -> list[dict[str, Any]]:
        """每个 agent 取最近 per_agent_limit 条事件，按 seq 合并升序（快照详情用）。

        避免 events_since 全局 limit 把后 spawn 的 agent 事件整体切掉。
        """
        out: list[dict[str, Any]] = []
        for agent_id in agent_ids:
            rows = self._conn.execute(
                "SELECT seq, session_id, agent_id, type, payload, created_at FROM events"
                " WHERE agent_id=? ORDER BY seq DESC LIMIT ?",
                (agent_id, per_agent_limit)).fetchall()
            for r in rows:
                d = dict(r)
                try:
                    d["payload"] = _json_loads(d["payload"])
                except Exception:
                    d["payload"] = {}
                out.append(d)
        out.sort(key=lambda e: e["seq"])
        return out

    def upsert_usage(self, *, agent_id: int, model: str, input_tokens: int,
                     output_tokens: int, cache_creation: int, cache_read: int,
                     cost_usd: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO usage (agent_id, model, input_tokens, output_tokens,"
                " cache_creation, cache_read, cost_usd, ts) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(agent_id, model) DO UPDATE SET"
                " input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,"
                " cache_creation=excluded.cache_creation, cache_read=excluded.cache_read,"
                " cost_usd=excluded.cost_usd, ts=excluded.ts",
                (agent_id, model, input_tokens, output_tokens, cache_creation,
                 cache_read, cost_usd, self._utc()))
            self._conn.commit()

    def usage_total(self, agent_id: int) -> dict[str, int | float]:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(input_tokens),0) input_tokens,"
            " COALESCE(SUM(output_tokens),0) output_tokens,"
            " COALESCE(SUM(cache_creation),0) cache_creation,"
            " COALESCE(SUM(cache_read),0) cache_read,"
            " COALESCE(SUM(cost_usd),0) cost_usd FROM usage WHERE agent_id=?", (agent_id,)).fetchone()
        return dict(row)

    def insert_message(self, *, agent_id: int, role: str, content: str) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO messages (agent_id, role, content, ts)"
                               " VALUES (?,?,?,?)", (agent_id, role, content, self._utc()))
            self._conn.execute(
                "DELETE FROM messages WHERE id IN (SELECT id FROM messages WHERE agent_id=?"
                " ORDER BY id DESC LIMIT -1 OFFSET ?)", (agent_id, self.max_messages_per_agent))
            self._conn.commit()

    def messages_for(self, agent_id: int, *, page: int = 0, size: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, role, content, ts FROM messages WHERE agent_id=? ORDER BY id LIMIT ? OFFSET ?",
            (agent_id, size, page * size)).fetchall()
        return [dict(r) for r in rows]

    def _maybe_retain(self) -> None:
        # 低频清理：retain_interval 秒内最多跑一次，避免每次 insert_event 全表 COUNT
        now = time.monotonic()
        if now - self._last_retain < self.retain_interval:
            return
        self._last_retain = now
        count = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        if count > self.max_events:
            keep_seq = self._conn.execute(
                "SELECT seq FROM events ORDER BY seq DESC LIMIT 1 OFFSET ?",
                (self.max_events,)).fetchone()
            if keep_seq:
                self._conn.execute("DELETE FROM events WHERE seq < ? AND type NOT IN"
                                   " ('agent.terminated','agent.usage')", (keep_seq[0],))
        if self.retention_days > 0:
            cutoff = datetime.now(timezone.utc).timestamp() - self.retention_days * 86400
            self._conn.execute("DELETE FROM events WHERE created_at < ?",
                               (datetime.fromtimestamp(cutoff, timezone.utc).isoformat(),))
        self._conn.commit()

def _json_dumps(payload: dict) -> str:
    import json
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

def _json_loads(s: str) -> dict:
    import json
    return json.loads(s)
