"""P5 DoD（gate）：PrimeAgentACPAdapter 能力契约桩。
真实 prime-agent --acp 冒烟 ⏳ 待用户环境执行；打通后转 SUPPORTED 并合入执行链。
"""

from __future__ import annotations

import pytest

from agent_mcp import models as m
from agent_mcp.cli_adapters import PrimeAgentACPAdapter, get_adapter


def test_acp_adapter_registered():
    assert isinstance(get_adapter("prime-agent-acp"), PrimeAgentACPAdapter)


def test_acp_gate_capabilities_honest():
    a = PrimeAgentACPAdapter()
    # gate 判定前：一切未真实验证的能力都不可当作 SUPPORTED 调用
    for name in m.ADAPTER_CAPABILITIES:
        state = a.capabilities.get(name)
        # P8 诚实：gate 未冒烟前，任何能力都不得声明为 SUPPORTED
        assert state != m.CAP_SUPPORTED
    # UNSUPPORTED 不可调用（Invariant 6）；DEGRADED 允许按降级语义调用
    with pytest.raises(PermissionError):
        a.capabilities.assert_supported("goal")
    a.capabilities.assert_supported("spawn")  # DEGRADED


def test_acp_build_command():
    a = PrimeAgentACPAdapter()
    a._BIN = ["prime-agent"]
    cmd = a.build_command(prompt="hi", cwd="/tmp", model=None, permission_mode="plan",
                          max_turns=8, resume=None)
    assert "--acp" in cmd and cmd[-1] == "hi"


def test_acp_parse_stream_permissive():
    a = PrimeAgentACPAdapter()
    events, usage = a.parse_stream(['event: foo', '{"type":"message","content":"ok"}', "garbage"])
    assert any(e["type"] == "agent.message" for e in events)

