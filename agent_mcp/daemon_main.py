from __future__ import annotations
import argparse
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

from agent_mcp.daemon_http import DaemonHTTPServer, EventBroadcaster, HEARTBEAT_SECONDS
from agent_mcp.db import DB
from agent_mcp.dispatch import SlotScheduler, is_pid_running

DEFAULT_PORT = 8765
DEFAULT_STATE_DIR = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "agent-mcp"
DEFAULT_WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


def _write_private(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)


def _load_or_create_token(state_dir: Path) -> str:
    """读取或生成 daemon token（0600 daemon.json；跨重启保留，MCP 端无需重读）。"""
    path = state_dir / "daemon.json"
    if path.is_file():
        try:
            token = json.loads(path.read_text(encoding="utf-8")).get("token")
            if token:
                return token
        except Exception:
            pass
    token = uuid.uuid4().hex
    _write_private(path, {"token": token})
    return token


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent MCP daemon")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--web-root", type=Path, default=DEFAULT_WEB_ROOT)
    args = parser.parse_args()

    state_dir = args.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)

    lock_path = state_dir / "daemon.lock"
    if lock_path.is_file():
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            if is_pid_running(lock.get("pid")):
                print(f"daemon already running (pid {lock['pid']})", file=sys.stderr)
                return 0
        except Exception:
            pass  # 残留/损坏锁，覆盖

    token = _load_or_create_token(state_dir)
    db = DB(state_dir / "daemon.db")
    scheduler = SlotScheduler()
    broadcaster = EventBroadcaster()
    dispatcher = None  # 派发执行器在后续任务接入
    srv = DaemonHTTPServer(("127.0.0.1", args.port), args.web_root, token=token,
                           db=db, dispatcher=dispatcher, broadcaster=broadcaster)

    _write_private(lock_path, {"pid": os.getpid(), "ts": time.time()})

    def _heartbeat() -> None:
        while True:
            time.sleep(HEARTBEAT_SECONDS)
            broadcaster.heartbeat_all()

    threading.Thread(target=_heartbeat, daemon=True).start()

    try:
        print(f"agent-mcp daemon on http://127.0.0.1:{srv.server_address[1]}", file=sys.stderr)
        srv.serve_forever()
    finally:
        srv.server_close()
        lock_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
