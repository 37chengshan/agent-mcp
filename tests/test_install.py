"""T13 安装/迁移脚本测试：三主载体注册片段 + 写配置（备份）+ 回滚 + 旧 grok-cli 迁移。

纯函数为主；文件写入测试用 tmp_path 注入路径，不触碰真实家目录。
"""
import json
from pathlib import Path

import pytest

from install import (
    LEGACY_TOOL_MAP,
    apply_claude_install,
    apply_codex_install,
    backup_path,
    claude_registration_json,
    codex_registration_toml,
    find_legacy_section,
    has_section,
    install_host,
    legacy_tool_map_text,
    omp_registration,
    remove_legacy_section,
)

SCRIPT = "/tmp/mcp_server.py"


# --- 三主载体注册片段（纯函数） ---

def test_codex_toml_snippet():
    toml = codex_registration_toml(SCRIPT)
    assert "[mcp_servers.agent-mcp]" in toml
    assert "mcp_server.py" in toml


def test_codex_toml_has_command_and_timeout():
    toml = codex_registration_toml(SCRIPT)
    assert 'command = "python3"' in toml
    assert "startup_timeout_sec = 30" in toml
    assert "mcp_server.py" in toml.split("args =")[1]


def test_claude_json_snippet():
    obj = claude_registration_json(SCRIPT)
    entry = obj["mcpServers"]["agent-mcp"]
    assert entry["command"].endswith("python3")
    assert "mcp_server.py" in entry["args"][0]


def test_omp_registration_returns_notes():
    notes = omp_registration(SCRIPT)
    assert isinstance(notes, str) and len(notes) > 20


# --- 备份命名 ---

def test_backup_path_naming():
    p = backup_path(Path("/tmp/config.toml"))
    assert p.name.startswith("config.toml.bak-agentmcp-")
    assert p.parent == Path("/tmp")


def test_backup_path_uses_given_ts():
    p = backup_path(Path("/tmp/config.toml"), ts="20260803T010203")
    assert p.name == "config.toml.bak-agentmcp-20260803T010203"


# --- codex config.toml 编辑（纯函数） ---

def test_has_section_detects_existing():
    text = "[model_provider]\nname = 'x'\n\n[mcp_servers.agent-mcp]\ncommand = 'python3'\n"
    assert has_section(text, "mcp_servers.agent-mcp")
    assert not has_section(text, "mcp_servers.grok-cli")


def test_apply_codex_appends_when_missing():
    snippet = codex_registration_toml(SCRIPT)
    new_text, action = apply_codex_install("# existing config\n", snippet)
    assert action == "append"
    assert "[mcp_servers.agent-mcp]" in new_text
    assert "# existing config" in new_text


def test_apply_codex_skips_when_present():
    snippet = codex_registration_toml(SCRIPT)
    old = "[mcp_servers.agent-mcp]\ncommand = 'python3'\n"
    new_text, action = apply_codex_install(old, snippet)
    assert action == "skip"
    assert new_text == old  # 原内容原样返回，不重复追加


def test_apply_codex_preserves_no_trailing_newline_file():
    snippet = codex_registration_toml(SCRIPT)
    new_text, action = apply_codex_install("x = 1", snippet)
    assert action == "append"
    # 无尾随换行的文件先补换行，再追加块
    assert new_text.endswith("startup_timeout_sec = 30\n")
    assert "[mcp_servers.agent-mcp]" in new_text.split("x = 1")[1]


# --- 旧 grok-cli 检测与移除 ---

def test_find_legacy_section():
    assert find_legacy_section("[mcp_servers.grok-cli]\ncommand = 'x'\n")
    assert not find_legacy_section("[mcp_servers.agent-mcp]\n")
    assert not find_legacy_section("")


def test_remove_legacy_section():
    text = ("# head\n"
            "[mcp_servers.grok-cli]\n"
            'command = "python3"\n'
            'args = ["/tmp/grok_cli_mcp.py"]\n'
            "\n"
            "[other]\n"
            "x = 1\n")
    out = remove_legacy_section(text)
    assert "[mcp_servers.grok-cli]" not in out
    assert "# head" in out
    assert "[other]" in out


# --- claude JSON 合并（纯函数） ---

def test_apply_claude_merges_other_servers():
    cfg = {"mcpServers": {"other": {"command": "x", "args": ["y"]}}}
    out = apply_claude_install(cfg, SCRIPT)
    assert out["mcpServers"]["other"] == {"command": "x", "args": ["y"]}
    assert out["mcpServers"]["agent-mcp"]["args"] == [SCRIPT]


def test_apply_claude_keeps_unrelated_keys():
    cfg = {"permissions": {"allow": ["Bash"]}, "mcpServers": {}}
    out = apply_claude_install(cfg, SCRIPT)
    assert out["permissions"] == {"allow": ["Bash"]}


# --- install_host：dry-run 不写文件，实际安装写备份 ---

