"""v4 Control Plane 持久层：runs / command_journal / goals / schedules / harness / refinement / tasks / meta。

设计：独立于既有 db.py（不改动旧表结构与旧方法）；构造时执行 V4_SCHEMA DDL，
并为 events 表补 generation 列（{generation, seq} 游标的地基）。所有方法复用
db.DB 的线程连接池/写锁/_utc，保持与既有 daemon 相同的并发语义。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from . import models as m
from .db import DB


V4_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  run_kind TEXT NOT NULL,
  trigger_type TEXT, trigger_ref TEXT,
  policy_json TEXT,
  status TEXT NOT NULL DEFAULT 'PENDING',
  stop_reason TEXT,
  agent_id INTEGER, parent_run_id TEXT, origin_command_id TEXT,
  attempts INTEGER NOT NULL DEFAULT 1,
  error_fingerprint TEXT,
  created_at TEXT, updated_at TEXT, completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_agent ON runs(agent_id);
CREATE INDEX IF NOT EXISTS idx_runs_trigger ON runs(trigger_type, trigger_ref);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE TABLE IF NOT EXISTS command_journal (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  client_id TEXT NOT NULL, command_id TEXT NOT NULL, request_hash TEXT NOT NULL,
  method TEXT NOT NULL, params_json TEXT,
  state TEXT NOT NULL DEFAULT 'recorded',
  result_json TEXT,
  created_at TEXT NOT NULL, completed_at TEXT,
  UNIQUE(client_id, command_id)
);
CREATE INDEX IF NOT EXISTS idx_journal_client ON command_journal(client_id, command_id, request_hash);
CREATE TABLE IF NOT EXISTS goals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id INTEGER, session_id TEXT NOT NULL,
  objective TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  rounds INTEGER NOT NULL DEFAULT 0,
  token_budget INTEGER, time_budget_seconds INTEGER,
  tokens_used INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_goals_session ON goals(session_id, status);
CREATE TABLE IF NOT EXISTS schedules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id INTEGER, session_id TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'one_shot',
  prompt TEXT NOT NULL,
  interval_expr TEXT,
  next_tick TEXT, claim_id TEXT, last_tick TEXT,
  paused INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_next ON schedules(next_tick);
CREATE TABLE IF NOT EXISTS harness_items (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  content TEXT, content_ref TEXT,
  path TEXT NOT NULL DEFAULT 'general',
  scope TEXT NOT NULL DEFAULT 'local',
  version INTEGER NOT NULL DEFAULT 1,
  provenance TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS harness_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT NOT NULL, revision INTEGER NOT NULL, prev_revision INTEGER,
  op TEXT NOT NULL, diff_json TEXT, snapshot_json TEXT,
  author TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_harness_rev ON harness_revisions(item_id, revision);
CREATE TABLE IF NOT EXISTS refinement_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trigger TEXT NOT NULL, session_id TEXT NOT NULL, run_id TEXT,
  fingerprint TEXT, changes_json TEXT, evidence TEXT,
  cooldown_until TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_refine_fingerprint ON refinement_events(fingerprint);
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
  agent_id INTEGER, host_handle TEXT, status TEXT NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
"""


