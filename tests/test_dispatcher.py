import json
from pathlib import Path

from agent_mcp.daemon_http import EventBroadcaster
from agent_mcp.daemon_main import Dispatcher
from agent_mcp.db import DB


def _make(tmp_path, *, max_concurrent=4, spawn_fn=None):
    db = DB(tmp_path / "test.db")
    bc = EventBroadcaster(max_clients=4)
    d = Dispatcher(db=db, broadcaster=bc, state_dir=tmp_path,
                   max_concurrent=max_concurrent, spawn_fn=spawn_fn,
                   monitor_interval=0.05)
    return d, db, bc


def _fake_spawn(tmp_path):
    """记录调用并返回假 worker 文件（不真正起进程）。"""
    calls = []
    def fake_spawn(target_cli, *, prompt, cwd, permission_mode="plan", model=None,
                   max_turns=8, resume=None, state_dir):
        tag = f"{target_cli}-{len(calls)}"
        state_path = tmp_path / f"{tag}.json"
        state_path.write_text(json.dumps({"status": "starting", "cwd": str(cwd)}))
        out_path = tmp_path / f"{tag}.out.log"
        out_path.write_text("fake output line\n")
        (tmp_path / f"{tag}.err.log").write_text("")
        calls.append({"target_cli": target_cli, "prompt": prompt, "cwd": str(cwd)})
        return {"worker_pid": 900000 + len(calls),
                "command_summary": f"{target_cli} {prompt}",
                "state_path": str(state_path), "out_path": str(out_path),
                "err_path": str(tmp_path / f"{tag}.err.log")}
    return fake_spawn, calls


def _finish(state_path: Path, rc: int = 0):
    st = json.loads(Path(state_path).read_text())
    st.update({"status": "finished", "process_status": rc})
    Path(state_path).write_text(json.dumps(st))


def _listen(bc):
    """先 connect（缓冲只给已连接客户端追加），再由调用方触发事件。"""
    return bc.connect()


def test_spawn_creates_agent_and_broadcasts(tmp_path):
    fake, calls = _fake_spawn(tmp_path)
    d, db, bc = _make(tmp_path, spawn_fn=fake)
    listener = _listen(bc)
    d.start()
    try:
        res = d.spawn({"target_cli": "claude", "prompt": "do it", "cwd": str(tmp_path),
                       "session_id": "s1", "task_name": "t1", "permission_mode": "plan"})
        assert res["status"] == "running" and res["agent_id"] == 1
        agent = db.get_agent(1)
        assert agent["cli"] == "claude" and agent["cwd"] == str(tmp_path)
        assert agent["status"] == "running" and agent["pid"] is not None
        assert calls == [{"target_cli": "claude", "prompt": "do it", "cwd": str(tmp_path)}]
        text = "".join(listener["buffer"])
        assert "agent.spawned" in text and "agent.running" in text
    finally:
        d.stop()


def test_spawn_queued_when_slots_full_then_promoted(tmp_path):
    fake, calls = _fake_spawn(tmp_path)
    d, db, bc = _make(tmp_path, max_concurrent=1, spawn_fn=fake)
    d.start()
    try:
        a = d.spawn({"target_cli": "claude", "prompt": "A", "cwd": str(tmp_path)})
        b = d.spawn({"target_cli": "claude", "prompt": "B", "cwd": str(tmp_path)})
        assert a["status"] == "running" and b["status"] == "queued"
        assert len(calls) == 1
        # A 完成后，B 应被补位 spawn
        _finish(tmp_path / "claude-0.json", rc=0)
        import time
        for _ in range(100):
            if len(calls) >= 2:
                break
            time.sleep(0.05)
        assert len(calls) == 2 and calls[1]["prompt"] == "B"
        assert db.get_agent(b["agent_id"])["status"] == "running"
    finally:
        d.stop()


def test_wait_returns_terminated_with_summary(tmp_path):
    fake, _ = _fake_spawn(tmp_path)
    d, db, bc = _make(tmp_path, spawn_fn=fake)
    d.start()
    try:
        a = d.spawn({"target_cli": "claude", "prompt": "X", "cwd": str(tmp_path)})
        _finish(tmp_path / "claude-0.json", rc=0)
        res = d.wait({"agent_id": a["agent_id"], "timeout": 10})
        assert res["status"] == "terminated" and res["stop_reason"] == "end_turn"
        assert "fake output" in res["summary"]
        assert db.get_agent(a["agent_id"])["status"] == "terminated"
    finally:
        d.stop()


def test_wait_nonzero_rc_marks_error(tmp_path):
    fake, _ = _fake_spawn(tmp_path)
    d, db, _ = _make(tmp_path, spawn_fn=fake)
    d.start()
    try:
        a = d.spawn({"target_cli": "claude", "prompt": "X", "cwd": str(tmp_path)})
        _finish(tmp_path / "claude-0.json", rc=2)
        res = d.wait({"agent_id": a["agent_id"], "timeout": 10})
        assert res["status"] == "error" and res["stop_reason"] == "cli_exit_nonzero"
    finally:
        d.stop()


