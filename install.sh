#!/usr/bin/env bash
# Agent MCP 一键安装脚本（curl | bash 友好，POSIX sh 可运行）
#
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/37chengshan/agent-mcp/main/install.sh | bash
#
# 行为：
#   1. 下载/克隆项目到 ${AGENT_MCP_DIR:-$HOME/.agent-mcp}
#   2. 选择要写入的 host（默认**不自动全装**）：
#      - 已设置 AGENT_MCP_HOST（如 codex,claude）→ 只装指定 host
#      - 交互终端 → 菜单多选
#      - 非交互管道且未指定 → 不写任何配置，提示用户显式指定
#   3. 安装完成后**询问用户**是否 star（同意才执行；非交互默认跳过）
#
# 环境变量：
#   AGENT_MCP_DIR   安装目录（默认 $HOME/.agent-mcp）
#   AGENT_MCP_HOST  要写入的 host，逗号分隔（如 codex,claude；可选 codex/claude/omp/opencode/kimi/zcode）
#   AGENT_MCP_NO_STAR  非空则安装后不询问 star

set -u

GITHUB_REPO="37chengshan/agent-mcp"
GITHUB_RAW="https://raw.githubusercontent.com/${GITHUB_REPO}/main"
# 点星/收藏用仓库首页（不是 /stargazers）
GITHUB_STAR_URL="https://github.com/${GITHUB_REPO}"
INSTALL_DIR="${AGENT_MCP_DIR:-$HOME/.agent-mcp}"
# 可写入的 host 全集（install.py 同口径）
AVAILABLE_HOSTS="codex claude omp opencode kimi zcode grok cursor gemini pi copilot cline qwen devin windsurf amazon-q atomcode kiro goose hermes crush"

say() { printf '%s\n' "$*"; }
die() { say "错误: $*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "需要 python3（>=3.9），请先安装 Python。"

# --- 校验：可选 SHA-256（上游有 checksum 文件则强制；缺失则警告不阻断） ---
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    printf ''
  fi
}

verify_tarball_sha256() {
  # $1=tarball  $2=optional checksum file path
  local sum tool_sum
  tool_sum="$(sha256_of "$1")"
  [ -n "$tool_sum" ] || { say "警告: 本机无 sha256sum/shasum，跳过归档校验。"; return 0; }
  if [ ! -f "$2" ]; then
    say "警告: 未找到校验文件（$2），跳过 SHA-256 校验。"
    say "  可手动核对: sha256sum $1"
    return 0
  fi
  # checksum 文件格式：`<hex>  <filename>` 或纯 hex
  sum="$(awk 'NF{print $1; exit}' "$2")"
  if [ -z "$sum" ]; then
    say "警告: 校验文件为空，跳过 SHA-256 校验。"
    return 0
  fi
  if [ "$sum" != "$tool_sum" ]; then
    die "SHA-256 校验失败: 期望 $sum，实际 $tool_sum"
  fi
  say "SHA-256 校验通过: $tool_sum"
}

