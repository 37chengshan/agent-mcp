"""DSH bundle 插件（packages/dsh-plugin）发布契约守卫。

单一事实来源：packages/dsh-plugin/（package.json + cordis.patch.yml + scripts/）。
examples/dsh/agentmcp.cordis.yml 仅作手工 fallback 摘录，不再单独维护完整字段。

本文件锁死四件事：
1. package.json 的 dsh.bundle.patch 非空且文件真实存在（npm tarball 可被 DSH 加载）
2. cordis.patch.yml 声明 serverName: agentmcp 与 @deepseek-ai/dsh-mcp-client
3. 插件载荷不含写死的用户主路径（/Users/<name>/...），换机器可直接安装
4. examples 手工 fallback 与 packages 单一来源的关系被显式声明（或关键字段一致）
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "packages" / "dsh-plugin"
PACKAGE_JSON = PKG / "package.json"
CORDIS_PATCH = PKG / "cordis.patch.yml"
LAUNCHER = PKG / "scripts" / "launch-agentmcp.mjs"
EXAMPLE_PATCH = ROOT / "examples" / "dsh" / "agentmcp.cordis.yml"

# 写死用户主路径：/Users/<name>/ 且 name 不是 <you> 之类占位符
_HARDCODED_HOME_RE = re.compile(r"/Users/(?!<)[A-Za-z0-9._-]+/")


def _package_json() -> dict:
    return json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))


def _plugin_texts() -> list[tuple[pathlib.Path, str]]:
    """插件载荷里应保持可移植的文本文件（不含 png）。"""
    texts: list[tuple[pathlib.Path, str]] = []
    for path in sorted(PKG.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() in {".png", ".gif", ".jpg", ".jpeg", ".webp", ".ico"}:
            continue
        try:
            texts.append((path, path.read_text(encoding="utf-8")))
        except UnicodeDecodeError:
            continue
    return texts


# ---- 1. package.json bundle 契约 ----

def test_package_json_declares_dsh_bundle_patch():
    """dsh.bundle.patch 必须非空，否则 dsh plugin add 不会应用任何 cordis 补丁。"""
    pkg = _package_json()
    assert pkg["name"] == "dsh-plugin-agentmcp"
    patch = (pkg.get("dsh") or {}).get("bundle") or {}
    assert patch.get("patch"), "package.json 缺少 dsh.bundle.patch"


def test_bundle_patch_file_exists_and_is_shipped():
    """patch 路径必须真实存在，且被 files 白名单带上（否则 npm 包里没有它）。"""
    pkg = _package_json()
    patch_rel = pkg["dsh"]["bundle"]["patch"]
    patch_path = (PKG / patch_rel).resolve()
    assert patch_path.is_file(), f"dsh.bundle.patch 指向不存在的文件: {patch_rel}"
    assert patch_path == CORDIS_PATCH.resolve()

    files = pkg.get("files") or []
    # cordis.patch.yml 可直接列文件名，也可被 scripts/assets 目录覆盖；必须点名
    assert any(
        f in {"cordis.patch.yml", "./cordis.patch.yml"} or f.rstrip("/") == "cordis.patch.yml"
        for f in files
    ), f"files 白名单未包含 cordis.patch.yml: {files}"


def test_package_json_peer_and_engines():
    """DSH 侧依赖 @deepseek-ai/dsh-mcp-client；引擎声明 Node ≥ 18（启动器用到 node:child_process）。"""
    pkg = _package_json()
    peers = pkg.get("peerDependencies") or {}
    assert "@deepseek-ai/dsh-mcp-client" in peers
    assert (pkg.get("license") or "").upper() == "MIT"
    assert "37chengshan/agent-mcp" in json.dumps(pkg.get("repository") or {})
    node_eng = ((pkg.get("engines") or {}).get("node") or "")
    assert node_eng, "缺少 engines.node"
    # 形如 >=18 / >=18.0.0
    assert re.search(r">=\s*18(\.\d+)*", node_eng), f"engines.node 应声明 >=18，实际: {node_eng}"


# ---- 2. cordis.patch.yml 内容契约 ----

def test_cordis_patch_targets_agentmcp_and_dsh_mcp_client():
    text = CORDIS_PATCH.read_text(encoding="utf-8")
    assert "serverName: agentmcp" in text
    assert "@deepseek-ai/dsh-mcp-client" in text
    assert "transport: stdio" in text
    assert "mcp-agentmcp" in text


def test_cordis_patch_launcher_or_python_resolution():
    """必须二选一：node 启动器，或 !!js 解析 python/mcp_server.py；且超时与失败策略齐全。"""
    text = CORDIS_PATCH.read_text(encoding="utf-8")
    uses_launcher = "launch-agentmcp.mjs" in text
    uses_js_resolve = "!!js" in text
    assert uses_launcher or uses_js_resolve, "cordis.patch.yml 需用启动器或 !!js 解析路径"

    assert "failOnStartupError: false" in text
    assert "toolCallTimeoutMs: 120000" in text
    # 不写死用户主路径
    assert not _HARDCODED_HOME_RE.search(text), "cordis.patch.yml 含写死的 /Users/<name>/ 路径"


def test_launcher_script_exists_and_is_parseable():
    assert LAUNCHER.is_file(), "缺少 scripts/launch-agentmcp.mjs"
    text = LAUNCHER.read_text(encoding="utf-8")
    # 解析顺序关键词：env → 向上走 / vendored → 用户安装副本
    assert "AGENT_MCP_MCP_SERVER" in text
    assert ".agent-mcp" in text
    assert "homedir" in text or "os.homedir" in text or "HOME" in text
    assert "mcp_server.py" in text


# ---- 3. 无写死用户主路径 ----

def test_no_hardcoded_user_home_paths_in_plugin_payload():
    for path, text in _plugin_texts():
        match = _HARDCODED_HOME_RE.search(text)
        assert match is None, f"{path.relative_to(ROOT)} 含写死主路径: {match.group(0)!r}"


def test_no_hardcoded_user_home_paths_in_dsh_examples():
    assert EXAMPLE_PATCH.is_file()
    text = EXAMPLE_PATCH.read_text(encoding="utf-8")
    match = _HARDCODED_HOME_RE.search(text)
    assert match is None, f"examples 含写死主路径: {match.group(0)!r}"


# ---- 4. examples 手工 fallback 与单一来源 ----

def test_examples_declare_packages_as_single_source_or_stay_consistent():
    """examples 必须显式指向 packages/dsh-plugin，或关键字段与之保持一致。"""
    assert EXAMPLE_PATCH.is_file(), "缺少 examples/dsh/agentmcp.cordis.yml"
    example = EXAMPLE_PATCH.read_text(encoding="utf-8")
    pkg_patch = CORDIS_PATCH.read_text(encoding="utf-8")

    points_at_packages = "packages/dsh-plugin" in example
    consistent = all(
        key in example
        for key in ("serverName: agentmcp", "@deepseek-ai/dsh-mcp-client", "mcp-agentmcp")
    ) and all(key in pkg_patch for key in ("serverName: agentmcp", "@deepseek-ai/dsh-mcp-client"))

    assert points_at_packages or consistent, (
        "examples/dsh/agentmcp.cordis.yml 应注明单一事实来源 packages/dsh-plugin，"
        "或关键字段（serverName / 插件名 / id）与 packages 保持一致"
    )
    if points_at_packages:
        # 已声明单一来源时，仍不允许悄悄改掉命名空间
        assert "serverName: agentmcp" in example
        assert "@deepseek-ai/dsh-mcp-client" in example


def test_docs_lead_with_dsh_plugin_add():
    """docs/dsh-integration.md §2 以 dsh plugin add 为主路径，手工 patch 为回退。"""
    doc = (ROOT / "docs" / "dsh-integration.md").read_text(encoding="utf-8")
    assert "dsh plugin add" in doc or "dsh plugin" in doc
    assert "dsh-plugin-agentmcp" in doc
    assert "packages/dsh-plugin" in doc
