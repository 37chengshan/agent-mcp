from __future__ import annotations
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import psutil

from agent_mcp.cli_adapters import get_adapter
from agent_mcp.sandbox import build_container_sandbox_command, requires_process_fallback

# SEC-H5: 危险环境变量精确名 + 前缀——caller env 注入时一律剥离。
# allowlist-merge：只合并 caller 显式请求的键，且必须先通过本 denylist；
# 绝不透传任意/未请求的环境变量。
_DANGEROUS_ENV_EXACT = frozenset({
    # 动态链接器 / 语言运行时注入面
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "LD_DEBUG",
    "NODE_OPTIONS", "NODE_PATH",
    "BASH_ENV", "ENV", "IFS",
    "PERL5OPT", "PERL5LIB",
    "RUBYOPT", "RUBYLIB",
    "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS",
    # git 配置/传输劫持
    "GIT_SSH_COMMAND", "GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
    # PYTHON* 前缀已覆盖，精确名再锁一道（文档对齐）
    "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE",
})
_DANGEROUS_ENV_PREFIXES = (
    "DYLD_",       # macOS 动态注入
    "PYTHON",      # PYTHONPATH / PYTHONHOME / PYTHONSTARTUP / PYTHONUSERBASE ...
    "BASH_FUNC_",  # bash 导出函数注入
    "LD_",         # 其余 LD_* 注入/调试面
)


def sanitize_env(env: dict[str, str] | None) -> dict[str, str]:
    """SEC-H5: allowlist-merge——只保留 caller 显式请求且通过 denylist 的键。"""
    if not env:
        return {}
    out: dict[str, str] = {}
    for key, value in env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if key in _DANGEROUS_ENV_EXACT or key.startswith(_DANGEROUS_ENV_PREFIXES):
            continue
        out[key] = value
    return out


