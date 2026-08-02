import json
import pytest
from agent_mcp.cli_adapters import get_adapter

CLAUDE_RESULT = {
    "is_error": False, "stop_reason": "end_turn", "session_id": "s-abc",
    "total_cost_usd": 0.3,
    "usage": {"input_tokens": 100, "output_tokens": 20,
              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 50},
    "modelUsage": {"m1": {"inputTokens": 100, "outputTokens": 20,
                          "cacheReadInputTokens": 50, "costUSD": 0.3}},
}

def test_claude_adapter_builds_command():
    a = get_adapter("claude")
    cmd = a.build_command(prompt="hi", cwd="/tmp", model="x",
                          permission_mode="plan", max_turns=5, resume=None)
    assert "--output-format" in cmd and "stream-json" in cmd
    assert "--permission-mode" in cmd and "plan" in cmd

def test_claude_parse_stream_extracts_usage():
    a = get_adapter("claude")
    lines = [json.dumps({"type": "result", "result": CLAUDE_RESULT})]
    events, usage = a.parse_stream(lines)
    assert usage["input_tokens"] == 100
    assert usage["cost_usd"] == 0.3
    assert any(e["type"] == "agent.usage" for e in events)

def test_claude_parse_dedupe_by_message_id():
    a = get_adapter("claude")
    lines = [
        json.dumps({"type": "assistant", "message": {"id": "m1", "content": "a",
                    "usage": {"input_tokens": 5, "output_tokens": 1}}}),
        json.dumps({"type": "assistant", "message": {"id": "m1", "content": "b",
                    "usage": {"input_tokens": 5, "output_tokens": 1}}}),
        json.dumps({"type": "result", "result": CLAUDE_RESULT}),
    ]
    events, usage = a.parse_stream(lines)
    assert usage["input_tokens"] == 100  # result 覆盖，assistant 同 id 不重复累加

def test_claude_parse_message_events():
    a = get_adapter("claude")
    lines = [json.dumps({"type": "assistant", "message": {"id": "m1", "content": "hi"}})]
    events, _ = a.parse_stream(lines)
    assert any(e["type"] == "agent.message" for e in events)

def test_unknown_cli_rejected():
    with pytest.raises(ValueError):
        get_adapter("nonexistent")