def _paths(tmp_path):
    return {
        "codex": tmp_path / "config.toml",
        "claude": tmp_path / ".claude.json",
    }


def test_install_dry_run_writes_nothing(tmp_path):
    codex_cfg = tmp_path / "config.toml"
    codex_cfg.write_text("# pre\n")
    script = tmp_path / "mcp_server.py"
    script.write_text("x")

    logs = install_host("codex", str(script), _paths(tmp_path), dry_run=True)

    assert any("[dry-run]" in line for line in logs)
    assert codex_cfg.read_text() == "# pre\n"  # 未修改
    assert not list(tmp_path.glob("*.bak-agentmcp-*"))  # 未产生备份


def test_install_codex_writes_and_backs_up(tmp_path):
    codex_cfg = tmp_path / "config.toml"
    codex_cfg.write_text("# pre\n")
    script = tmp_path / "mcp_server.py"
    script.write_text("x")

    logs = install_host("codex", str(script), _paths(tmp_path), dry_run=False)

    text = codex_cfg.read_text()
    assert "[mcp_servers.agent-mcp]" in text
    assert "# pre" in text
    backups = list(tmp_path.glob("*.bak-agentmcp-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "# pre\n"  # 备份是原内容
    assert any("备份" in line for line in logs)


def test_install_claude_writes_and_backs_up(tmp_path):
    claude_cfg = tmp_path / ".claude.json"
    claude_cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    script = tmp_path / "mcp_server.py"
    script.write_text("x")

    logs = install_host("claude", str(script), _paths(tmp_path), dry_run=False)

    data = json.loads(claude_cfg.read_text())
    assert "agent-mcp" in data["mcpServers"]
    assert data["mcpServers"]["other"]["command"] == "x"  # 已有 server 保留
    assert list(tmp_path.glob(".claude.json.bak-agentmcp-*"))
    assert any("备份" in line for line in logs)


def test_install_omp_only_returns_guidance(tmp_path):
    script = tmp_path / "mcp_server.py"
    script.write_text("x")
    logs = install_host("omp", str(script), _paths(tmp_path))
    assert len(logs) == 1
    assert "omp" in logs[0].lower()
    assert "mcp_server.py" in logs[0]


def test_install_skip_when_already_registered(tmp_path):
    codex_cfg = tmp_path / "config.toml"
    codex_cfg.write_text("[mcp_servers.agent-mcp]\ncommand = 'python3'\n")
    script = tmp_path / "mcp_server.py"
    script.write_text("x")

    logs = install_host("codex", str(script), _paths(tmp_path), dry_run=False)

    assert any("跳过" in line for line in logs)
    assert not list(tmp_path.glob("*.bak-agentmcp-*"))  # 无变更不备份


def test_install_detects_legacy_and_removes_with_flag(tmp_path):
    codex_cfg = tmp_path / "config.toml"
    codex_cfg.write_text("[mcp_servers.grok-cli]\ncommand = 'python3'\n")
    script = tmp_path / "mcp_server.py"
    script.write_text("x")
    paths = _paths(tmp_path)

    logs = install_host("codex", str(script), paths, dry_run=False)
    assert any("grok-cli" in line for line in logs)  # 默认提示废弃
    assert "[mcp_servers.grok-cli]" in codex_cfg.read_text()  # 未删除

    logs2 = install_host("codex", str(script), paths, dry_run=False, remove_legacy=True)
    text = codex_cfg.read_text()
    assert "[mcp_servers.grok-cli]" not in text
    assert "[mcp_servers.agent-mcp]" in text
    assert any("已移除" in line for line in logs2)


def test_install_unknown_host_raises(tmp_path):
    script = tmp_path / "mcp_server.py"
    script.write_text("x")
    with pytest.raises(ValueError):
        install_host("windows", str(script), _paths(tmp_path))


def test_install_claude_corrupt_json_aborts_without_overwrite(tmp_path):
    claude_cfg = tmp_path / ".claude.json"
    claude_cfg.write_text("{not json")
    script = tmp_path / "mcp_server.py"
    script.write_text("x")

    logs = install_host("claude", str(script), _paths(tmp_path), dry_run=False)

    assert any("错误" in line for line in logs)
    assert claude_cfg.read_text() == "{not json"  # 原样保留
    assert not list(tmp_path.glob(".claude.json.bak-agentmcp-*"))


# --- 工具名映射表（旧 9 → 新 8） ---

def test_legacy_tool_map_text_covers_all_nine():
    text = legacy_tool_map_text()
    assert "list_grok_models" in text
    assert "cancel_grok_dispatch" in text
    assert "spawn_agent" in text
    assert "interrupt_agent" in text
    assert "list_agents" in text
    for entry in LEGACY_TOOL_MAP:
        assert entry["old"] in text
        assert entry["new"] in text


def test_legacy_tool_map_has_nine_entries():
    assert len(LEGACY_TOOL_MAP) == 9
    olds = [e["old"] for e in LEGACY_TOOL_MAP]
    assert len(set(olds)) == 9  # 无重复
