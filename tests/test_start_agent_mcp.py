"""Launcher (start_agent_mcp.py) 契约：默认 state dir 与 daemon/mcp_server 同口径。

daemon_main.default_state_dir / mcp_server.state_dir_from_env 均为
AGENT_MCP_HOME > CODEX_HOME > ~/.codex，launcher 必须一致，否则 mcp_server
探测的 daemon.json 与 launcher 实际拉起的 daemon 可能分属不同 state dir。
"""
from pathlib import Path

import start_agent_mcp


def test_launcher_default_state_dir_prefers_agent_mcp_home(monkeypatch):
    monkeypatch.delenv("AGENT_MCP_HOME", raising=False)
    monkeypatch.setenv("CODEX_HOME", "/tmp/codexhome")
    assert start_agent_mcp.default_state_dir() == Path("/tmp/codexhome") / "agent-mcp"

    monkeypatch.setenv("AGENT_MCP_HOME", "/tmp/amh")
    assert start_agent_mcp.default_state_dir() == Path("/tmp/amh") / "agent-mcp"


def test_launcher_default_state_dir_falls_back_to_codex_home_dir(monkeypatch):
    monkeypatch.delenv("AGENT_MCP_HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert start_agent_mcp.default_state_dir() == Path.home() / ".codex" / "agent-mcp"
