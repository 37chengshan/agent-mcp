"""v4 系统 Invariants（roadmap §13）——模型层 + StoreV4 持久层。

P0 阶段落地不依赖 daemon 路由的判定；P1/P2 接线后补充路由级用例。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent_mcp import models as m
from agent_mcp.db import DB
from agent_mcp.store_v4 import StoreV4


@pytest.fixture()
def store(tmp_path):
    db = DB(tmp_path / "t.db")
    return StoreV4(db)


def _now_iso(offset_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


# ---------- Run 状态机（§4 / §13 基础） ----------


def test_run_state_machine_enforces_transitions():
    assert m.run_transition(m.RUN_PENDING, m.RUN_ADMITTED) == m.RUN_ADMITTED
    assert m.run_transition(m.RUN_ADMITTED, m.RUN_RUNNING) == m.RUN_RUNNING
    assert m.run_transition(m.RUN_RUNNING, m.RUN_WAITING) == m.RUN_WAITING
    with pytest.raises(ValueError):
        m.run_transition(m.RUN_PENDING, m.RUN_COMPLETED)  # 非法跳转
    with pytest.raises(ValueError):
        m.run_transition(m.RUN_COMPLETED, m.RUN_RUNNING)  # 终端态不可再转移


def test_store_run_transition_persists_and_immutable_after_terminal(store):
    run = store.run_create(run_kind="spawn", origin_command_id="c1")
    assert run["status"] == m.RUN_PENDING
    store.run_transition(run["id"], m.RUN_ADMITTED)
    store.run_transition(run["id"], m.RUN_RUNNING)
    done = store.run_transition(run["id"], m.RUN_COMPLETED)
    assert store.run_get(run["id"])["status"] == m.RUN_COMPLETED
    with pytest.raises(ValueError):
        store.run_transition(run["id"], m.RUN_RUNNING)
    assert done["completed_at"] is not None


def test_run_to_agent_status_projection():
    assert m.run_to_agent_status(m.RUN_PENDING) == "queued"
    assert m.run_to_agent_status(m.RUN_RUNNING) == "running"
    assert m.run_to_agent_status(m.RUN_WAITING) == "needs_advisor"
    assert m.run_to_agent_status(m.RUN_COMPLETED) == "terminated"
    assert m.run_to_agent_status(m.RUN_INCOMPLETE) == "incomplete"


# ---------- I1/I2：命令幂等 / conflict ----------


def test_invariant_1_same_command_executes_once(store):
    client, cmd, params = "c1", "cmd-1", {"a": 1}
    h = m.request_hash("spawn", params)
    first = store.journal_record(client_id=client, command_id=cmd, request_hash=h, method="spawn", params=params)
    assert first["state"] == "recorded"
    store.journal_complete(client_id=client, command_id=cmd, result={"ok": True})
    existing = store.journal_get(client, cmd)
    assert m.journal_classify(existing, h) == m.JOURNAL_REPLAY
    assert json.loads(existing["result_json"]) == {"ok": True}


def test_invariant_2_conflicting_hash_never_executes(store):
    client, cmd = "c2", "cmd-2"
    h1 = m.request_hash("spawn", {"a": 1})
    store.journal_record(client_id=client, command_id=cmd, request_hash=h1, method="spawn", params={"a": 1})
    h2 = m.request_hash("spawn", {"a": 2})
    existing = store.journal_get(client, cmd)
    assert m.journal_classify(existing, h2) == m.JOURNAL_CONFLICT
    assert existing["request_hash"] == h1  # 原请求哈希未被覆盖


def test_journal_compaction_respects_ack_watermark(store):
    h1 = m.request_hash("spawn", {"a": 1})
    store.journal_record(client_id="c", command_id="one", request_hash=h1, method="spawn", params={"a": 1})
    store.journal_complete(client_id="c", command_id="one", result={"ok": 1})
    h2 = m.request_hash("spawn", {"a": 2})
    store.journal_record(client_id="c", command_id="two", request_hash=h2, method="spawn", params={"a": 2})
    assert store.journal_compaction_candidates("c") == []  # 未 ack → 不可压缩
    assert store.journal_ack("c", "one") == 1
    cands = store.journal_compaction_candidates("c")
    assert cands == [1]
    store.journal_compact("c", cands[0])
    assert store.journal_get("c", "one") is None
    assert store.journal_get("c", "two") is not None  # 未确认条目永不删除


# ---------- I3：同一 schedule tick 至多一个 Run ----------


def test_invariant_3_schedule_tick_single_claim(store):
    sid = store.schedule_create(session_id="s1", prompt="tick", kind="one_shot", next_tick=_now_iso(-1))
    assert store.schedule_claim(sid, "claim-1") is True
    assert store.schedule_claim(sid, "claim-2") is False  # 第二次 claim 失败 → 不产生第二个 Run


def test_schedule_advance_and_release(store):
    sid = store.schedule_create(session_id="s1", prompt="tick", kind="cron", interval_expr="5m", next_tick=_now_iso(-1))
    assert store.schedule_claim(sid, "claim-1") is True
    store.schedule_advance(sid, _now_iso(300))
    assert store.schedule_claim(sid, "claim-2") is True  # 推进后下一 tick 可 claim
    store.schedule_release_claim(sid)
    assert store.schedule_claim(sid, "claim-3") is True


# ---------- I4：Goal completed 不再 continuation ----------


def test_invariant_4_goal_completed_stops_continuation(store):
    goal_id = store.goal_create(session_id="s1", objective="ship it", token_budget=5)
    store.goal_add_tokens(goal_id, 3)
    active = store.goal_get(goal_id)
    assert m.goal_should_continue(active, rounds=1, tokens_used=3, elapsed_seconds=10) is True
    store.goal_update_status(goal_id, m.GOAL_COMPLETED)
    done = store.goal_get(goal_id)
    assert m.goal_should_continue(done, rounds=1, tokens_used=3, elapsed_seconds=10) is False
    # 预算耗尽也停
    store2 = store
    g2 = store2.goal_create(session_id="s1", objective="budgeted", token_budget=2)
    store2.goal_add_tokens(g2, 2)
    over = store2.goal_get(g2)
    assert m.goal_should_continue(over, rounds=0, tokens_used=2, elapsed_seconds=0) is False


# ---------- I5：Autonomous 永不突破任一预算 ----------


def test_invariant_5_autonomous_budget_min():
    policy = m.AutonomousPolicy(max_continuations=3, max_turns=10, max_tokens=1000, max_seconds=120)
    assert policy.can_continue(continuations=2, turns=9, tokens=999, elapsed_seconds=119)
    assert not policy.can_continue(continuations=3, turns=0, tokens=0, elapsed_seconds=0)  # continuation 到顶
    assert not policy.can_continue(continuations=0, turns=10, tokens=0, elapsed_seconds=0)  # turns 到顶
    assert not policy.can_continue(continuations=0, turns=0, tokens=1000, elapsed_seconds=0)  # tokens 到顶
    assert not policy.can_continue(continuations=0, turns=0, tokens=0, elapsed_seconds=120)  # 墙钟到顶
    # min() 生效：任一维度先到顶即停
    assert not policy.can_continue(continuations=3, turns=100, tokens=99999, elapsed_seconds=99999)
    remaining = policy.remaining(continuations=1, turns=2, tokens=500, elapsed_seconds=60)
    assert remaining["continuations"] == 2
    assert remaining["turns"] == 8


def test_invariant_5_loop_never_exceeds():
    policy = m.AutonomousPolicy(max_continuations=2, max_turns=3, max_tokens=100, max_seconds=30)
    spent = {"continuations": 0, "turns": 0, "tokens": 0, "elapsed_seconds": 0}
    steps = 0
    for _ in range(100):
        if not policy.can_continue(**spent):
            break
        spent["continuations"] += 1
        spent["turns"] += 1
        spent["tokens"] += 30
        spent["elapsed_seconds"] += 10
        steps += 1
    assert steps == 2  # continuation 预算最先到顶（min 生效），恰执行 2 步
    assert spent["continuations"] <= 2
    assert spent["turns"] <= 3
    assert spent["tokens"] <= 100
    assert spent["elapsed_seconds"] <= 30


# ---------- I6：Adapter 不得调用未声明 capability ----------


def test_invariant_6_adapter_capability_guard():
    cap = m.AdapterCapability({"spawn": m.CAP_SUPPORTED, "resume": m.CAP_DEGRADED})
    cap.assert_supported("spawn")
    cap.assert_supported("resume")  # DEGRADED 允许调用，但调用方须按降级语义处理
    with pytest.raises(PermissionError):
        cap.assert_supported("goal")  # 未声明（UNSUPPORTED）→ 拒绝


# ---------- I7：base system prompt 永不可被 refine 修改 ----------


def test_invariant_7_base_prompt_immutable():
    with pytest.raises(ValueError):
        m.assert_refine_target_allowed({"id": m.BASE_PROMPT_ID, "kind": "prompt"}, op="update")
    with pytest.raises(ValueError):
        m.assert_refine_target_allowed({"id": m.BASE_PROMPT_ID, "kind": "prompt"}, op="delete")
    # 普通 harness 条目可编辑；非法 kind / op 一律拒绝
    m.assert_refine_target_allowed({"id": "m1", "kind": "memory"}, op="update")
    with pytest.raises(ValueError):
        m.assert_refine_target_allowed({"id": "m2", "kind": "hack"}, op="update")
    with pytest.raises(ValueError):
        m.assert_refine_target_allowed(None, op="rename")


# ---------- I8：Harness rollback 不丢历史 revision ----------


def test_invariant_8_harness_rollback_preserves_revisions(store):
    store.harness_create(item_id="h1", kind="memory", title="t", content="v1", scope="local")
    store.harness_update(item_id="h1", expected_version=1, content="v2")
    store.harness_update(item_id="h1", expected_version=2, content="v3")
    revs_before = len(store.harness_revisions("h1"))  # create + 2 updates = 3
    store.harness_rollback("h1", to_revision=2)
    item = store.harness_get("h1")
    assert item["content"] == "v1"
    assert len(store.harness_revisions("h1")) == revs_before + 1  # rollback 追加 revision，历史不丢
    store.harness_rollback("h1", to_revision=1)
    assert store.harness_get("h1")["content"] == "v1"


# ---------- I9：daemon 重启不产生 generation collision ----------


def test_invariant_9_generation_no_collision(tmp_path):
    db = DB(tmp_path / "g.db")
    g1, n1 = StoreV4(db).next_generation()
    g2, n2 = StoreV4(db).next_generation()  # 模拟重启（同库新实例）
    assert g2 == g1 + 1
    assert (g1, n1) != (g2, n2)


# ---------- I10：MCP protocol session 与应用状态解耦 ----------


def test_invariant_10_application_state_decoupled_from_protocol_session(store):
    # journal 以 application 身份（client_id/command_id）为键；协议层 session 不影响幂等
    h = m.request_hash("spawn", {"a": 1})
    store.journal_record(client_id="app-client", command_id="c", request_hash=h, method="spawn", params={"a": 1})
    existing = store.journal_get("app-client", "c")
    # 即使宿主以另一个 "Mcp-Session-Id" 重放，也判定为 replay（应用状态独立）
    assert m.journal_classify(existing, h) == m.JOURNAL_REPLAY


# ---------- IntervalSpec（schedule 依赖的纯规则） ----------


def test_interval_spec_simple_and_cron():
    base = datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc)
    assert m.IntervalSpec.next_after("30m", base) == (base + timedelta(minutes=30)).isoformat()
    assert m.IntervalSpec.next_after("1h", base) == (base + timedelta(hours=1)).isoformat()
    assert m.IntervalSpec.next_after("0 9 * * 1-5", base) is not None
    assert m.IntervalSpec.next_after("*/15 * * * *", base) == (base + timedelta(minutes=15)).isoformat()
    assert m.IntervalSpec.parse("bogus") is None
    assert m.IntervalSpec.next_after("", base) is None

