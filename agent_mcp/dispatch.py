from __future__ import annotations
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

import psutil

from agent_mcp.cli_adapters import get_adapter


class SlotScheduler:
    """FIFO 并发槽位（Codex V2 AgentExecutionLimiter 的本地版）。"""
    def __init__(self, max_concurrent: int = 4):
        self.max = max_concurrent
        self._active: set[str] = set()
        self._queue: list[str] = []
        self._lock = threading.Lock()

    def acquire(self, agent_key: str) -> bool:
        with self._lock:
            if agent_key in self._active or agent_key in self._queue:
                return False
            if len(self._active) < self.max:
                self._active.add(agent_key)
                return True
            self._queue.append(agent_key)
            return False

    def release(self, agent_key: str) -> str | None:
        """释放槽位，返回可补位的排队 key（若有）。"""
        with self._lock:
            self._active.discard(agent_key)
            while self._queue:
                nxt = self._queue.pop(0)
                if nxt not in self._active:
                    self._active.add(nxt)
                    return nxt
            return None

    def queued(self) -> list[str]:
        with self._lock:
            return list(self._queue)


def terminate_process_tree(pid: int, *, timeout: float = 5.0) -> bool:
    """跨平台进程树终止。macOS 用 SIGTERM→SIGKILL；Windows TerminateProcess。"""
    if pid <= 0:
        return False
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    try:
        children = proc.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        proc.terminate()
        gone, alive = psutil.wait_procs([proc] + children, timeout=timeout)
        for still in alive:
            try:
                still.kill()
            except psutil.NoSuchProcess:
                pass
        return True
    except (psutil.Error, OSError):
        return False


def is_pid_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def build_worker_command(*, state_path: Path, out_path: Path, err_path: Path,
                         cwd: str, cli_command: list[str]) -> list[str]:
    """分离 worker：本脚本 --dispatch-worker 模式（与现有 grok MCP 同构）。"""
    worker = Path(__file__).resolve().parent.parent / "dispatch_worker.py"
    return [sys.executable, str(worker), str(state_path), str(out_path),
            str(err_path), cwd, json.dumps(cli_command, ensure_ascii=False)]


def spawn_detached(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.Popen:
    """跨平台分离启动（daemon / worker 用）。"""
    kwargs: dict[str, Any] = dict(env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                   | getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def spawn_cli_worker(target_cli: str, *, prompt: str, cwd: str,
                     permission_mode: str = "plan", model: str | None = None,
                     max_turns: int = 8, resume: str | None = None,
                     state_dir: Path) -> dict[str, Any]:
    """spawn 一个 CLI 任务 worker（T9 daemon 用）。

    流程：get_adapter → binary() 检查（缺失抛结构化 ValueError）→
    build_command → build_worker_command → spawn_detached。
    返回 {"worker_pid": ..., "command_summary": ...}；state_dir 下按任务
    生成 {cli}-{tag}.json / .out.log / .err.log（并发安全）。
    """
    adapter = get_adapter(target_cli)
    binary = adapter.binary()
    if not binary:
        raise ValueError(
            f"CLI {target_cli} was not found. Install it or set PATH")
    cli_cmd = adapter.build_command(prompt=prompt, cwd=cwd, model=model,
                                    permission_mode=permission_mode,
                                    max_turns=max_turns, resume=resume)
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{target_cli}-{uuid.uuid4().hex[:8]}"
    worker_cmd = build_worker_command(
        state_path=state_dir / f"{tag}.json",
        out_path=state_dir / f"{tag}.out.log",
        err_path=state_dir / f"{tag}.err.log",
        cwd=cwd, cli_command=cli_cmd)
    proc = spawn_detached(worker_cmd)
    return {"worker_pid": proc.pid, "command_summary": " ".join(cli_cmd)}
