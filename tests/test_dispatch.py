import json
import subprocess
import pytest
from agent_mcp import cli_adapters
from agent_mcp.dispatch import (SlotScheduler, build_worker_command,
                                terminate_process_tree, is_pid_running,
                                spawn_cli_worker, spawn_detached)

def test_slot_scheduler_fifo():
    s = SlotScheduler(max_concurrent=2)
    assert s.acquire("a") and s.acquire("b")
    assert not s.acquire("c")  # 满，入队
    assert s.queued() == ["c"]
    assert s.release("a") == "c"  # 释放时 FIFO 自动补位
    assert s.acquire("c") is False  # 已被补位激活，不可重复入队
    s.release("b")
    assert s.acquire("d")  # 有空位后可入

def test_slot_scheduler_release_promotes_queued():
    s = SlotScheduler(max_concurrent=1)
    assert s.acquire("a")
    assert not s.acquire("b")  # 入队
    nxt = s.release("a")
    assert nxt == "b"  # 队列补位
    assert s.queued() == []

def test_worker_command_includes_state_paths(tmp_path):
    cmd = build_worker_command(state_path=tmp_path / "s.json",
                               out_path=tmp_path / "o.log", err_path=tmp_path / "e.log",
                               cwd=str(tmp_path), cli_command=["claude", "-p", "hi"])
    assert any("dispatch_worker.py" in c for c in cmd)
    assert str(tmp_path / "s.json") in cmd
    assert str(tmp_path / "o.log") in cmd
    assert str(tmp_path / "e.log") in cmd

def test_process_tree_terminate_smoke():
    # psutil 进程树终止的轻量冒烟：spawn sleep 子进程再杀
    import subprocess, time, psutil
    p = subprocess.Popen(["sh", "-c", "sleep 30 & sleep 30"])
    time.sleep(0.5)
    tree = psutil.Process(p.pid).children(recursive=True)
    assert len(tree) >= 1
    assert terminate_process_tree(p.pid)
    gone, alive = psutil.wait_procs([p] + tree, timeout=5)
    assert not alive  # 整棵树退出

def test_is_pid_running_and_reaped():
    import subprocess, time
    p = subprocess.Popen(["sleep", "30"])
    assert is_pid_running(p.pid)
    p.terminate()
    p.wait(timeout=5)
    time.sleep(0.2)
    assert not is_pid_running(p.pid)
    assert not is_pid_running(None)
    assert not is_pid_running(-1)

def test_spawn_cli_worker_builds_and_spawns(monkeypatch, tmp_path):
    captured = {}
    def fake_spawn(cmd, **kw):
        captured["cmd"] = cmd
        return subprocess.Popen(["true"])
    monkeypatch.setattr("agent_mcp.dispatch.spawn_detached", fake_spawn)
    info = spawn_cli_worker("claude", prompt="hi", cwd="/tmp",
                            permission_mode="plan",
                            state_dir=tmp_path)
    cmd = captured["cmd"]
    assert any("dispatch_worker.py" in c for c in cmd)
    assert any(c.startswith(str(tmp_path)) for c in cmd)  # state/out/err 落在 state_dir 下
    cli_json = json.loads(cmd[-1])
    assert cli_json[0].endswith("claude") and "hi" in cli_json
    assert "claude" in info["command_summary"]
    assert "--permission-mode" in info["command_summary"]
    assert is_pid_running(info["worker_pid"])  # spawn 的对象存活（true 进程）

def test_spawn_cli_worker_binary_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_adapters._CLAUDE, "binary", lambda: None)
    with pytest.raises(ValueError, match="was not found"):
        spawn_cli_worker("claude", prompt="hi", cwd="/tmp", state_dir=tmp_path)
