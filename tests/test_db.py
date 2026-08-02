import sqlite3
from agent_mcp.db import DB

def test_agent_crud_and_tree(tmp_path):
    db = DB(tmp_path / "test.db")
    aid = db.insert_agent(parent_id=None, session_id="s1", task_name="/root",
                          cli="claude", model="x", cwd=str(tmp_path))
    cid = db.insert_agent(parent_id=aid, session_id="s1", task_name="/root/t1",
                          cli="grok", model="y", cwd=str(tmp_path))
    db.set_status(cid, "running")
    db.set_status(cid, "terminated", stop_reason="end_turn")
    row = db.get_agent(cid)
    assert row["status"] == "terminated" and row["stop_reason"] == "end_turn"
    assert row["parent_id"] == aid

def test_events_are_sequence_and_delta_not_persisted(tmp_path):
    db = DB(tmp_path / "test.db")
    e1 = db.insert_event(agent_id=1, type="agent.message", payload={"text": "x"}, session_id="s1")
    e2 = db.insert_event(agent_id=1, type="agent.message_delta", payload={"d": "x"}, session_id="s1")
    assert e1 == 1
    assert e2 is None  # delta 不落库
    rows = db.events_since(0, session_id="s1")
    assert len(rows) == 1 and rows[0]["type"] == "agent.message"

def test_usage_projection_and_upsert(tmp_path):
    db = DB(tmp_path / "test.db")
    db.upsert_usage(agent_id=1, model="m1", input_tokens=10, output_tokens=5,
                    cache_creation=0, cache_read=0, cost_usd=0.1)
    db.upsert_usage(agent_id=1, model="m1", input_tokens=3, output_tokens=1,
                    cache_creation=0, cache_read=0, cost_usd=0.05)  # 同模型再上报 → 覆盖
    total = db.usage_total(agent_id=1)
    assert total["input_tokens"] == 3 and total["cost_usd"] == 0.05

def test_session_scoping(tmp_path):
    db = DB(tmp_path / "test.db")
    db.insert_agent(parent_id=None, session_id="s1", task_name="/root", cli="c", cwd=".")
    db.insert_agent(parent_id=None, session_id="s2", task_name="/root", cli="c", cwd=".")
    assert len(db.agents_by_session("s1")) == 1
    assert len(db.agents_by_session(None)) == 2

def test_messages_retention_limit(tmp_path):
    db = DB(tmp_path / "test.db", max_messages_per_agent=3)
    for i in range(5):
        db.insert_message(agent_id=1, role="assistant", content=f"msg{i}")
    msgs = db.messages_for(1)
    assert len(msgs) == 3
    assert msgs[0]["content"] == "msg2"  # 只保留最近 3 条
