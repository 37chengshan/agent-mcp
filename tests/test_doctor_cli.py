"""start_agent_mcp --doctor 契约：只读健康体检，JSON 输出，不拉起 daemon。"""
from __future__ import annotations

import json
from pathlib import Path

import start_agent_mcp


def test_doctor_report_shape_and_keys(tmp_path):
    report = start_agent_mcp.doctor_report(tmp_path / "state", 8765)
    assert set(report) >= {"ok", "version", "port", "base_url", "state_dir",
                           "daemon_healthy", "checks"}
    names = {c["name"] for c in report["checks"]}
    for expect in ("python", "psutil", "mcp_server", "daemon_main", "web_index",
                   "web_loader", "state_dir", "daemon_json", "token", "health",
                   "version"):
        assert expect in names
    for c in report["checks"]:
        assert set(c) == {"name", "ok", "detail"}
        assert isinstance(c["ok"], bool)


def test_doctor_cli_json_and_exit_code(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(start_agent_mcp, "is_healthy", lambda *_a, **_k: False)
    code = start_agent_mcp.main([
        "--doctor", "--state-dir", str(tmp_path / "empty"), "--port", "8799",
    ])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["port"] == 8799
    assert data["daemon_healthy"] is False
    assert data["ok"] is False
    assert code == 1


def test_doctor_ok_when_healthy_and_files_present(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    state.mkdir()
    (state / "daemon.json").write_text(json.dumps({"token": "t"}), encoding="utf-8")
    monkeypatch.setattr(start_agent_mcp, "is_healthy", lambda *_a, **_k: True)
    code = start_agent_mcp.main(["--doctor", "--state-dir", str(state), "--port", "8765"])
    data = json.loads(capsys.readouterr().out)
    # 文件/依赖检查应通过（仓库内 mcp_server/web 存在）；token+health 被 mock 为 ok
    assert data["daemon_healthy"] is True
    by = {c["name"]: c for c in data["checks"]}
    assert by["mcp_server"]["ok"] is True
    assert by["web_index"]["ok"] is True
    assert by["token"]["ok"] is True
    assert code == 0
