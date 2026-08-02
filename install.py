#!/usr/bin/env python3
"""Agent MCP 安装 / 迁移脚本（纯 stdlib，不 import agent_mcp）。

三主载体（codex/claude/omp）注册同一 MCP server（mcp_server.py）：
- --install：写配置前先备份（*.bak-agentmcp-<ts>）；--dry-run 只打印不写文件
- codex：~/.codex/config.toml 末尾追加 [mcp_servers.agent-mcp]；检测旧 [mcp_servers.grok-cli]
  并提示废弃，--remove-legacy 自动移除
- claude：~/.claude.json（或 --claude-config <path>）的 mcpServers 合并写入
- omp：MCP client 配置格式未实测，只输出操作指引
- --rollback：从最新备份恢复（恢复后删除备份）
- --legacy-map：打印旧 9 工具 → 新 8 工具映射（breaking change 迁移表）

只做配置变更，不拷贝代码文件：默认假定 mcp_server.py 已在目标位置，
或由用户自行拷贝整个项目目录。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

SERVER_NAME = "agent-mcp"
LEGACY_NAME = "grok-cli"
BACKUP_SUFFIX = ".bak-agentmcp-"
HOSTS = ("codex", "claude", "omp")

# 旧 grok-cli 9 工具 → 新 8 工具映射（breaking change；旧 skill/提示词按此迁移）
LEGACY_TOOL_MAP: list[dict[str, str]] = [
    {"old": "list_grok_models", "new": "spawn_agent",
     "note": "模型枚举无直接等价：模型选择改为 spawn_agent 的 model 参数，未指定走 CLI 默认"},
    {"old": "ask_grok", "new": "spawn_agent",
     "note": "spawn_agent(target_cli=\"grok\", permission_mode=\"plan\")；prompt 对应原提问"},
    {"old": "delegate_to_grok", "new": "spawn_agent",
     "note": "spawn_agent(target_cli=\"grok\", permission_mode=\"plan\")"},
    {"old": "delegate_to_grok_full", "new": "spawn_agent",
     "note": "spawn_agent(target_cli=\"grok\", permission_mode=\"fullAccess\")"},
    {"old": "dispatch_to_grok", "new": "spawn_agent",
     "note": "同 delegate_to_grok：spawn_agent(target_cli=\"grok\", permission_mode=\"plan\")"},
    {"old": "dispatch_to_grok_full", "new": "spawn_agent",
     "note": "同 delegate_to_grok_full：spawn_agent(target_cli=\"grok\", permission_mode=\"fullAccess\")"},
    {"old": "get_grok_dispatch_status", "new": "wait_agent / get_agent_activity",
     "note": "阻塞等结果用 wait_agent(agent_id)；查实时活动用 get_agent_activity(agent_id, since_seq)"},
    {"old": "list_grok_dispatches", "new": "list_agents",
     "note": "list_agents() 列出 agent 树；session_id 过滤会话"},
    {"old": "cancel_grok_dispatch", "new": "interrupt_agent",
     "note": "interrupt_agent(agent_id) 终止进程树并标记 cancelled"},
]


# --- 三主载体注册片段（纯函数） ---

def _toml_str(value: str) -> str:
    """TOML 基本字符串字面量（转义规则与 JSON 字符串一致）。"""
    return json.dumps(value)


def codex_registration_toml(script_path: str) -> str:
    """生成 codex config.toml 的 [mcp_servers.agent-mcp] 片段。"""
    return (
        f"[mcp_servers.{SERVER_NAME}]\n"
        f'command = {_toml_str("python3")}\n'
        f"args = [{_toml_str(script_path)}]\n"
        "startup_timeout_sec = 30\n"
    )


def claude_registration_json(script_path: str) -> dict[str, Any]:
    """生成 claude 注册对象（~/.claude.json 或项目 .mcp.json 的 mcpServers 片段）。"""
    return {
        "mcpServers": {
            SERVER_NAME: {
                "command": "python3",
                "args": [script_path],
            }
        }
    }


def omp_registration(script_path: str) -> str:
    """omp 的 MCP client 配置格式未实测，返回操作指引文本。"""
    return (
        f"omp MCP client 配置格式尚未实测，无法自动写入。请手动添加：\n"
        f"1. 在 omp 的 MCP client 配置中新增一个 server（名称 {SERVER_NAME}）；\n"
        f'2. 命令填 "python3"，参数填 ["{script_path}"]；\n'
        f"3. 如需超时设置，配置启动超时 30 秒；\n"
        f"4. 重启 omp 会话使其生效，然后用 tools/list 确认 {SERVER_NAME} 的 8 个工具已加载。"
    )


# --- 备份 / 文件编辑（纯函数） ---

def backup_path(target: Path, ts: str | None = None) -> Path:
    """备份路径：<原名>.bak-agentmcp-<ts>。"""
    stamp = ts or time.strftime("%Y%m%dT%H%M%S")
    return target.with_name(target.name + f"{BACKUP_SUFFIX}{stamp}")


def has_section(text: str, section: str) -> bool:
    """text 中是否存在 [section] 顶层表。"""
    return re.search(rf"(?m)^\[\s*{re.escape(section)}\s*\]\s*$", text) is not None


def find_legacy_section(text: str) -> bool:
    """codex config.toml 是否存在旧 [mcp_servers.grok-cli]。"""
    return has_section(text, f"mcp_servers.{LEGACY_NAME}")


def remove_legacy_section(text: str) -> str:
    """移除旧 [mcp_servers.grok-cli] 块（到下一个顶层表或文件末尾）。"""
    pattern = re.compile(
        rf"(?ms)^\[\s*mcp_servers\.{re.escape(LEGACY_NAME)}\s*\]\s*(?:(?!^\[)[^\n]*\n?)*"
    )
    return pattern.sub("", text)


def apply_codex_install(text: str, snippet: str) -> tuple[str, str]:
    """把 snippet 追加到 codex config.toml；已有 [mcp_servers.agent-mcp] 时返回 (原文本, "skip")。"""
    if has_section(text, f"mcp_servers.{SERVER_NAME}"):
        return text, "skip"
    if text and not text.endswith("\n"):
        text += "\n"
    if text and not text.endswith("\n\n"):
        text += "\n"
    return text + snippet.rstrip() + "\n", "append"


def apply_claude_install(config: dict[str, Any], script_path: str) -> dict[str, Any]:
    """合并 agent-mcp 注册进 claude 配置，保留其他 mcpServers 与顶层键。"""
    servers = dict(config.get("mcpServers", {}))
    servers[SERVER_NAME] = claude_registration_json(script_path)["mcpServers"][SERVER_NAME]
    out = dict(config)
    out["mcpServers"] = servers
    return out


# --- 安装执行 ---

def default_paths() -> dict[str, Path]:
    """各 host 的默认配置文件路径（claude 可用 --claude-config 覆盖）。"""
    home = Path.home()
    return {
        "codex": home / ".codex" / "config.toml",
        "claude": home / ".claude.json",
    }


def _write_with_backup(cfg: Path, content: str) -> str:
    """写文件前备份原文件，返回备份路径（原文件不存在则不备份）。"""
    bak = backup_path(cfg)
    if cfg.exists():
        shutil.copy2(cfg, bak)
        return str(bak)
    return ""


def _install_codex(cfg: Path, script_path: str, *, dry_run: bool,
                   remove_legacy: bool) -> list[str]:
    logs: list[str] = []
    text = cfg.read_text() if cfg.exists() else ""
    snippet = codex_registration_toml(script_path)
    changed = False

    if find_legacy_section(text):
        logs.append(f"[deprecated] 检测到旧 [mcp_servers.{LEGACY_NAME}]（grok-cli MCP v1），"
                    f"工具已改名，建议删除；--remove-legacy 自动移除")
        if remove_legacy:
            text = remove_legacy_section(text)
            logs.append(f"已移除 [mcp_servers.{LEGACY_NAME}]")
            changed = True

    new_text, action = apply_codex_install(text, snippet)
    if action == "append":
        logs.append(f"将追加 [mcp_servers.{SERVER_NAME}] 注册（command=python3, args=[{script_path}]）")
        changed = True
    else:
        logs.append(f"[mcp_servers.{SERVER_NAME}] 已存在，跳过注册（保留现配置）")

    if not changed:
        return logs  # 无任何变更，不备份不写
    if dry_run:
        logs.append(f"[dry-run] 目标文件 {cfg}；本次不会写入")
        return logs

    bak = _write_with_backup(cfg, new_text)
    if bak:
        logs.append(f"备份 → {bak}")
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(new_text)
    logs.append(f"已写入 {cfg}")
    return logs


def _install_claude(cfg: Path, script_path: str, *, dry_run: bool) -> list[str]:
    logs: list[str] = []
    if cfg.exists():
        try:
            config = json.loads(cfg.read_text())
        except (OSError, json.JSONDecodeError) as e:
            return [f"错误：无法解析 {cfg}（{e}），未做任何修改。"]
        if not isinstance(config, dict):
            return [f"错误：{cfg} 顶层不是 JSON 对象，未做任何修改。"]
    else:
        config = {}
    updated = apply_claude_install(config, script_path)

    if dry_run:
        logs.append(f"[dry-run] 将更新 {cfg} 的 mcpServers.{SERVER_NAME}")
        logs.append(json.dumps(updated["mcpServers"][SERVER_NAME], ensure_ascii=False, indent=2))
        return logs

    bak = _write_with_backup(cfg, json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
    if bak:
        logs.append(f"备份 → {bak}")
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
    logs.append(f"已写入 {cfg}")
    return logs


def install_host(host: str, script_path: str, paths: dict[str, Path],
                 *, dry_run: bool = False, remove_legacy: bool = False) -> list[str]:
    """注册到单个 host，返回操作日志行。dry_run 时不写任何文件。"""
    if host == "codex":
        return _install_codex(paths["codex"], script_path, dry_run=dry_run,
                              remove_legacy=remove_legacy)
    if host == "claude":
        return _install_claude(paths["claude"], script_path, dry_run=dry_run)
    if host == "omp":
        return [omp_registration(script_path)]
    raise ValueError(f"未知 host: {host}")


# --- 回滚 ---

def rollback(paths: dict[str, Path], host: str | None = None) -> list[str]:
    """从最新备份恢复配置；恢复后删除备份。host=None 时恢复全部。"""
    logs: list[str] = []
    for h in HOSTS:
        if h == "omp" or (host and h != host):
            continue  # omp 无自动写入，无备份
        cfg = paths[h]
        backups = sorted(cfg.parent.glob(cfg.name + f"{BACKUP_SUFFIX}*"))
        if not backups:
            logs.append(f"[{h}] 未找到 {cfg.name}{BACKUP_SUFFIX}* 备份，跳过")
            continue
        latest = backups[-1]
        cfg.write_text(latest.read_text())
        logs.append(f"[{h}] 已从 {latest.name} 恢复 {cfg}")
        latest.unlink()
        logs.append(f"[{h}] 已删除备份 {latest.name}")
    return logs


# --- 工具名映射表输出 ---

def legacy_tool_map_text() -> str:
    lines = ["旧 9 工具 → 新 8 工具映射（breaking change；旧 skill/提示词按此迁移）", ""]
    for entry in LEGACY_TOOL_MAP:
        lines.append(f"{entry['old']:28s} → {entry['new']}")
        lines.append(f"{'':28s}    {entry['note']}")
    return "\n".join(lines)


# --- CLI ---

def default_script_path() -> str:
    """默认 mcp_server.py：脚本所在目录。"""
    return str(Path(__file__).resolve().parent / "mcp_server.py")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Agent MCP 安装/迁移：三主载体（codex/claude/omp）注册 mcp_server.py。"
                    "写配置前自动备份（*.bak-agentmcp-<ts>），--rollback 可恢复。",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--install", action="store_true",
                      help="注册 mcp_server.py 到指定 host（默认 all）")
    mode.add_argument("--rollback", action="store_true",
                      help="从最新备份恢复配置（--host 过滤）")
    mode.add_argument("--legacy-map", action="store_true",
                      help="打印旧 9 工具 → 新 8 工具映射表")
    parser.add_argument("script_path", nargs="?", default=None,
                        help="mcp_server.py 路径（默认：脚本所在目录）")
    parser.add_argument("--host", choices=[*HOSTS, "all"], default="all",
                        help="目标 host（默认 all；rollback 时同样生效）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将做的变更，不写任何文件")
    parser.add_argument("--remove-legacy", action="store_true",
                        help="同时移除旧 [mcp_servers.grok-cli] 注册")
    parser.add_argument("--claude-config", default=None,
                        help="claude 配置文件路径（默认 ~/.claude.json；"
                             "也可指向项目 .mcp.json）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.legacy_map:
        print(legacy_tool_map_text())
        return 0

    if args.rollback:
        paths = default_paths()
        if args.claude_config:
            paths["claude"] = Path(args.claude_config)
        print("\n".join(rollback(paths, host=None if args.host == "all" else args.host)))
        return 0

    if not args.install:
        print("未指定模式；可用 --install / --rollback / --legacy-map。", file=sys.stderr)
        return 2

    script = Path(args.script_path or default_script_path()).resolve()
    if not script.exists():
        print(f"错误：{script} 不存在。", file=sys.stderr)
        return 1

    paths = default_paths()
    if args.claude_config:
        paths["claude"] = Path(args.claude_config)
    hosts: list[str] = list(HOSTS) if args.host == "all" else [args.host]
    for h in hosts:
        logs = install_host(h, str(script), paths,
                            dry_run=args.dry_run, remove_legacy=args.remove_legacy)
        print(f"== [{h}] ==")
        print("\n".join(logs))
    if args.dry_run:
        print("（dry-run：以上均为将要执行的变更，未写任何文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
