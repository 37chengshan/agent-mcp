"""P3 DoD：harness 工具面（route 级：真实 DB + Dispatcher + _API_METHODS 反射）。"""

from __future__ import annotations

import json

import pytest

from agent_mcp.daemon_http import EventBroadcaster, _API_METHODS
from agent_mcp.daemon_main import Dispatcher
from agent_mcp.db import DB
from agent_mcp.store_v4 import StoreV4


class NoopWorker:
    def __call__(self, target_cli, **kwargs):
        return {"worker_pid": 0, "command_summary": "noop",
                "state_path": "", "out_path": "", "err_path": ""}


@pytest.fixture()
def env(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db = DB(tmp_path / "test.sqlite3")
    dispatcher = Dispatcher(db=db, broadcaster=EventBroadcaster(), state_dir=state_dir,
                            spawn_fn=NoopWorker(), store_v4=StoreV4(db))
    return {"db": db, "dispatcher": dispatcher}


def call(env, path, body):
    method = _API_METHODS[path]
    try:
        return 200, getattr(env["dispatcher"], method)(body)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return 500, {"error": str(exc)}


def test_harness_crud_route(env):
    status, res = call(env, "/api/harness/add",
                       {"item_id": "h1", "kind": "memory", "title": "lesson", "content": "v1"})
    assert status == 200 and res["item"]["version"] == 1

    status, res = call(env, "/api/harness/update",
                       {"item_id": "h1", "expected_version": 1, "content": "v2"})
    assert status == 200 and res["item"]["version"] == 2

    status, res = call(env, "/api/harness/list", {"kind": "memory", "scope": "local"})
    assert status == 200 and len(res["items"]) == 1

    status, res = call(env, "/api/harness/update",
                       {"item_id": "h1", "expected_version": 1, "content": "stale"})
    assert status == 400 and "version conflict" in res["error"]

    status, res = call(env, "/api/harness/rollback", {"item_id": "h1", "to_revision": 2})
    assert status == 200 and res["item"]["content"] == "v1"

    status, res = call(env, "/api/harness/delete", {"item_id": "h1", "expected_version": 3})
    assert status == 200 and res["deleted"] is True

    status, res = call(env, "/api/harness/list", {})
    assert res["items"] == []


def test_harness_global_requires_authorization(env):
    status, res = call(env, "/api/harness/add",
                       {"item_id": "g1", "kind": "skill", "title": "s", "scope": "global"})
    assert status == 400 and "authorization" in res["error"]
    status, res = call(env, "/api/harness/add",
                       {"item_id": "g1", "kind": "skill", "title": "s", "scope": "global",
                        "authorized_global": True})
    assert status == 200


def test_harness_requires_store(env):
    # 未接 store_v4 的 Dispatcher → 结构化 400
    from agent_mcp.daemon_http import EventBroadcaster
    from agent_mcp.db import DB
    import pathlib
    st = pathlib.Path(env["db"].path).parent / "s2"
    st.mkdir(exist_ok=True)
    db2 = DB(st / "d2.db") if False else env["db"]
    plain = Dispatcher(db=db2, broadcaster=EventBroadcaster(), state_dir=st)
    status, res = call({"dispatcher": plain}, "/api/harness/list", {})
    assert status == 400 and "store not ready" in res["error"]

