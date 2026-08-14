from __future__ import annotations
import gzip
import json
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
  payload TEXT NOT NULL, created_at TEXT,
  tier TEXT NOT NULL DEFAULT 'authority',
  payload_z BLOB
);
CREATE INDEX IF NOT EXISTS idx_events_tier ON events(tier);
CREATE TABLE IF NOT EXISTS usage (
  agent_id INTEGER NOT NULL, model TEXT NOT NULL,
  input_tokens INTEGER, output_tokens INTEGER,
  cache_creation INTEGER, cache_read INTEGER, cost_usd REAL, ts TEXT,
  PRIMARY KEY (agent_id, model)
);
CREATE INDEX IF NOT EXISTS idx_usage_agent ON usage(agent_id);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id INTEGER NOT NULL, role TEXT, content TEXT, ts TEXT
);
CREATE TABLE IF NOT EXISTS spawn_cache (
  hash TEXT PRIMARY KEY, agent_id INTEGER, result TEXT NOT NULL,
  created_at TEXT, expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_spawn_cache_expires ON spawn_cache(expires_at);
CREATE TABLE IF NOT EXISTS project_memory (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'lesson',
  key TEXT, content TEXT NOT NULL, tags TEXT,
  created_at TEXT NOT NULL, source TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_session_kind ON project_memory(session_id, kind);
CREATE INDEX IF NOT EXISTS idx_memory_session_created ON project_memory(session_id, created_at);
"""

# F7: verbose 层 payload 超 2KB 存 gzip 压缩；authority 层不压（查频高）
_PAYLOAD_GZIP_THRESHOLD = 2048
# F2: 每线程独立 sqlite 连接，上限 8（=槽位上限），空闲 60s 关
_MAX_DB_CONNECTIONS = 8
_DB_CONN_IDLE_TIMEOUT = 60.0

class DB:
    def __init__(self, path: Path | str, *, max_events: int = 100_000,
                 retention_days: int = 7, max_messages_per_agent: int = 500,
                 retain_interval: float = 60.0):
        self.path = Path(path)
        # F2: threading.local 每线程独立连接（WAL 模式已支持并发读）。
        # 上限 _MAX_DB_CONNECTIONS 连接（=槽位上限），空闲 _DB_CONN_IDLE_TIMEOUT 关。
        # 写仍串行：BEGIN IMMEDIATE 避忙等，self._lock 保留作写锁。
        self._local = threading.local()
        self._lock = threading.RLock()          # 写锁（写事务串行）
        self._conn_lock = threading.Lock()      # 连接池注册互斥
        self._conns: dict[int, sqlite3.Connection] = {}  # id(conn) -> conn（上限注册）
        self._conn_last_used: dict[int, float] = {}     # id(conn) -> monotonic 上次使用
        self._init_conn = self._new_conn()       # schema 初始化用连接
        # 旧库迁移兜底：老 events 表无 tier/payload_z 列，必须先补列再建索引/跑 schema，
        # 否则 CREATE INDEX idx_events_tier ON events(tier) 对无 tier 列的老表直接炸
        try:
            self._init_conn.execute("ALTER TABLE events ADD COLUMN tier TEXT NOT NULL DEFAULT 'authority'")
        except sqlite3.OperationalError:
            pass  # 列已存在（新库或已迁移）
        self._init_conn.executescript(SCHEMA)
        # F7: 旧库迁移补 payload_z 列（schema IF NOT EXISTS 只管建表，不管 ALTER）
        try:
            self._init_conn.execute("ALTER TABLE events ADD COLUMN payload_z BLOB")
        except sqlite3.OperationalError:
            pass  # 列已存在
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

    # ---- F2: 每线程独立连接池 ----

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _conn(self) -> sqlite3.Connection:
        """取本线程连接；无则建并注册（上限 _MAX_DB_CONNECTIONS，超限退回共享 init_conn）。"""
        c = getattr(self._local, "conn", None)
        if c is not None:
            return c
        with self._conn_lock:
            if len(self._conns) < _MAX_DB_CONNECTIONS:
                c = self._new_conn()
                self._conns[id(c)] = c
                self._conn_last_used[id(c)] = time.monotonic()
                self._local.conn = c
                return c
        # 池满：退回 schema 初始化连接（仍由 self._lock 写锁串行保护）
        return self._init_conn

    def _close_idle_conns(self) -> None:
        """低频清理：关闭空闲超 _DB_CONN_IDLE_TIMEOUT 的非本线程连接。"""
        now = time.monotonic()
        with self._conn_lock:
            stale = [cid for cid, t in self._conn_last_used.items()
                     if now - t > _DB_CONN_IDLE_TIMEOUT]
            for cid in stale:
                c = self._conns.pop(cid, None)
                self._conn_last_used.pop(cid, None)
                if c is not None and c is not getattr(self._local, "conn", None):
                    try:
                        c.close()
                    except Exception:
                        pass

    # ---- F7: verbose 层 payload gzip 压缩/解压 ----

    @staticmethod
    def _encode_payload(payload: dict, tier: str) -> tuple[str, bytes | None]:
        """返 (payload_text, payload_z)。verbose 层超 2KB 存 gzip；authority 层不压。"""
        text = _json_dumps(payload)
        if tier == "verbose" and len(text) > _PAYLOAD_GZIP_THRESHOLD:
            return text, gzip.compress(text.encode("utf-8"))
        return text, None

    @staticmethod
    def _decode_payload(row: sqlite3.Row) -> dict:
        """读时解压：payload_z 非空优先解压，否则直接 payload。"""
        z = row["payload_z"] if "payload_z" in row.keys() else None
        if z:
            try:
                return _json_loads(gzip.decompress(z).decode("utf-8"))
            except Exception:
                return {}
        try:
            return _json_loads(row["payload"])
        except Exception:
            return {}

    def _utc(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def insert_agent(self, *, parent_id, session_id, task_name, cli, model=None,
                     cwd=None, permission_mode=None, command_summary=None) -> int:
        with self._lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "INSERT INTO agents (parent_id, session_id, task_name, cli, model, cwd,"
                    " permission_mode, status, created_at, updated_at, command_summary)"
                    " VALUES (?,?,?,?,?,?,?, 'queued', ?, ?, ?)",
                    (parent_id, session_id, task_name, cli, model, cwd, permission_mode,
                     self._utc(), self._utc(), command_summary))
                conn.commit()
                return int(cur.lastrowid)
            except Exception:
                conn.rollback()
                raise

    def set_status(self, agent_id: int, status: str, *, stop_reason: str | None = None,
                   pid: int | None = None, cli_session_id: str | None = None) -> None:
        with self._lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE agents SET status=?, stop_reason=?, updated_at=?,"
                    " finished_at=COALESCE(finished_at, CASE WHEN ? IN ('terminated','error','cancelled','incomplete') THEN ? END),"
                    " pid=COALESCE(?, pid), cli_session_id=COALESCE(?, cli_session_id) WHERE id=?",
                    (status, stop_reason, self._utc(), status, self._utc(), pid,
                     cli_session_id, agent_id))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def touch_activity(self, agent_id: int) -> None:
        """运行中心跳：只更新 updated_at，不改 status、不触发状态机。"""
        with self._lock:
            conn = self._conn()
            conn.execute(
                "UPDATE agents SET updated_at=? WHERE id=?",
                (self._utc(), agent_id))
            conn.commit()

    def get_agent(self, agent_id: int) -> dict[str, Any] | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM agents WHERE id=?", (agent_id,)
        ).fetchone()
        return dict(row) if row else None

    def agents_by_session(self, session_id: str | None) -> list[dict[str, Any]]:
        """F3:一条 LEFT JOIN 取 agents + 每个 agent 最后一条 message（last_message 字段），
        消除 N+1（旧逐行 messages_for(size=1)）。无消息时 last_message=''。"""
        sql = ("SELECT a.*, COALESCE(m.content, '') AS last_message FROM agents a"
               " LEFT JOIN (SELECT agent_id, content FROM messages WHERE id IN"
               " (SELECT MAX(id) FROM messages GROUP BY agent_id)) m"
               " ON m.agent_id = a.id")
        conn = self._conn()
        if session_id is None:
            rows = conn.execute(sql + " ORDER BY a.id").fetchall()
        else:
            rows = conn.execute(sql + " WHERE a.session_id=? ORDER BY a.id",
                                (session_id,)).fetchall()
        return [dict(r) for r in rows]

    def insert_event(self, *, agent_id: int, type: str, payload: dict,
                     session_id: str, tier: str = "authority") -> int | None:
        if type == "agent.message_delta":
            return None
        # F7: verbose 层 payload 超 2KB 存 gzip 压缩（payload_z）；authority 层不压
        payload_text, payload_z = self._encode_payload(payload, tier)
        with self._lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "INSERT INTO events (session_id, agent_id, type, payload, created_at, tier, payload_z)"
                    " VALUES (?,?,?,?,?,?,?)", (session_id, agent_id, type,
                                        payload_text, self._utc(), tier, payload_z))
                conn.commit()
                self._maybe_retain()
                return int(cur.lastrowid)
            except Exception:
                conn.rollback()
                raise

    def events_since(self, seq: int, *, session_id: str | None = None,
                     limit: int = 1000, compress_consumed: bool = False,
                     keep_recent: int = 5, tier: str | None = None) -> list[dict[str, Any]]:
        """读事件。compress_consumed=true 时把已消费的 tool_use/tool_result
        payload 替成 [consumed]，只保留最近 keep_recent 条原文，裁主 agent 上下文。
        tier 非空时只返该层（authority/progress/verbose）。F7: payload_z 非空时解压。"""
        where = ["seq>?"]
        params: list[Any] = [seq]
        if session_id is not None:
            where.append("session_id=?")
            params.append(session_id)
        if tier is not None:
            where.append("tier=?")
            params.append(tier)
        sql = ("SELECT seq, session_id, agent_id, type, payload, created_at, tier, payload_z"
               " FROM events WHERE " + " AND ".join(where)
               + " ORDER BY seq LIMIT ?")
        params.append(limit)
        conn = self._conn()
        rows = conn.execute(sql, params).fetchall()
        out = []
        consumed_types = {"agent.tool_use", "agent.tool_result"}
        recent_consumed_indices = []
        for idx, r in enumerate(rows):
            d = dict(r)
            d["payload"] = self._decode_payload(r)
            if d.get("type") in consumed_types:
                recent_consumed_indices.append(idx)
            out.append(d)
        if compress_consumed and recent_consumed_indices:
            keep_set = set(recent_consumed_indices[-keep_recent:])
            for idx in recent_consumed_indices:
                if idx not in keep_set:
                    out[idx]["payload"] = {"consumed": True}
        return out

    def max_seq(self) -> int:
        """事件表当前最大 seq；无事件时返回 0（SSE 断线回放固定上界用）。"""
        conn = self._conn()
        row = conn.execute("SELECT MAX(seq) AS m FROM events").fetchone()
        return int(row["m"]) if row and row["m"] is not None else 0

    def events_by_agents(self, agent_ids: list[int], *, per_agent_limit: int = 60) -> list[dict[str, Any]]:
        """每个 agent 取最近 per_agent_limit 条事件，按 seq 合并升序（快照详情用）。
        F7: payload_z 非空时解压。

        避免 events_since 全局 limit 把后 spawn 的 agent 事件整体切掉。
        """
        out: list[dict[str, Any]] = []
        conn = self._conn()
        for agent_id in agent_ids:
            rows = conn.execute(
                "SELECT seq, session_id, agent_id, type, payload, created_at, payload_z FROM events"
                " WHERE agent_id=? ORDER BY seq DESC LIMIT ?",
                (agent_id, per_agent_limit)).fetchall()
            for r in rows:
                d = dict(r)
                d["payload"] = self._decode_payload(r)
                out.append(d)
        out.sort(key=lambda e: e["seq"])
        return out

    def upsert_usage(self, *, agent_id: int, model: str, input_tokens: int,
                     output_tokens: int, cache_creation: int, cache_read: int,
                     cost_usd: float) -> None:
        with self._lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO usage (agent_id, model, input_tokens, output_tokens,"
                    " cache_creation, cache_read, cost_usd, ts) VALUES (?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(agent_id, model) DO UPDATE SET"
                    " input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,"
                    " cache_creation=excluded.cache_creation, cache_read=excluded.cache_read,"
                    " cost_usd=excluded.cost_usd, ts=excluded.ts",
                    (agent_id, model, input_tokens, output_tokens, cache_creation,
                     cache_read, cost_usd, self._utc()))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def usage_total(self, agent_id: int) -> dict[str, int | float]:
        conn = self._conn()
        row = conn.execute(
            "SELECT COALESCE(SUM(input_tokens),0) input_tokens,"
            " COALESCE(SUM(output_tokens),0) output_tokens,"
            " COALESCE(SUM(cache_creation),0) cache_creation,"
            " COALESCE(SUM(cache_read),0) cache_read,"
            " COALESCE(SUM(cost_usd),0) cost_usd FROM usage WHERE agent_id=?",
            (agent_id,),
        ).fetchone()
        out = dict(row)
        # ET 有效 token：cache-read 折 0.1（便宜），output 折 4（贵），解耦工作量与效率
        out["et"] = (int(out.get("input_tokens") or 0)
                     - int(out.get("cache_read") or 0) * 0.9
                     + int(out.get("output_tokens") or 0) * 4)
        return out

    def usage_series(self, hours: int = 24) -> list[dict[str, Any]]:
        """按小时聚合 usage（最近 N 小时，缺失补 0），趋势图数据源。
        数据源：state_dir/usage/*.jsonl（每 run 一行，含 ts 字段）。
        每小时一行 {ts, input, output, cache_read, cost}。"""
        try:
            hours = max(1, min(int(hours), 168))
        except (TypeError, ValueError):
            hours = 24
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc).replace(minute=0, second=0, microsecond=0)
        buckets: dict[str, dict[str, Any]] = {}
        for i in range(hours - 1, -1, -1):
            h = now - datetime.timedelta(hours=i)
            key = h.strftime("%Y-%m-%dT%H:00:00Z")
            buckets[key] = {"ts": key, "input": 0, "output": 0,
                            "cache_read": 0, "cost": 0.0}
        usage_dir = self.path.parent / "usage"
        if not usage_dir.is_dir():
            return list(buckets.values())
        try:
            for f in usage_dir.glob("*.jsonl"):
                try:
                    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                        if not line.strip():
                            continue
                        rec = json.loads(line)
                        ts = str(rec.get("ts") or "")
                        if not ts:
                            continue
                        key = ts[:13] + ":00:00Z"  # YYYY-MM-DDTHH:00:00Z
                        if key not in buckets:
                            continue
                        b = buckets[key]
                        b["input"] += int(rec.get("input_tokens") or 0)
                        b["output"] += int(rec.get("output_tokens") or 0)
                        b["cache_read"] += int(rec.get("cache_read") or 0)
                        b["cost"] += float(rec.get("cost_usd") or 0.0)
                except (OSError, json.JSONDecodeError):
                    continue  # 坏行跳过
        except Exception:
            pass
        return list(buckets.values())

    def agent_anomalies(self, agent_id: int) -> list[dict[str, Any]]:
        """D2: daemon 侧预计算异常 badge——stuck loop / 高失败率 / 静默 / 超时。
        前端免扫全事件流，snapshot 直挂 anomalies。口径宽松免漏报：
          - stuck_loop：同 tool+file 重复 5+（tool_use payload 的 name+file）
          - high_fail：tool_result 失败率 >30%（至少 5 条样本）
          - silent：近 60 条事件无 tool_use/message 且 status=running
          - timeout：stop_reason=timeout 或 status=incomplete
        返 [{"kind":"stuck_loop","detail":"…"}] 形，空列表=无异常。"""
        conn = self._conn()
        rows = conn.execute(
            "SELECT type, payload FROM events WHERE agent_id=?"
            " ORDER BY seq DESC LIMIT 80",
            (agent_id,)).fetchall()
        out: list[dict[str, Any]] = []
        if not rows:
            return out
        # timeout / incomplete 直查 agents 表（set_status 落 stop_reason）
        a = self.get_agent(agent_id) or {}
        if a.get("stop_reason") == "timeout" or a.get("status") == "incomplete":
            out.append({"kind": "timeout", "detail": "超时未完成"})
        # stuck_loop：tool_use payload 的 name+file 重复 5+
        tool_keys: list[str] = []
        for r in rows:
            if r["type"] != "agent.tool_use":
                continue
            try:
                p = json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
            except Exception:
                p = {}
            key = str(p.get("name", "")) + "|" + str(p.get("file") or p.get("path") or "")
            if key and key != "|":
                tool_keys.append(key)
        if tool_keys:
            counts: dict[str, int] = {}
            for k in tool_keys:
                counts[k] = counts.get(k, 0) + 1
            for k, n in counts.items():
                if n >= 5:
                    out.append({"kind": "stuck_loop", "detail": f"同工具重复 {n} 次：{k.split('|')[0]}"})
                    break
        # high_fail：tool_result 失败率 >30%（样本≥5）
        succ = fail = 0
        for r in rows:
            if r["type"] != "agent.tool_result":
                continue
            try:
                p = json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
            except Exception:
                p = {}
            if p.get("is_error") or p.get("error"):
                fail += 1
            else:
                succ += 1
        total = succ + fail
        if total >= 5 and fail / total > 0.3:
            out.append({"kind": "high_fail", "detail": f"工具失败率 {fail}/{total}"})
        # silent：running 且近 60 条无 tool_use/message（静默）
        if a.get("status") == "running":
            has_activity = any(r["type"] in ("agent.tool_use", "agent.message")
                               for r in rows[:60])
            if not has_activity:
                out.append({"kind": "silent", "detail": "近 60 事件无通信"})
        return out

    def insert_message(self, *, agent_id: int, role: str, content: str) -> None:
        with self._lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("INSERT INTO messages (agent_id, role, content, ts)"
                            " VALUES (?,?,?,?)", (agent_id, role, content, self._utc()))
                conn.execute(
                    "DELETE FROM messages WHERE id IN (SELECT id FROM messages WHERE agent_id=?"
                    " ORDER BY id DESC LIMIT -1 OFFSET ?)", (agent_id, self.max_messages_per_agent))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def messages_for(self, agent_id: int, *, page: int = 0, size: int = 100) -> list[dict[str, Any]]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, role, content, ts FROM messages WHERE agent_id=?"
            " ORDER BY id LIMIT ? OFFSET ?",
            (agent_id, size, page * size),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- 记忆银行（阶段 1）：project_memory 键值式记忆 + LIKE 关键词检索 ----

    def insert_memory(self, *, session_id: str, kind: str = "lesson",
                      key: str | None = None, content: str, tags: str | None = None,
                      source: str | None = None) -> int:
        """写一条记忆（session_id 隔离；kind/key/tags 供检索过滤；source 记来源）。"""
        with self._lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "INSERT INTO project_memory (session_id, kind, key, content, tags,"
                    " created_at, source) VALUES (?,?,?,?,?,?,?)",
                    (session_id, kind, key, content, tags, self._utc(), source))
                conn.commit()
                return int(cur.lastrowid)
            except Exception:
                conn.rollback()
                raise

    def recall_memories(self, session_id: str, *, query: str | None = None,
                        kind: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        """检索记忆：query 关键词 LIKE 命中 content/key/tags，可按 kind 过滤，
        按 created_at DESC 收敛 limit 条（同 session 隔离）。"""
        where = ["session_id=?"]
        params: list[Any] = [session_id]
        if query:
            like = f"%{query}%"
            where.append("(content LIKE ? OR key LIKE ? OR tags LIKE ?)")
            params += [like, like, like]
        if kind:
            where.append("kind=?")
            params.append(kind)
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, kind, key, content, tags, created_at, source FROM project_memory"
            " WHERE " + " AND ".join(where)
            + " ORDER BY created_at DESC, id DESC LIMIT ?",
            params + [limit]).fetchall()
        return [dict(r) for r in rows]

    def spawn_cache_get(self, key: str) -> dict[str, Any] | None:
        """命中返回 {result: dict, agent_id: int}；过期/缺失返回 None，并惰清过期项。"""
        now = self._utc()
        conn = self._conn()
        row = conn.execute(
            "SELECT agent_id, result, expires_at FROM spawn_cache WHERE hash=?",
            (key,)).fetchone()
        if row is None:
            return None
        if row["expires_at"] < now:
            with self._lock:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute("DELETE FROM spawn_cache WHERE hash=?", (key,))
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            return None
        return {"agent_id": row["agent_id"], "result": _json_loads(row["result"])}

    def spawn_cache_put(self, key: str, agent_id: int, result: dict,
                        ttl_seconds: float) -> None:
        now = self._utc()
        expires = datetime.now(timezone.utc).timestamp() + ttl_seconds
        expires_at = datetime.fromtimestamp(expires, timezone.utc).isoformat()
        with self._lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO spawn_cache (hash, agent_id, result, created_at, expires_at)"
                    " VALUES (?,?,?,?,?)"
                    " ON CONFLICT(hash) DO UPDATE SET agent_id=excluded.agent_id,"
                    " result=excluded.result, created_at=excluded.created_at,"
                    " expires_at=excluded.expires_at",
                    (key, agent_id, _json_dumps(result), now, expires_at))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def purge_spawn_cache(self) -> int:
        """D3: 删除所有过期 spawn_cache 行，返回删除条数。"""
        now = self._utc()
        with self._lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "DELETE FROM spawn_cache WHERE expires_at < ?", (now,))
                conn.commit()
                return cur.rowcount or 0
            except Exception:
                conn.rollback()
                raise

    def purge_events(self) -> int:
        """D4: 删除 created_at 早于 7 天的 events 并 VACUUM，返回删除条数。"""
        cutoff = datetime.now(timezone.utc).timestamp() - 7 * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        with self._lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "DELETE FROM events WHERE created_at < ?", (cutoff_iso,))
                deleted = cur.rowcount or 0
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            # VACUUM 不能在事务内执行
            conn.execute("VACUUM")
            return deleted

    def _maybe_retain(self) -> None:
        # 低频清理：retain_interval 秒内最多跑一次，避免每次 insert_event 全表 COUNT
        now = time.monotonic()
        if now - self._last_retain < self.retain_interval:
            return
        self._last_retain = now
        conn = self._conn()
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        if count > self.max_events:
            keep_seq = conn.execute(
                "SELECT seq FROM events ORDER BY seq DESC LIMIT 1 OFFSET ?",
                (self.max_events,)).fetchone()
            if keep_seq:
                conn.execute("DELETE FROM events WHERE seq < ? AND type NOT IN"
                           " ('agent.terminated','agent.usage')", (keep_seq[0],))
        if self.retention_days > 0:
            cutoff = datetime.now(timezone.utc).timestamp() - self.retention_days * 86400
            conn.execute("DELETE FROM events WHERE created_at < ?",
                       (datetime.fromtimestamp(cutoff, timezone.utc).isoformat(),))
        conn.commit()
        # F2: 顺手清理空闲超阈的非本线程连接
        self._close_idle_conns()

def _json_dumps(payload: dict) -> str:
    import json
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

def _json_loads(s: str) -> dict:
    import json
    return json.loads(s)
