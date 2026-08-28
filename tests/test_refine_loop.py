"""P3 DoD：refine 三段式管道（reviewer 只产 proposal；同指纹 cooldown；base prompt 守卫）。"""

from __future__ import annotations

import pytest

from agent_mcp import models as m
from agent_mcp.db import DB
from agent_mcp.daemon_http import EventBroadcaster, _API_METHODS
from agent_mcp.daemon_main import Dispatcher
from agent_mcp.refine import apply_proposal, commit_proposal, run_preview, validate_proposal
from agent_mcp.store_v4 import StoreV4


@pytest.fixture()
def env(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db = DB(tmp_path / "test.sqlite3")
    dispatcher = Dispatcher(db=db, broadcaster=EventBroadcaster(), state_dir=state_dir,
                            store_v4=StoreV4(db))
    return {"db": db, "dispatcher": dispatcher}


def call(env, path, body):
    try:
        return 200, getattr(env["dispatcher"], _API_METHODS[path])(body)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return 500, {"error": str(exc)}


def ops_create():
    return [{"op": "create", "item": {"id": "m1", "kind": "memory", "title": "lesson",
                                      "content": "v1", "scope": "local"}}]


# ---------- 纯管道 ----------


def test_validate_proposal_rejects_invalid():
    with pytest.raises(ValueError):
        validate_proposal([{"op": "rename", "item": {"id": "x"}}])
    with pytest.raises(ValueError):
        validate_proposal([{"op": "create", "item": {"id": "x", "kind": "hack"}}])
    with pytest.raises(ValueError):
        validate_proposal("not-a-list")
    assert validate_proposal(ops_create())[0]["op"] == "create"


def test_run_preview_reviewer_never_writes(tmp_path):
    db = DB(tmp_path / "r.db")
    store = StoreV4(db)
    seen = {"calls": 0}

    def reviewer(trajectory, items):
        seen["calls"] += 1
        return {"ops": ops_create()}

    preview = run_preview(trajectory=[], harness_items=[], reviewer=reviewer, session_id="s1")
    assert preview["proposal_count"] == 1
    assert seen["calls"] == 1
    assert store.harness_get("m1") is None  # preview 不写任何东西


def test_commit_proposal_applies_and_cooldown(tmp_path):
    db = DB(tmp_path / "c.db")
    store = StoreV4(db)
    fp = m.failure_fingerprint("s1", "err")
    r1 = commit_proposal(store, ops_create(), session_id="s1", fingerprint=fp)
    assert r1["count"] == 1
    assert store.harness_get("m1")["content"] == "v1"
    # 同指纹 cooldown 内再提交 → 拒绝（same-failure suppression）
    with pytest.raises(ValueError, match="cooldown"):
        commit_proposal(store, ops_create(), session_id="s1", fingerprint=fp)
    # 不同指纹可提交另一条目
    r2 = commit_proposal(store, [{"op": "create", "item": {"id": "m2", "kind": "memory",
                                                           "title": "t", "content": "c"}}],
                         session_id="s1", fingerprint="other-fp")
    assert r2["count"] == 1


def test_apply_proposal_guards_base_prompt(tmp_path):
    db = DB(tmp_path / "b.db")
    store = StoreV4(db)
    store.harness_create(item_id=m.BASE_PROMPT_ID, kind="prompt", title="base", content="x")
    with pytest.raises(ValueError, match="immutable"):
        apply_proposal(store, [{"op": "update", "target": m.BASE_PROMPT_ID,
                                "item": {"id": m.BASE_PROMPT_ID, "expected_version": 1,
                                         "content": "rewritten"}}], session_id="s1")


# ---------- Dispatcher 路由级 ----------


def test_refine_preview_explicit_ops(env):
    status, res = call(env, "/api/refine/preview", {"reviewer_ops": ops_create()})
    assert status == 200 and res["proposal_count"] == 1 and res["reviewer"] == "explicit"


def test_refine_preview_injected_reviewer(env):
    def reviewer(trajectory, items):
        return ops_create()
    env["dispatcher"]._refine_reviewer = reviewer
    status, res = call(env, "/api/refine/preview", {"session_id": "s1"})
    assert status == 200 and res["proposal_count"] == 1


def test_refine_preview_without_backend_rejected(env):
    status, res = call(env, "/api/refine/preview", {"session_id": "s1"})
    assert status == 400 and "reviewer" in res["error"]


def test_refine_commit_rollback_route(env):
    status, res = call(env, "/api/refine/commit", {"ops": ops_create(), "session_id": "s1"})
    assert status == 200 and res["count"] == 1
    item = env["db"]
    from agent_mcp.store_v4 import StoreV4
    store = StoreV4(env["db"]) if not hasattr(env["dispatcher"], "store_v4") else env["dispatcher"].store_v4
    assert store.harness_get("m1")["content"] == "v1"
    status, res = call(env, "/api/refine/rollback", {"item_id": "m1", "to_revision": 1})
    assert status == 200

