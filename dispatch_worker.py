#!/usr/bin/env python3
"""分离 worker：运行 CLI 命令并写 state/stdout/stderr（daemon 派发的独立进程）。

用法: python dispatch_worker.py <state.json> <stdout> <stderr> <cwd> <json_command>

与 grok_cli_mcp.py 的 --dispatch-worker 分支同构；自包含，不依赖 agent_mcp 包
（worker 由 daemon 以任意 cwd 分离启动）。state 文件仅含元数据，不携带密钥。
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_json(path: Path, data: dict) -> None:
    Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def dispatch_worker(state_path: Path, stdout_path: Path, stderr_path: Path,
                    command: list[str], cwd: Path) -> int:
    """读 state → 标 running（worker_pid）→ 运行 CLI → 标 finished（process_status）。"""
    state = read_json(state_path)
    state.update({"worker_pid": os.getpid(), "status": "running", "updated_at": utc_now()})
    write_json(state_path, state)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with stdout_path.open("w", encoding="utf-8") as out, \
                stderr_path.open("w", encoding="utf-8") as err:
            completed = subprocess.run(command, cwd=cwd, stdout=out, stderr=err,
                                       text=True, check=False)
        rc = completed.returncode
    except OSError as exc:
        # CLI 缺失等启动失败：写错误状态，避免 state 永远停在 running
        state = read_json(state_path)
        state.update({"status": "finished", "process_status": -1, "error": str(exc),
                      "completed_at": utc_now(), "updated_at": utc_now()})
        write_json(state_path, state)
        return -1
    state = read_json(state_path)
    state.update({"status": "finished", "process_status": rc,
                  "completed_at": utc_now(), "updated_at": utc_now()})
    write_json(state_path, state)
    return rc


def main() -> int:
    if len(sys.argv) != 6:
        print(__doc__, file=sys.stderr)
        return 2
    state_path = Path(sys.argv[1])
    stdout_path = Path(sys.argv[2])
    stderr_path = Path(sys.argv[3])
    cwd = Path(sys.argv[4]).resolve()
    try:
        command = json.loads(sys.argv[5])
    except json.JSONDecodeError:
        return 2
    if not isinstance(command, list) or not all(isinstance(i, str) for i in command):
        return 2
    return dispatch_worker(state_path, stdout_path, stderr_path, command, cwd)


if __name__ == "__main__":
    raise SystemExit(main())