# --- 1. 获取项目文件 ---
# 下载函数：codeload tarball（git 不可用或 clone 失败时的回退通道）
fetch_tarball() {
  command -v curl >/dev/null 2>&1 || die "需要 git 或 curl。"
  tmp="$(mktemp -d)"
  curl -fsSL "https://codeload.github.com/${GITHUB_REPO}/tar.gz/refs/heads/main" \
    -o "$tmp/repo.tar.gz" || die "下载失败。"
  # 可选校验：AGENT_MCP_CHECKSUM 提供本地文件路径，或尝试拉远端 .sha256
  CHECKSUM_SRC="${AGENT_MCP_CHECKSUM:-}"
  if [ -z "$CHECKSUM_SRC" ]; then
    if curl -fsSL "https://raw.githubusercontent.com/${GITHUB_REPO}/main/release/agent-mcp-main.tar.gz.sha256" \
         -o "$tmp/repo.tar.gz.sha256" 2>/dev/null; then
      CHECKSUM_SRC="$tmp/repo.tar.gz.sha256"
    fi
  fi
  verify_tarball_sha256 "$tmp/repo.tar.gz" "$CHECKSUM_SRC"
  tar -xzf "$tmp/repo.tar.gz" -C "$tmp" || die "解压失败。"
  # POSIX sh：解压顶层应为唯一目录（<repo>-<ref>），取第一个
  found=""
  for d in "$tmp"/*/; do
    [ -d "$d" ] || continue
    found="$d"
    break
  done
  [ -n "$found" ] || die "解压失败。"
  cp -R "$found/." "$INSTALL_DIR"/ || die "拷贝项目文件失败。"
  rm -rf "$tmp"
}

if [ -f "$INSTALL_DIR/install.py" ]; then
  say "已存在 ${INSTALL_DIR}，尝试更新…"
  if command -v git >/dev/null 2>&1 && [ -d "$INSTALL_DIR/.git" ]; then
    (cd "$INSTALL_DIR" && git pull --ff-only) >/dev/null 2>&1 \
      || say "git pull 失败，继续使用现有文件。"
  fi
else
  say "下载 agent-mcp 到 ${INSTALL_DIR} …"
  # 仅当目录原先不存在时才允许后续失败清理；已有目录绝不清空（防误删用户数据）
  DIR_PREEXISTED=0
  [ -e "$INSTALL_DIR" ] && DIR_PREEXISTED=1
  mkdir -p "$INSTALL_DIR"
  if command -v git >/dev/null 2>&1; then
    if git clone --depth 1 "https://github.com/${GITHUB_REPO}.git" "$INSTALL_DIR" >/dev/null 2>&1; then
      :
    else
      # git clone 失败（网络/代理/证书常见）→ 自动回退归档下载，不中断安装
      say "git clone 失败，自动改用归档下载…"
      if [ "$DIR_PREEXISTED" -eq 0 ]; then
        # 只清理本脚本刚创建的空目录；已存在目录直接拒绝覆盖
        case "$INSTALL_DIR" in
          "$HOME"/*|/tmp/*|/var/tmp/*) rm -rf "$INSTALL_DIR" ;;
          *) die "拒绝清理非预期安装目录: ${INSTALL_DIR}（请手动处理后重试）" ;;
        esac
        mkdir -p "$INSTALL_DIR"
      else
        if [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
          die "目标目录已存在且非空，拒绝覆盖: ${INSTALL_DIR}（请清空或换 AGENT_MCP_DIR）"
        fi
      fi
      fetch_tarball
    fi
  else
    # 无 git 时直接走归档下载
    say "未检测到 git，使用归档下载…"
    fetch_tarball
  fi
  [ -f "$INSTALL_DIR/install.py" ] || die "项目文件不完整，请重试。"
fi

# --- 2. 选择要写入的 host ---
# 优先级：AGENT_MCP_HOST 显式指定 > 交互菜单多选 > 非交互未指定则中止（不自动全装）
select_hosts() {
  if [ -n "${AGENT_MCP_HOST:-}" ]; then
    # 逗号/空格分隔 → 逐个校验
    TARGET_HOSTS="$(printf '%s' "$AGENT_MCP_HOST" | tr ',' ' ')"
    for h in $TARGET_HOSTS; do
      case " $AVAILABLE_HOSTS " in
        *" $h "*) : ;;
        *) die "未知 host: ${h}（可选：${AVAILABLE_HOSTS}）" ;;
      esac
    done
    return
  fi
  if [ ! -t 0 ]; then
    # 非交互管道（curl | bash）且未指定：不写任何配置，提示用户显式指定
    say ""
    say "未指定要写入的 host，为避免自动全装，本次不注册任何 host。"
    say "请显式指定后重跑，例如："
    say "  AGENT_MCP_HOST=codex,claude curl -fsSL https://raw.githubusercontent.com/${GITHUB_REPO}/main/install.sh | bash"
    say "可选的 host：${AVAILABLE_HOSTS}"
    exit 0
  fi
  # 交互终端：菜单多选
  say ""
  say "选择要注册 MCP + skill 的 host（可多选，逗号或空格分隔，或输入 all）："
  i=1
  for h in $AVAILABLE_HOSTS; do
    say "  [$i] $h"
    i=$((i + 1))
  done
  say "  [0] 全部（all）"
  printf '请输入（默认不注册，直接回车跳过）: '
  read -r choice || exit 0
  case "$choice" in
    "" ) TARGET_HOSTS="" ;;
    all | 0 ) TARGET_HOSTS="$AVAILABLE_HOSTS" ;;
    * )
      # 数字或名字混合解析（先把逗号/空格归一为逗号再逐段匹配）
      norm="$(printf '%s' "$choice" | tr ' ,' ',')"
      TARGET_HOSTS=""
      i=1
      for h in $AVAILABLE_HOSTS; do
        case ",${norm}," in
          *",${i},"* | *",${h},"* ) TARGET_HOSTS="$TARGET_HOSTS $h" ;;
        esac
        i=$((i + 1))
      done
      [ -n "$TARGET_HOSTS" ] || say "未识别到有效 host，本次不注册任何 host。"
      ;;
  esac
}

select_hosts

# --- 3. 运行安装（逐个 host） ---
if [ -n "${TARGET_HOSTS:-}" ]; then
  for host in $TARGET_HOSTS; do
    say "== 安装 host: ${host} =="
    (cd "$INSTALL_DIR" && python3 install.py --install --host "$host")
    rc=$?
    if [ "$rc" -ne 0 ]; then
      say "安装未完成（退出码 ${rc}）。可尝试 --dry-run 排查："
      say "  cd $INSTALL_DIR && python3 install.py --install --host ${host} --dry-run"
      exit "$rc"
    fi
  done
else
  say ""
  say "未选择任何 host，安装结束（项目文件已就绪于 ${INSTALL_DIR}）。"
  say "之后可随时执行：cd $INSTALL_DIR && python3 install.py --install --host <host>"
fi

# --- 4. star 提示（只打印链接；绝不自动 open/xdg-open/gh） ---
if [ -z "${AGENT_MCP_NO_STAR:-}" ]; then
  say ""
  say "安装完成！如果觉得有用，欢迎给 ${GITHUB_REPO} 点个 star ⭐"
  say "  ${GITHUB_STAR_URL}"
  say "（已取消自动打开浏览器 / gh star，避免定时任务反复弹页）"
fi

say ""
say "启动监控页（可选）：cd $INSTALL_DIR && python3 start_agent_mcp.py --open"
say "更多说明：https://github.com/${GITHUB_REPO}#readme"
