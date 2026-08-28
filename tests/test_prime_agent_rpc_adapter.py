"""P4 DoD：PrimeAgentRPCAdapter（fake 协议帧级测试；能力三态默认声明）。"""

from __future__ import annotations

from agent_mcp import models as m
from agent_mcp.cli_adapters import PrimeAgentRPCAdapter, get_adapter


def test_adapter_registered_with_alias():
    assert isinstance(get_adapter("prime-agent-rpc"), PrimeAgentRPCAdapter)
    assert isinstance(get_adapter("prime-agent"), PrimeAgentRPCAdapter)


def test_capability_contract_defaults():
    a = PrimeAgentRPCAdapter()
    a.capabilities.assert_supported("spawn")
    a.capabilities.assert_supported("resume")
    a.capabilities.assert_supported("steer")
    import pytest
    with pytest.raises(PermissionError):
        a.capabilities.assert_supported("goal")  # 默认 UNSUPPORTED：未协商不得调用（Invariant 6）
    # DEGRADED 允许调用但调用方须按降级语义处理
    a.capabilities.assert_supported("autonomous")


def test_capability_overrides():
    a = PrimeAgentRPCAdapter({"goal": m.CAP_SUPPORTED})
    a.capabilities.assert_supported("goal")


def test_build_command_rpc_resume(monkeypatch):
    a = PrimeAgentRPCAdapter()
    monkeypatch.setattr(a, "binary", lambda: "/usr/bin/prime-agent")
    monkeypatch.delenv("AGENT_MCP_PRIME_RPC_ARGS", raising=False)
    cmd = a.build_command(prompt="hello", cwd="/tmp", model=None,
                          permission_mode="plan", max_turns=8, resume="sess-1")
    assert cmd[0] == "/usr/bin/prime-agent"
    assert "--rpc" in cmd
    assert "--resume" in cmd and "sess-1" in cmd
    assert cmd[-1] == "hello"


def test_parse_stream_normalizes_and_accumulates_usage():
    a = PrimeAgentRPCAdapter()
    lines = [
        '{"type":"message","text":"working..."}',
        '{"type":"tool_use","name":"bash","arguments":{"cmd":"ls"}}',
        '{"type":"usage","usage":{"input_tokens":10,"output_tokens":4,"cost_usd":0.001}}',
        '{"type":"unknown_frame","x":1}',
        "not-json",
    ]
    events, usage = a.parse_stream(lines)
    types = [e["type"] for e in events]
    assert "agent.message" in types
    assert "agent.tool_use" in types
    assert "agent.log" in types  # 未知帧降级为日志（不丢数据）
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 4
    assert abs(usage["cost_usd"] - 0.001) < 1e-9


def test_extract_session_id():
    a = PrimeAgentRPCAdapter()
    assert a.extract_session_id({"sessionId": "s9"}) == "s9"
    assert a.extract_session_id({"session_id": "s8"}) == "s8"
    assert a.extract_session_id({"text": "no session"}) is None

