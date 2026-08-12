#!/usr/bin/env bash
# Agent MCP 一键安装脚本（curl | bash 友好，POSIX sh 可运行）
#
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/37chengshan/agent-mcp/main/install.sh | bash
#
# 行为：
#   1. 下载/克隆项目到 ${AGENT_MCP_DIR:-$HOME/.agent-mcp}
#   2. 运行 install.py --install --host all（自动注册六个 host 的 MCP + skill，写配置前自动备份）
#   3. 安装完成后提示是否 star（GitHub CLI 已登录则直接 gh repo star，否则打开浏览器）
#
# 环境变量：
#   AGENT_MCP_DIR   安装目录（默认 $HOME/.agent-mcp）
#   AGENT_MCP_HOST  目标 host（默认 all；可选 codex/claude/omp/opencode/kimi/zcode）
#   AGENT_MCP_NO_STAR  非空则安装后不提示 star

set -u

GITHUB_REPO="37chengshan/agent-mcp"
GITHUB_RAW="https://raw.githubusercontent.com/${GITHUB_REPO}/main"
GITHUB_STAR_URL="https://github.com/${GITHUB_REPO}/stargazers"
INSTALL_DIR="${AGENT_MCP_DIR:-$HOME/.agent-mcp}"
TARGET_HOST="${AGENT_MCP_HOST:-all}"

say() { printf '%s\n' "$*"; }
die() { say "错误: $*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "需要 python3（>=3.9），请先安装 Python。"

# --- 1. 获取项目文件 ---
if [ -f "$INSTALL_DIR/install.py" ]; then
  say "已存在 $INSTALL_DIR，尝试更新…"
  if command -v git >/dev/null 2>&1 && [ -d "$INSTALL_DIR/.git" ]; then
    (cd "$INSTALL_DIR" && git pull --ff-only) >/dev/null 2>&1 \
      || say "git pull 失败，继续使用现有文件。"
  fi
else
  say "下载 agent-mcp 到 $INSTALL_DIR …"
  mkdir -p "$INSTALL_DIR"
  if command -v git >/dev/null 2>&1; then
    git clone --depth 1 "https://github.com/${GITHUB_REPO}.git" "$INSTALL_DIR" >/dev/null 2>&1 \
      || die "git clone 失败。"
  else
    # 无 git 时退回 curl 下载 tarball
    command -v curl >/dev/null 2>&1 || die "需要 git 或 curl。"
    tmp="$(mktemp -d)"
    curl -fsSL "${GITHUB_RAW}.tar.gz" -o "$tmp/repo.tar.gz" || die "下载失败。"
    tar -xzf "$tmp/repo.tar.gz" -C "$tmp"
    shopt -s nullglob dotglob
    src=("$tmp"/*)
    shopt -u nullglob dotglob
    [ -d "${src[0]}" ] || die "解压失败。"
    mv "${src[0]}"/* "$INSTALL_DIR"/ 2>/dev/null || true
    mv "${src[0]}"/.* "$INSTALL_DIR"/ 2>/dev/null || true
    rm -rf "$tmp"
  fi
  [ -f "$INSTALL_DIR/install.py" ] || die "项目文件不完整，请重试。"
fi

# --- 2. 运行安装 ---
say "== 安装 host: $TARGET_HOST =="
(cd "$INSTALL_DIR" && python3 install.py --install --host "$TARGET_HOST")
rc=$?
if [ "$rc" -ne 0 ]; then
  say "安装未完成（退出码 $rc）。可尝试 --dry-run 排查："
  say "  cd $INSTALL_DIR && python3 install.py --install --host $TARGET_HOST --dry-run"
  exit "$rc"
fi

# --- 3. star 提示 ---
if [ -z "${AGENT_MCP_NO_STAR:-}" ]; then
  say ""
  say "安装完成！如果觉得有用，欢迎给 ${GITHUB_REPO} 点个 star ⭐"
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    gh repo star "$GITHUB_REPO" >/dev/null 2>&1 \
      && say "已通过 GitHub CLI 点亮 star ⭐" \
      || say "GitHub CLI 已登录但 star 失败（可能已 star），可手动访问：$GITHUB_STAR_URL"
  else
    say "未检测到已登录的 GitHub CLI，将尝试打开浏览器：$GITHUB_STAR_URL"
    case "$(uname -s)" in
      Darwin) open "$GITHUB_STAR_URL" >/dev/null 2>&1 || true ;;
      Linux)  xdg-open "$GITHUB_STAR_URL" >/dev/null 2>&1 || true ;;
      *)      say "请手动打开：$GITHUB_STAR_URL" ;;
    esac
  fi
fi

say ""
say "启动监控页（可选）：cd $INSTALL_DIR && python3 start_agent_mcp.py --open"
say "更多说明：https://github.com/${GITHUB_REPO}#readme"