class SlotScheduler:
    """分池并槽位：read_pool（读密集，默认上限 6）/write_pool（写密集，默认上限 2）。
    写池占用不饿死读任务；followup（同 agent_id 续跑）优先于新 spawn 补位。"""
    def __init__(self, max_concurrent: int = 4, read_pool_max: int = 6,
                 write_pool_max: int = 2,
                 on_stuck_callback: Any = None):
        # 兼容旧调用：max_concurrent 仍作总并发兜底
        self.read_pool_max = read_pool_max
        self.write_pool_max = write_pool_max
        self._active_read: set[str] = set()
        self._active_write: set[str] = set()
        # queue 元素：(key, is_write, enqueue_ts) —— followup 续跑（is_write=False）补位优先
        self._queue: list[tuple[str, bool, float]] = []
        self._lock = threading.Lock()
        self._on_stuck_callback = on_stuck_callback
        # D1: watchdog 线程——每 30s 扫队列，排队 >60s 的任务调 on_stuck_callback(key)
        self._wd_stop = threading.Event()
        self._wd_thread: threading.Thread | None = None
        if on_stuck_callback is not None:
            self._wd_thread = threading.Thread(target=self._watchdog_loop,
                                               daemon=True,
                                               name="slot-watchdog")
            self._wd_thread.start()

    def _pool_of(self, agent_key: str) -> tuple[set[str], int]:
        """同 agent_id 已在某池则归那池；否则按 caller 标注判定。
        队列项的 is_write 标注决定新 key 归池。"""
        if agent_key in self._active_read or agent_key in self._active_write:
            return (self._active_read if agent_key in self._active_read
                    else self._active_write, 0)
        return (self._active_read, 0)  # 默认读池，is_write 由 acquire 调用方决

    def acquire(self, agent_key: str, *, is_write: bool = False) -> bool:
        with self._lock:
            active = self._active_read | self._active_write
            if agent_key in active or any(k == agent_key for k, _, _ in self._queue):
                return False
            target = self._active_write if is_write else self._active_read
            limit = self.write_pool_max if is_write else self.read_pool_max
            if len(target) < limit:
                target.add(agent_key)
                return True
            self._queue.append((agent_key, is_write, time.monotonic()))
            return False

    def release(self, agent_key: str) -> str | None:
        """释放槽位，返回可补位的排队 key（若有）。followup 续跑优先于新 spawn。"""
        with self._lock:
            self._active_read.discard(agent_key)
            self._active_write.discard(agent_key)
            if not self._queue:
                return None
            # 先扫续跑（is_write=False）的排队 key，按 FIFO 首个可补
            for order in (False, True):
                for i, (k, w, _) in enumerate(self._queue):
                    if w != order:
                        continue
                    target = self._active_write if w else self._active_read
                    limit = self.write_pool_max if w else self.read_pool_max
                    if len(target) < limit and k not in (self._active_read | self._active_write):
                        target.add(k)
                        self._queue.pop(i)
                        return k
            return None

    def queued(self) -> list[str]:
        with self._lock:
            return [k for k, _, _ in self._queue]

    def remove(self, agent_key: str) -> None:
        """从队列/活动中移除（中断排队任务用），不触发补位。"""
        with self._lock:
            self._active_read.discard(agent_key)
            self._active_write.discard(agent_key)
            self._queue = [(k, w, ts) for k, w, ts in self._queue if k != agent_key]

    def _watchdog_loop(self) -> None:
        """D1: 每 30s 扫队列，排队 >60s 的任务调 on_stuck_callback(key) 告警。"""
        while not self._wd_stop.wait(30.0):
            now = time.monotonic()
            stuck: list[str] = []
            with self._lock:
                for k, _, ts in self._queue:
                    if now - ts > 60.0:
                        stuck.append(k)
            for key in stuck:
                try:
                    self._on_stuck_callback(key)
                except Exception:
                    pass  # 告警失败不致命


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
                         cwd: str, cli_command: list[str],
                         timeout_seconds: float | None = None,
                         env: dict[str, str] | None = None) -> list[str]:
    """分离 worker：本脚本 --dispatch-worker 模式（与现有 grok MCP 同构）。

    timeout_seconds 置于 command json 之前。SEC-H5: env 不进 argv——
    写入 0600 临时文件，argv 只传路径。"""
    worker = Path(__file__).resolve().parent.parent / "dispatch_worker.py"
    command = [sys.executable, str(worker), str(state_path), str(out_path),
               str(err_path), cwd, str(timeout_seconds or 0),
               json.dumps(cli_command, ensure_ascii=False)]
    if env:
        safe_env = sanitize_env(env)
        if safe_env:
            env_path = state_path.with_suffix(".env.json")
            payload = json.dumps(safe_env, ensure_ascii=False).encode("utf-8")
            if os.name == "nt":
                env_path.write_bytes(payload)
            else:
                fd = os.open(str(env_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    os.write(fd, payload)
                finally:
                    os.close(fd)
                os.chmod(env_path, 0o600)
            command.append(f"@{env_path}")  # @ 前缀 = env 文件路径，不含密钥
    return command


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
                     state_dir: Path,
                     timeout_seconds: float | None = None,
                     env: dict[str, str] | None = None,
                     sandbox_container: str | None = None,
                     sandbox_network: str = "none") -> dict[str, Any]:
    """spawn 一个 CLI 任务 worker（T9 daemon 用）。

    流程：get_adapter → binary() 检查（缺失抛结构化 ValueError）→
    build_command → (可选 build_container_sandbox_command) → build_worker_command → spawn_detached。
    返回 {"worker_pid", "command_summary", "state_path", "out_path", "err_path"}；
    state_dir 下按任务生成 {cli}-{tag}.json / .out.log / .err.log（并发安全）。
    timeout_seconds 透传给 worker（超时由 worker 终止进程树并标 timed_out）。
    """
    adapter = get_adapter(target_cli)
    binary = adapter.binary()
    if not binary:
        raise ValueError(
            f"CLI {target_cli} was not found. Install it or set PATH")
    cli_cmd = adapter.build_command(prompt=prompt, cwd=cwd, model=model,
                                    permission_mode=permission_mode,
                                    max_turns=max_turns, resume=resume)
    if sandbox_container:
        cli_cmd = build_container_sandbox_command(
            cli_cmd,
            image=sandbox_container,
            mount_cwd=cwd,
            # SEC-M4: 与注释对齐——空/none/false/0/off 均视为禁网
            network_disabled=str(sandbox_network).strip().lower()
            in ("", "none", "false", "0", "off"),
            read_only=(permission_mode == "plan"),
        )
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(state_dir, 0o700)
    tag = f"{target_cli}-{uuid.uuid4().hex[:8]}"
    state_path = state_dir / f"{tag}.json"
    out_path = state_dir / f"{tag}.out.log"
    err_path = state_dir / f"{tag}.err.log"
    worker_cmd = build_worker_command(state_path=state_path, out_path=out_path,
                                      err_path=err_path, cwd=cwd, cli_command=cli_cmd,
                                      timeout_seconds=timeout_seconds, env=env)
    proc = spawn_detached(worker_cmd)
    return {"worker_pid": proc.pid, "command_summary": " ".join(cli_cmd),
            "state_path": str(state_path), "out_path": str(out_path),
            "err_path": str(err_path)}
