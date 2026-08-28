"""v4 Control Plane Protocol（roadmap §6）路由级测试：/api/v4/command 信封、journal
幂等（new/replay/conflict/uncertain）、ack 水位、/api/protocol 协商、代际事件游标。

遵循仓库既有约定：真实 DB + 真实 Dispatcher + 与 HTTP 处理器同源的模块函数
（handle_v4_command / handle_v4_ack / protocol_payload），不起 socket。
"""

from __future__ import annotations

import pytest

from agent_mcp import models as m
from agent_mcp.daemon_http import (
    EventBroadcaster,
    handle_v4_ack,
    handle_v4_command,
    parse_event_cursor,
    protocol_payload,
)
from agent_mcp.daemon_main import Dispatcher
from agent_mcp.db import DB
from agent_mcp.store_v4 import StoreV4


class NoopWorker:
    def __init__(self):
        self.spawned = []

    def __call__(self, target_cli, **kwargs):
        self.spawned.append((target_cli, kwargs))
        return {"worker_pid": 0, "command_summary": "noop",
                "state_path": "", "out_path": "", "err_path": ""}


class BoomDispatcher(Dispatcher):
    """spawn 抛异常：模拟执行中途不确定。"""

    def spawn(self, body):  # noqa: D102
        raise RuntimeError("boom mid-execution")


class FakeServer:
    def __init__(self, dispatcher, store_v4, generation=0, nonce=""):
        self.dispatcher = dispatcher
        self.store_v4 = store_v4
        self.generation = generation
        self.generation_nonce = nonce


@pytest.fixture()
def env(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db = DB(tmp_path / "test.sqlite3")
    worker = NoopWorker()
    dispatcher = Dispatcher(db=db, broadcaster=EventBroadcaster(), state_dir=state_dir,
                            spawn_fn=worker)
    store = StoreV4(db)
    server = FakeServer(dispatcher, store, generation=7, nonce="abcdef")
    return {"db": db, "worker": worker, "dispatcher": dispatcher,
            "store": store, "server": server}


PARAMS = {"target_cli": "omp", "prompt": "hi", "cwd": "/tmp", "session_id": "s1"}


def _cmd(env, **overrides):
    body = {
        "protocol": "v4",
        "client_id": "client-1",
        "command_id": "cmd-1",
        "request_hash": m.request_hash("spawn", PARAMS),
        "method": "spawn",
        "params": dict(PARAMS),
    }
    body.update(overrides)
    return handle_v4_command(env["server"], body)


# ---------- 能力协商 ----------


def test_protocol_payload_declares_v4_capabilities(env):
    payload = protocol_payload(env["server"])
    assert payload["version"] == 4
    assert payload["protocol"] == "v4"
    assert "command_id_conflict" in payload["capabilities"]
    assert "generation_event_cursor" in payload["capabilities"]
    assert payload["adapters"]["control-plane"]["journal"] is True
    assert payload["endpoints"]["command"] == "/api/v4/command"


# ---------- 信封：new → replay → conflict ----------


def test_v4_command_new_then_replay_then_conflict(env):
    status, result = _cmd(env)
    assert status == 200
    assert result["_idempotency"] == "new"
    assert len(env["worker"].spawned) == 1

    # 同 request_hash 重放：返回已记录结果，绝不重复执行
    status, result = _cmd(env)
    assert status == 200
    assert result["_idempotency"] == "replay"
    assert len(env["worker"].spawned) == 1  # 不重复执行（Invariant 1）

    # 同 command_id 不同 hash：command_id_conflict，绝不执行（Invariant 2）
    other = m.request_hash("spawn", {"target_cli": "omp", "prompt": "bye", "cwd": "/tmp"})
    status, result = _cmd(env, request_hash=other)
    assert status == 409
    assert result["error"] == "command_id_conflict"
    assert len(env["worker"].spawned) == 1


def test_v4_command_validation(env):
    status, _ = _cmd(env, protocol="v3")
    assert status == 400
    status, _ = _cmd(env, command_id="")
    assert status == 400
    status, _ = _cmd(env, method="definitely_not_a_method")
    assert status == 400
    assert status == 400
    # 未知方法也是确定性的：journal 落库错误结果，重放同样返回 400 不重复派发
    status, _ = _cmd(env, method="definitely_not_a_method")
    assert status == 400
    assert len(env["worker"].spawned) == 0


# ---------- uncertain：不盲目重放 ----------


def test_v4_command_uncertain_marked_and_not_replayed(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db = DB(tmp_path / "u.sqlite3")
    dispatcher = BoomDispatcher(db=db, broadcaster=EventBroadcaster(), state_dir=state_dir)
    store = StoreV4(db)
    server = FakeServer(dispatcher, store)

    body = {
        "protocol": "v4",
        "client_id": "c",
        "command_id": "boom",
        "request_hash": m.request_hash("spawn", {"a": 1}),
        "method": "spawn",
        "params": {"a": 1},
    }
    status, result = handle_v4_command(server, body)
    assert status == 500
    assert result["state"] == "uncertain"
    entry = store.journal_get("c", "boom")
    assert entry["state"] == "uncertain"

    # 重放：返回 409 不确定，而非盲目重放
    status, result = handle_v4_command(server, body)
    assert status == 409
    assert result["state"] == "uncertain"


# ---------- ack 水位 / compaction ----------


def test_v4_ack_and_watermark(env):
    s, r = _cmd(env, command_id="one")
    assert s == 200
    s, r = _cmd(env, command_id="two")
    assert s == 200
    s, r = handle_v4_ack(env["server"], {"client_id": "client-1", "up_to_command_id": "one"})
    assert s == 200
    assert r["acked"] == 1
    cands = env["store"].journal_compaction_candidates("client-1")
    assert len(cands) == 1
    env["store"].journal_compact("client-1", cands[0])
    assert env["store"].journal_get("client-1", "one") is None
    assert env["store"].journal_get("client-1", "two") is not None  # 未确认不删

    s, r = handle_v4_ack(env["server"], {"client_id": "client-1", "up_to_command_id": "nope"})
    assert s == 200 and r["acked"] == 0


# ---------- 代际事件游标（Invariant 9 的协议面） ----------


def test_event_cursor_generation_parsing():
    assert parse_event_cursor("7:42") == (7, 42)
    assert parse_event_cursor("42") == (None, 42)
    assert parse_event_cursor("bogus") == (None, 0)
    assert parse_event_cursor("") == (None, 0)


def test_sse_frame_generation_id():
    from agent_mcp.daemon_http import Handler
    frame = Handler._frame({"type": "x", "payload": {}}, 42, message_mode=False, generation=7)
    assert frame.split("\n")[0] == "id: 7:42"
    legacy = Handler._frame({"type": "x", "payload": {}}, 42, message_mode=False, generation=0)
    assert legacy.split("\n")[0] == "id: 42"


def test_strip_replayed_handles_generation_tokens():
    from agent_mcp.daemon_http import Handler
    chunk = "id: 7:42\nevent: x\ndata: {}\n\nid: 7:43\nevent: y\ndata: {}\n\n"
    stripped = Handler._strip_replayed(chunk, {42})
    assert "id: 7:42" not in stripped
    assert "id: 7:43" in stripped

