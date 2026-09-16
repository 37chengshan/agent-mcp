#!/usr/bin/env python3
"""Idempotently start the Agent MCP daemon and optionally open its local UI."""
from __future__ import annotations

import argparse
import json
import os
import hashlib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DAEMON = ROOT / "agent_mcp" / "daemon_main.py"
DEFAULT_PORT = 8765


def default_state_dir() -> Path:
    """与 daemon_main/mcp_server 同口径：AGENT_MCP_HOME 优先，兼容 CODEX_HOME，缺省 ~/.codex。"""
    base = (os.environ.get("AGENT_MCP_HOME")
            or os.environ.get("CODEX_HOME")
            or Path.home() / ".codex")
    return Path(base) / "agent-mcp"


DEFAULT_STATE_DIR = default_state_dir()
HEALTH_ATTEMPTS = 20
HEALTH_INTERVAL = 0.25


def daemon_command(state_dir: Path, port: int = DEFAULT_PORT) -> list[str]:
    return [sys.executable, str(DAEMON), "--port", str(port), "--state-dir", str(state_dir),
            "--web-root", str(ROOT / "web")]


def browser_command(url: str) -> list[str]:
    if os.name == "nt":
        return ["cmd", "/c", "start", "", url]
    if sys.platform == "darwin":
        return ["open", url]
    return ["xdg-open", url]

def read_token(state_dir: Path) -> str:
    try:
        token = json.loads((state_dir / "daemon.json").read_text(encoding="utf-8")).get("token")
        return str(token) if token else ""
    except (OSError, json.JSONDecodeError):
        return ""


def is_healthy(base_url: str, token: str = "") -> bool:
    try:
        with urllib.request.urlopen(f"{base_url}/health", timeout=0.8) as response:
            if response.status != 200:
                return False
            if not hasattr(response, "read"):
                return True
            body = json.loads(response.read().decode("utf-8"))
            if body.get("service") != "agent-mcp-daemon":
                return False
            expected = hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""
            return not expected or body.get("token_sha256") == expected
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False


def start_daemon(state_dir: Path, port: int = DEFAULT_PORT) -> bool:
    base_url = f"http://127.0.0.1:{port}"
    if is_healthy(base_url, read_token(state_dir)):
        return False
    state_dir.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": str(ROOT),
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(daemon_command(state_dir, port), **kwargs)
    for _ in range(HEALTH_ATTEMPTS):
        if is_healthy(base_url, read_token(state_dir)):
            return True
        time.sleep(HEALTH_INTERVAL)
    return False


def doctor_report(state_dir: Path, port: int) -> dict:
    """本地健康体检：daemon / 依赖 / web / 版本 / 状态目录。只读，不改任何文件。"""
    import importlib.util
    base_url = f"http://127.0.0.1:{port}"
    token = read_token(state_dir)
    healthy = is_healthy(base_url, token)
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add("python", sys.version_info >= (3, 9), f"{sys.version.split()[0]} (need >=3.9)")
    add("psutil", importlib.util.find_spec("psutil") is not None, "daemon hard dependency")
    add("mcp_server", (ROOT / "mcp_server.py").is_file(), str(ROOT / "mcp_server.py"))
    add("daemon_main", DAEMON.is_file(), str(DAEMON))
    add("web_index", (ROOT / "web" / "index.html").is_file(), str(ROOT / "web" / "index.html"))
    add("web_loader", (ROOT / "web" / "panels" / "loader.js").is_file(),
        str(ROOT / "web" / "panels" / "loader.js"))
    add("state_dir", state_dir.is_dir(), str(state_dir))
    add("daemon_json", (state_dir / "daemon.json").is_file(),
        str(state_dir / "daemon.json"))
    add("token", bool(token), "present" if token else "missing")
    add("health", healthy, base_url if healthy else f"not healthy at {base_url}")

    version = "unknown"
    try:
        text = (ROOT / "agent_mcp" / "__init__.py").read_text(encoding="utf-8")
        import re as _re
        m = _re.search(r'^__version__\s*=\s*"([^"]+)"', text, _re.M)
        if m:
            version = m.group(1)
    except OSError:
        pass
    add("version", version != "unknown", version)

    ok = all(c["ok"] for c in checks)
    return {
        "ok": ok,
        "version": version,
        "port": port,
        "base_url": base_url,
        "state_dir": str(state_dir),
        "daemon_healthy": healthy,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=int(os.environ.get("AGENT_MCP_PORT", DEFAULT_PORT)))
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--open", action="store_true", help="Open the local monitoring page after startup.")
    parser.add_argument("--doctor", action="store_true",
                        help="Run local health checks and print JSON (does not start daemon).")
    args = parser.parse_args(argv)

    if args.doctor:
        report = doctor_report(args.state_dir, args.port)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1

    base_url = f"http://127.0.0.1:{args.port}"
    started = start_daemon(args.state_dir, args.port)
    token = read_token(args.state_dir)
    if not is_healthy(base_url, token):
        print(json.dumps({"status": "error", "summary": "Agent MCP daemon did not become healthy."}))
        return 1
    url = f"{base_url}/#token={urllib.parse.quote(token, safe='')}"
    # Only open the browser when the daemon was actually started this call.
    # If the daemon was already running, the monitoring page is already open
    # (or was opened by the first start), so skip to avoid stacking tabs.
    open_browser = args.open and started
    if open_browser:
        subprocess.Popen(browser_command(url), stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=os.name != "nt")
    # 不把含 #token= 的完整 URL 打进 stdout（终端捕获/CI 日志会落盘明文）。
    # 浏览器打开时 token 已走 URL fragment；already_running 只给无 token 的 base。
    reported_url = f"{base_url}/"
    print(json.dumps({
        "status": "started" if started else "already_running",
        "url": reported_url,
        "write_auth": "opened_in_browser" if open_browser else "url_fragment",
        "hint": "完整带 token 链接见 state-dir/daemon.json 或 start --open",
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