def test_wait_timeout_returns_running(tmp_path):
    fake, _ = _fake_spawn(tmp_path)
    d, db, _ = _make(tmp_path, spawn_fn=fake)
    d.start()
    try:
        a = d.spawn({"target_cli": "claude", "prompt": "X", "cwd": str(tmp_path)})
        res = d.wait({"agent_id": a["agent_id"], "timeout": 0.5})
        assert res["status"] == "running"
    finally:
        d.stop()


def test_interrupt_cancels_and_releases_slot(tmp_path):
    fake, _ = _fake_spawn(tmp_path)
    d, db, bc = _make(tmp_path, max_concurrent=1, spawn_fn=fake)
    listener = _listen(bc)
    d.start()
    try:
        a = d.spawn({"target_cli": "claude", "prompt": "A", "cwd": str(tmp_path)})
        b = d.spawn({"target_cli": "claude", "prompt": "B", "cwd": str(tmp_path)})
        res = d.interrupt({"agent_id": a["agent_id"]})
        assert res["status"] == "cancelled" and res["stop_reason"] == "interrupted"
        assert res["usage_incomplete"] is True
        assert db.get_agent(a["agent_id"])["status"] == "cancelled"
        assert "agent.cancelled" in "".join(listener["buffer"])
        # 槽位已释放：B 应被补位
        import time
        for _ in range(100):
            if db.get_agent(b["agent_id"])["status"] == "running":
                break
            time.sleep(0.05)
        assert db.get_agent(b["agent_id"])["status"] == "running"
    finally:
        d.stop()


def test_send_message_delivered_then_undelivered(tmp_path):
    fake, _ = _fake_spawn(tmp_path)
    d, db, _ = _make(tmp_path, spawn_fn=fake)
    d.start()
    try:
        a = d.spawn({"target_cli": "claude", "prompt": "X", "cwd": str(tmp_path)})
        res = d.send_message({"agent_id": a["agent_id"], "message": "ping"})
        assert res["status"] == "delivered"
        _finish(tmp_path / "claude-0.json", rc=0)
        d.wait({"agent_id": a["agent_id"], "timeout": 10})
        res = d.send_message({"agent_id": a["agent_id"], "message": "after"})
        assert res["status"] == "undelivered"
        msgs = db.messages_for(a["agent_id"])
        assert [m["role"] for m in msgs] == ["user", "user"]
    finally:
        d.stop()


def test_followup_merges_pending_messages_and_respawns(tmp_path):
    fake, calls = _fake_spawn(tmp_path)
    d, db, _ = _make(tmp_path, spawn_fn=fake)
    d.start()
    try:
        a = d.spawn({"target_cli": "claude", "prompt": "X", "cwd": str(tmp_path)})
        _finish(tmp_path / "claude-0.json", rc=0)
        d.wait({"agent_id": a["agent_id"], "timeout": 10})
        d.send_message({"agent_id": a["agent_id"], "message": "note one"})
        d.send_message({"agent_id": a["agent_id"], "message": "note two"})
        res = d.followup({"agent_id": a["agent_id"], "prompt": "continue"})
        assert res["status"] == "running" and res["merged_messages"] == 2
        assert "continue" in calls[1]["prompt"]
        assert "note one" in calls[1]["prompt"] and "note two" in calls[1]["prompt"]
    finally:
        d.stop()


def test_followup_while_running_queues_then_chains(tmp_path):
    fake, calls = _fake_spawn(tmp_path)
    d, db, _ = _make(tmp_path, spawn_fn=fake)
    d.start()
    try:
        a = d.spawn({"target_cli": "claude", "prompt": "A", "cwd": str(tmp_path)})
        res = d.followup({"agent_id": a["agent_id"], "prompt": "more"})
        assert res["status"] == "queued" and res["merged_messages"] == 0
        _finish(tmp_path / "claude-0.json", rc=0)
        import time
        for _ in range(100):
            if len(calls) >= 2 and db.get_agent(a["agent_id"])["status"] == "running":
                break
            time.sleep(0.05)
        assert len(calls) == 2 and "more" in calls[1]["prompt"]
    finally:
        d.stop()


def test_list_activity_usage_shapes(tmp_path):
    fake, _ = _fake_spawn(tmp_path)
    d, db, _ = _make(tmp_path, spawn_fn=fake)
    d.start()
    try:
        a = d.spawn({"target_cli": "claude", "prompt": "X", "cwd": str(tmp_path),
                     "session_id": "s9"})
        lst = d.list_agents({"session_id": "s9"})
        assert [x["id"] for x in lst["agents"]] == [a["agent_id"]]
        act = d.activity({"agent_id": a["agent_id"], "since_seq": 0})
        assert act["events"] and act["next_seq"] > 0
        assert act["events"][0]["type"] == "agent.spawned"
        use = d.usage({"agent_id": a["agent_id"]})
        assert set(use) >= {"input_tokens", "output_tokens", "cost_usd"}
        assert use["estimated"] is True
    finally:
        d.stop()


def test_spawn_rejects_missing_required_fields(tmp_path):
    fake, _ = _fake_spawn(tmp_path)
    d, db, _ = _make(tmp_path, spawn_fn=fake)
    d.start()
    try:
        import pytest
        with pytest.raises(ValueError):
            d.spawn({"target_cli": "claude"})
        with pytest.raises(ValueError):
            d.spawn({"prompt": "hi", "cwd": str(tmp_path)})
    finally:
        d.stop()