def _j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class StoreV4:
    """v4 Control Plane 持久层。构造时自动建表/迁移；所有写操作在 db._lock 下串行。"""

    def __init__(self, db: DB):
        self.db = db
        conn = db._init_conn
        try:
            conn.execute("ALTER TABLE events ADD COLUMN generation INTEGER")
        except Exception:
            pass  # 列已存在
        conn.executescript(V4_SCHEMA)

    # ---- meta / generation（Invariant 9：重启不产生 generation collision） ----
    def meta_get(self, key: str) -> str | None:
        conn = self.db._conn()
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def meta_set(self, key: str, value: str) -> None:
        with self.db._lock:
            conn = self.db._conn()
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            conn.commit()

    def next_generation(self) -> tuple[int, str]:
        """持久化单调 epoch + 随机 nonce：每次 daemon 启动 +1，重启绝不复用旧代。"""
        with self.db._lock:
            conn = self.db._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT value FROM meta WHERE key='gen_epoch'").fetchone()
                epoch = int(row["value"]) + 1 if row and row["value"] else 1
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('gen_epoch', ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(epoch),),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return epoch, uuid.uuid4().hex[:8]

    # ---- command_journal（Invariant 1/2：幂等 / conflict / uncertain / ack / compaction） ----
    def journal_record(
        self,
        *,
        client_id: str,
        command_id: str,
        request_hash: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """记录命令（幂等锚点）。返回 (entry, created)。

        created=True 表示 THIS caller 插入了行（唯一执行权）；
        created=False 表示并发/重试撞上既有行，调用方必须走 replay 路径（J2 TOCTOU）。
        """
        params_json = _j(params or {})
        with self.db._lock:
            conn = self.db._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO command_journal"
                    " (client_id, command_id, request_hash, method, params_json, state, created_at)"
                    " VALUES (?,?,?,?,?,'recorded',?)",
                    (client_id, command_id, request_hash, method, params_json, self.db._utc()),
                )
                created = (cur.rowcount or 0) == 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        entry = self.journal_get(client_id, command_id)
        return entry, created

    def journal_recover_orphans(self, *, max_age_seconds: float = 30.0,
                                all_stale: bool = False) -> int:
        """J1 崩溃恢复：stale recorded → uncertain，禁止盲目重放。

        all_stale=True（daemon 启动）：任何 recorded 都是上一进程孤儿；
        否则只回收超过 max_age_seconds 的 recorded（长卡死）。
        """
        with self.db._lock:
            conn = self.db._conn()
            if all_stale:
                cur = conn.execute(
                    "UPDATE command_journal SET state='uncertain' WHERE state='recorded'")
            else:
                cutoff = (datetime.now(timezone.utc)
                          - timedelta(seconds=max_age_seconds)).isoformat()
                cur = conn.execute(
                    "UPDATE command_journal SET state='uncertain'"
                    " WHERE state='recorded' AND created_at < ?",
                    (cutoff,),
                )
            conn.commit()
            return cur.rowcount or 0

    def journal_get(self, client_id: str, command_id: str) -> dict[str, Any] | None:
        conn = self.db._conn()
        row = conn.execute(
            "SELECT id, client_id, command_id, request_hash, method, params_json, state, result_json,"
            " created_at, completed_at FROM command_journal WHERE client_id=? AND command_id=?",
            (client_id, command_id),
        ).fetchone()
        return dict(row) if row else None

    def journal_complete(
        self,
        *,
        client_id: str,
        command_id: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        """完成命令：落库结果。仅当 journal 判定为 new 时调用一次（调用方保证）。"""
        result_json = _j(result or {})
        with self.db._lock:
            conn = self.db._conn()
            conn.execute(
                "UPDATE command_journal SET state='completed', result_json=?, completed_at=?"
                " WHERE client_id=? AND command_id=? AND state='recorded'",
                (result_json, self.db._utc(), client_id, command_id),
            )
            conn.commit()

    def journal_mark_uncertain(self, client_id: str, command_id: str) -> None:
        """已接收但无持久结果 → uncertain（下次不盲目重放，报不确定）。"""
        with self.db._lock:
            conn = self.db._conn()
            conn.execute(
                "UPDATE command_journal SET state='uncertain' WHERE client_id=? AND command_id=?",
                (client_id, command_id),
            )
            conn.commit()

    def journal_ack(self, client_id: str, up_to_command_id: str) -> int:
        """客户端确认水位：将 <= 该 command 对应 journal 行 id 且 completed 的条目标 acked。"""
        with self.db._lock:
            conn = self.db._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                anchor = conn.execute(
                    "SELECT MAX(id) id FROM command_journal WHERE client_id=? AND command_id=?",
                    (client_id, up_to_command_id),
                ).fetchone()
                if not anchor or anchor["id"] is None:
                    return 0
                cur = conn.execute(
                    "UPDATE command_journal SET state='acked'"
                    " WHERE client_id=? AND id<=? AND state='completed'",
                    (client_id, anchor["id"]),
                )
                conn.commit()
                return cur.rowcount or 0
            except Exception:
                conn.rollback()
                raise

    def journal_compaction_candidates(self, client_id: str) -> list[int]:
        """安全压缩候选：<= 已确认水位（最后一个 acked 行 id）且 state=acked 的条目。

        禁 LRU 删除：未确认的条目绝不压缩（保持幂等）。
        """
        conn = self.db._conn()
        row = conn.execute(
            "SELECT MAX(id) m FROM command_journal WHERE client_id=? AND state='acked'",
            (client_id,),
        ).fetchone()
        if not row or row["m"] is None:
            return []
        rows = conn.execute(
            "SELECT id FROM command_journal WHERE client_id=? AND id<=? AND state='acked'",
            (client_id, row["m"]),
        ).fetchall()
        return [int(r["id"]) for r in rows]

    def journal_compact(self, client_id: str, watermark_id: int) -> int:
        """压缩：仅删除 <= watermark_id 且 state=acked 的条目。返回删除条数。"""
        with self.db._lock:
            conn = self.db._conn()
            cur = conn.execute(
                "DELETE FROM command_journal WHERE client_id=? AND id<=? AND state='acked'",
                (client_id, watermark_id),
            )
            conn.commit()
            return cur.rowcount or 0

    # ---- runs（唯一执行单位） ----
    def run_create(
        self,
        *,
        run_kind: str,
        trigger_type: str | None = None,
        trigger_ref: str | int | None = None,
        policy_json: str | None = None,
        origin_command_id: str | None = None,
        parent_run_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        run_id = run_id or uuid.uuid4().hex
        now = self.db._utc()
        with self.db._lock:
            conn = self.db._conn()
            conn.execute(
                "INSERT INTO runs (id, run_kind, trigger_type, trigger_ref, policy_json,"
                " status, origin_command_id, parent_run_id, created_at, updated_at)"
                " VALUES (?,?,?,?,?,'PENDING',?,?,?,?)",
                (
                    run_id,
                    run_kind,
                    trigger_type,
                    None if trigger_ref is None else str(trigger_ref),
                    policy_json,
                    origin_command_id,
                    parent_run_id,
                    now,
                    now,
                ),
            )
            conn.commit()
        return self.run_get(run_id)

    def run_get(self, run_id: str) -> dict[str, Any] | None:
        conn = self.db._conn()
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def runs_for_agent(self, agent_id: int) -> list[dict[str, Any]]:
        conn = self.db._conn()
        rows = conn.execute(
            "SELECT * FROM runs WHERE agent_id=? ORDER BY created_at ASC", (agent_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def runs_all(self) -> list[dict[str, Any]]:
        conn = self.db._conn()
        rows = conn.execute("SELECT * FROM runs ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]

    def runs_list(self, limit: int = 100) -> list[dict[str, Any]]:
        conn = self.db._conn()
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(r) for r in rows]

    def runs_in_status(self, *statuses: str) -> list[dict[str, Any]]:
        conn = self.db._conn()
        placeholders = ",".join("?" for _ in statuses)
        rows = conn.execute(
            "SELECT * FROM runs WHERE status IN (%s)" % placeholders, statuses
        ).fetchall()
        return [dict(r) for r in rows]

    def run_transition(
        self, run_id: str, status: str, *, stop_reason: str | None = None
    ) -> dict[str, Any]:
        """按 Run 状态机推进；非法转移抛 ValueError（终端态不可再动）。"""
        with self.db._lock:
            conn = self.db._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
                if row is None:
                    raise ValueError("run not found: %s" % run_id)
                m.run_transition(str(row["status"]), status)
                now = self.db._utc()
                conn.execute(
                    "UPDATE runs SET status=?, stop_reason=COALESCE(?, stop_reason),"
                    " updated_at=?, completed_at=COALESCE(completed_at, CASE WHEN ? IN"
                    " ('COMPLETED','FAILED','CANCELLED','INTERRUPTED','INCOMPLETE') THEN ? END)"
                    " WHERE id=?",
                    (status, stop_reason, now, status, now, run_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.run_get(run_id)

    def run_bind_agent(self, run_id: str, agent_id: int) -> None:
        with self.db._lock:
            conn = self.db._conn()
            conn.execute("UPDATE runs SET agent_id=?, updated_at=? WHERE id=?", (agent_id, self.db._utc(), run_id))
            conn.commit()

    def run_bump_attempts(self, run_id: str) -> int:
        with self.db._lock:
            conn = self.db._conn()
            conn.execute("UPDATE runs SET attempts=attempts+1, updated_at=? WHERE id=?", (self.db._utc(), run_id))
            conn.commit()
        run = self.run_get(run_id)
        return int(run["attempts"]) if run else 0

    def run_set_error_fingerprint(self, run_id: str, fingerprint: str) -> None:
        with self.db._lock:
            conn = self.db._conn()
            conn.execute("UPDATE runs SET error_fingerprint=?, updated_at=? WHERE id=?", (fingerprint, self.db._utc(), run_id))
            conn.commit()

    # ---- goals（Trigger/Intent，不含执行） ----
    def goal_create(
        self,
        *,
        session_id: str,
        objective: str,
        agent_id: int | None = None,
        token_budget: int | None = None,
        time_budget_seconds: int | None = None,
    ) -> int:
        now = self.db._utc()
        with self.db._lock:
            conn = self.db._conn()
            cur = conn.execute(
                "INSERT INTO goals (agent_id, session_id, objective, token_budget,"
                " time_budget_seconds, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (agent_id, session_id, objective, token_budget, time_budget_seconds, now, now),
            )
            conn.commit()
            return int(cur.lastrowid)

    def goal_get(self, goal_id: int) -> dict[str, Any] | None:
        conn = self.db._conn()
        row = conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        return dict(row) if row else None

    def goals_by_session(self, session_id: str, *, active_only: bool = False) -> list[dict[str, Any]]:
        conn = self.db._conn()
        sql = "SELECT * FROM goals WHERE session_id=?"
        if active_only:
            sql += " AND status='active'"
        rows = conn.execute(sql + " ORDER BY id DESC", (session_id,)).fetchall()
        return [dict(r) for r in rows]

    def goals_active(self) -> list[dict[str, Any]]:
        conn = self.db._conn()
        rows = conn.execute("SELECT * FROM goals WHERE status='active' ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]

    def goals_list(self, session_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        conn = self.db._conn()
        if session_id:
            rows = conn.execute(
                "SELECT * FROM goals WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM goals ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(r) for r in rows]

    def goal_update_status(self, goal_id: int, status: str) -> dict[str, Any] | None:
        if status not in m.GOAL_STATUSES:
            raise ValueError("invalid goal status: %s" % status)
        now = self.db._utc()
        with self.db._lock:
            conn = self.db._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()
                if row is None:
                    raise ValueError("goal not found: %s" % goal_id)
                # 非法迁移（completed→active 等）拒绝
                m.goal_transition(str(row["status"]), status)
                conn.execute(
                    "UPDATE goals SET status=?, updated_at=?, completed_at=COALESCE(completed_at,"
                    " CASE WHEN ?='completed' THEN ? END) WHERE id=?",
                    (status, now, status, now, goal_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.goal_get(goal_id)

    def goal_bump_round(self, goal_id: int) -> dict[str, Any] | None:
        with self.db._lock:
            conn = self.db._conn()
            conn.execute("UPDATE goals SET rounds=rounds+1, updated_at=? WHERE id=?", (self.db._utc(), goal_id))
            conn.commit()
        return self.goal_get(goal_id)

    def goal_add_tokens(self, goal_id: int, tokens: int) -> None:
        with self.db._lock:
            conn = self.db._conn()
            conn.execute("UPDATE goals SET tokens_used=tokens_used+?, updated_at=? WHERE id=?", (tokens, self.db._utc(), goal_id))
            conn.commit()

    # ---- schedules（tick claim 先于派发；Invariant 3） ----
    def schedule_create(
        self,
        *,
        session_id: str,
        prompt: str,
        kind: str = "one_shot",
        interval_expr: str | None = None,
        next_tick: str | None = None,
        agent_id: int | None = None,
    ) -> int:
        if kind not in ("one_shot", "cron", "heartbeat"):
            raise ValueError("invalid schedule kind: %s" % kind)
        now = self.db._utc()
        with self.db._lock:
            conn = self.db._conn()
            cur = conn.execute(
                "INSERT INTO schedules (agent_id, session_id, kind, prompt, interval_expr,"
                " next_tick, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (agent_id, session_id, kind, prompt, interval_expr, next_tick, now, now),
            )
            conn.commit()
            return int(cur.lastrowid)

    def schedules_due(self, now_iso: str) -> list[dict[str, Any]]:
        conn = self.db._conn()
        rows = conn.execute(
            "SELECT * FROM schedules WHERE paused=0 AND next_tick IS NOT NULL AND next_tick<=?",
            (now_iso,),
        ).fetchall()
        return [dict(r) for r in rows]

    def schedules_list(self, session_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        conn = self.db._conn()
        if session_id:
            rows = conn.execute(
                "SELECT * FROM schedules WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM schedules ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(r) for r in rows]

    def schedule_get(self, schedule_id: int) -> dict[str, Any] | None:
        conn = self.db._conn()
        row = conn.execute("SELECT * FROM schedules WHERE id=?", (int(schedule_id),)).fetchone()
        return dict(row) if row else None

    def schedule_claim(self, schedule_id: int, claim_id: str) -> bool:
        """原子 claim：未 claim 且非 paused 才成功（Invariant 3：同一 tick 至多一个 Run）。"""
        with self.db._lock:
            conn = self.db._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "UPDATE schedules SET claim_id=?, updated_at=? WHERE id=? AND paused=0"
                    " AND (claim_id IS NULL OR claim_id='')",
                    (claim_id, self.db._utc(), schedule_id),
                )
                conn.commit()
                return cur.rowcount == 1
            except Exception:
                conn.rollback()
                raise

    def schedule_advance(self, schedule_id: int, next_tick: str | None) -> None:
        """派发成功后推进：next_tick 置新值（None=不再触发），清 claim。"""
        with self.db._lock:
            conn = self.db._conn()
            conn.execute(
                "UPDATE schedules SET next_tick=?, claim_id=NULL, updated_at=? WHERE id=?",
                (next_tick, self.db._utc(), schedule_id),
            )
            conn.commit()

    def schedule_release_claim(self, schedule_id: int) -> None:
        """派发失败回滚 claim（tick 保留，下一轮可重试；不重新生成 Run）。"""
        with self.db._lock:
            conn = self.db._conn()
            conn.execute("UPDATE schedules SET claim_id=NULL, updated_at=? WHERE id=?", (self.db._utc(), schedule_id))
            conn.commit()

    def schedule_cancel(self, schedule_id: int) -> None:
        with self.db._lock:
            conn = self.db._conn()
            conn.execute("UPDATE schedules SET paused=1, updated_at=? WHERE id=?", (self.db._utc(), schedule_id))
            conn.commit()

    # ---- harness（revision-based；Invariant 7/8） ----
    def harness_create(
        self,
        *,
        item_id: str,
        kind: str,
        title: str,
        content: str | None = None,
        content_ref: str | None = None,
        path: str = "general",
        scope: str = "local",
        provenance: str | None = None,
    ) -> dict[str, Any]:
        m.assert_harness_kind(kind)
        if scope not in ("local", "global"):
            raise ValueError("invalid scope: %s" % scope)
        now = self.db._utc()
        snapshot = {
            "id": item_id,
            "kind": kind,
            "title": title,
            "content": content,
            "content_ref": content_ref,
            "path": path,
            "scope": scope,
            "version": 1,
        }
        with self.db._lock:
            conn = self.db._conn()
            conn.execute(
                "INSERT INTO harness_items (id, kind, title, content, content_ref, path, scope,"
                " version, provenance, created_at, updated_at) VALUES (?,?,?,?,?,?,?,1,?,?,?)",
                (item_id, kind, title, content, content_ref, path, scope, provenance, now, now),
            )
            conn.execute(
                "INSERT INTO harness_revisions (item_id, revision, prev_revision, op, diff_json,"
                " snapshot_json, author, created_at) VALUES (?,1,NULL,'create','{}',?,?,?)",
                (item_id, _j(snapshot), provenance, now),
            )
            conn.commit()
        return self.harness_get(item_id)

    def harness_get(self, item_id: str) -> dict[str, Any] | None:
        conn = self.db._conn()
        row = conn.execute("SELECT * FROM harness_items WHERE id=?", (item_id,)).fetchone()
        return dict(row) if row else None

    def harness_list(self, *, kind: str | None = None, scope: str | None = None) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if kind:
            where.append("kind=?")
            params.append(kind)
        if scope:
            where.append("scope=?")
            params.append(scope)
        conn = self.db._conn()
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute("SELECT * FROM harness_items%s ORDER BY updated_at DESC" % clause, params).fetchall()
        return [dict(r) for r in rows]

    def harness_update(
        self,
        *,
        item_id: str,
        expected_version: int,
        content: str | None = None,
        title: str | None = None,
        path: str | None = None,
        content_ref: str | None = None,
        author: str | None = None,
    ) -> dict[str, Any]:
        """乐观并发（expected_version）：冲突抛 ValueError；每次修改产生 revision。"""
        with self.db._lock:
            conn = self.db._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM harness_items WHERE id=?", (item_id,)).fetchone()
                if row is None:
                    raise ValueError("harness item not found: %s" % item_id)
                old = dict(row)
                if int(old["version"]) != expected_version:
                    raise ValueError("version conflict: expected %s, got %s" % (expected_version, old["version"]))
                new_version = int(old["version"]) + 1
                new_content = old["content"] if content is None else content
                new_title = old["title"] if title is None else title
                new_path = old["path"] if path is None else path
                new_ref = old["content_ref"] if content_ref is None else content_ref
                conn.execute(
                    "UPDATE harness_items SET content=?, title=?, path=?, content_ref=?, version=?,"
                    " updated_at=? WHERE id=?",
                    (new_content, new_title, new_path, new_ref, new_version, self.db._utc(), item_id),
                )
                diff = {"before": old.get("content"), "after": new_content}
                conn.execute(
                    "INSERT INTO harness_revisions (item_id, revision, prev_revision, op, diff_json,"
                    " snapshot_json, author, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (item_id, new_version, old["version"], "update", _j(diff), _j(old), author, self.db._utc()),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.harness_get(item_id)

    def harness_delete(self, *, item_id: str, expected_version: int, author: str | None = None) -> None:
        with self.db._lock:
            conn = self.db._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM harness_items WHERE id=?", (item_id,)).fetchone()
                if row is None:
                    raise ValueError("harness item not found: %s" % item_id)
                old = dict(row)
                if int(old["version"]) != expected_version:
                    raise ValueError("version conflict: expected %s, got %s" % (expected_version, old["version"]))
                conn.execute(
                    "INSERT INTO harness_revisions (item_id, revision, prev_revision, op, diff_json,"
                    " snapshot_json, author, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (item_id, int(old["version"]) + 1, int(old["version"]), "delete", "{}", _j(old), author, self.db._utc()),
                )
                conn.execute("DELETE FROM harness_items WHERE id=?", (item_id,))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def harness_rollback(self, item_id: str, to_revision: int, author: str | None = None) -> dict[str, Any]:
        """回滚到某 revision 记录的快照（该 change 之前的状态）；历史 revision 永不删除。"""
        with self.db._lock:
            conn = self.db._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                target = conn.execute(
                    "SELECT * FROM harness_revisions WHERE item_id=? AND revision=? ORDER BY id DESC LIMIT 1",
                    (item_id, to_revision),
                ).fetchone()
                if target is None:
                    raise ValueError("revision not found: %s rev %s" % (item_id, to_revision))
                snapshot = json.loads(target["snapshot_json"] or "{}")
                current = conn.execute("SELECT * FROM harness_items WHERE id=?", (item_id,)).fetchone()
                cur_version = int(current["version"]) if current else 0
                new_version = cur_version + 1
                now = self.db._utc()
                if current is None:
                    conn.execute(
                        "INSERT INTO harness_items (id, kind, title, content, content_ref, path, scope,"
                        " version, provenance, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            snapshot.get("id", item_id),
                            snapshot.get("kind", "memory"),
                            snapshot.get("title", item_id),
                            snapshot.get("content"),
                            snapshot.get("content_ref"),
                            snapshot.get("path", "general"),
                            snapshot.get("scope", "local"),
                            new_version,
                            snapshot.get("provenance"),
                            now,
                            now,
                        ),
                    )
                else:
                    conn.execute(
                        "UPDATE harness_items SET kind=?, title=?, content=?, content_ref=?, path=?,"
                        " scope=?, version=?, updated_at=? WHERE id=?",
                        (
                            snapshot.get("kind", current["kind"]),
                            snapshot.get("title", current["title"]),
                            snapshot.get("content"),
                            snapshot.get("content_ref"),
                            snapshot.get("path", "general"),
                            snapshot.get("scope", "local"),
                            new_version,
                            now,
                            item_id,
                        ),
                    )
                conn.execute(
                    "INSERT INTO harness_revisions (item_id, revision, prev_revision, op, diff_json,"
                    " snapshot_json, author, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        item_id,
                        new_version,
                        cur_version if current else None,
                        "rollback",
                        "{}",
                        _j(dict(current)) if current else "{}",
                        author,
                        now,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.harness_get(item_id)

    def harness_revisions(self, item_id: str) -> list[dict[str, Any]]:
        conn = self.db._conn()
        rows = conn.execute(
            "SELECT id, item_id, revision, prev_revision, op, diff_json, author, created_at"
            " FROM harness_revisions WHERE item_id=? ORDER BY revision ASC",
            (item_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- refinement events（cooldown / same-failure suppression） ----
    def refinement_record(
        self,
        *,
        trigger: str,
        session_id: str,
        run_id: str | None = None,
        fingerprint: str | None = None,
        changes: list[dict[str, Any]] | None = None,
        evidence: str | None = None,
        cooldown_until: str | None = None,
    ) -> int:
        changes_json = _j(changes or [])
        with self.db._lock:
            conn = self.db._conn()
            cur = conn.execute(
                "INSERT INTO refinement_events (trigger, session_id, run_id, fingerprint, changes_json,"
                " evidence, cooldown_until, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (trigger, session_id, run_id, fingerprint, changes_json, evidence, cooldown_until, self.db._utc()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def refinement_last(self, fingerprint: str) -> dict[str, Any] | None:
        conn = self.db._conn()
        row = conn.execute(
            "SELECT * FROM refinement_events WHERE fingerprint=? ORDER BY id DESC LIMIT 1",
            (fingerprint,),
        ).fetchone()
        return dict(row) if row else None

    def refinement_cooldown_active(self, fingerprint: str, now_iso: str) -> bool:
        conn = self.db._conn()
        row = conn.execute(
            "SELECT MAX(cooldown_until) c FROM refinement_events WHERE fingerprint=?",
            (fingerprint,),
        ).fetchone()
        return bool(row and row["c"] and str(row["c"]) > now_iso)

    # ---- tasks（MCP Task 扩展 handle ↔ Run 映射；Task ≠ Run） ----
    def task_upsert(
        self,
        *,
        task_id: str,
        run_id: str,
        agent_id: int | None = None,
        host_handle: str | None = None,
        status: str = "working",
        expires_at: str | None = None,
    ) -> None:
        now = self.db._utc()
        with self.db._lock:
            conn = self.db._conn()
            conn.execute(
                "INSERT INTO tasks (task_id, run_id, agent_id, host_handle, status, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(task_id) DO UPDATE SET run_id=excluded.run_id,"
                " agent_id=excluded.agent_id, host_handle=excluded.host_handle,"
                " status=excluded.status, expires_at=excluded.expires_at",
                (task_id, run_id, agent_id, host_handle, status, now, expires_at),
            )
            conn.commit()

    def task_get(self, task_id: str) -> dict[str, Any] | None:
        conn = self.db._conn()
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def task_by_run(self, run_id: str) -> dict[str, Any] | None:
        conn = self.db._conn()
        row = conn.execute(
            "SELECT * FROM tasks WHERE run_id=? ORDER BY created_at DESC LIMIT 1", (run_id,)
        ).fetchone()
        return dict(row) if row else None

