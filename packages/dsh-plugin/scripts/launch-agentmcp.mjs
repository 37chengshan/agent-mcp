#!/usr/bin/env node
/**
 * agent-mcp MCP stdio 启动器（供 DSH @deepseek-ai/dsh-mcp-client 调用）。
 *
 * 职责：跨平台挑 Python 解释器 + 定位 mcp_server.py，再以 stdio spawn 透传。
 * 比在 cordis.patch.yml 里写死 python3 / 绝对路径可靠——换机器、换安装位置无需改 patch。
 *
 * mcp_server.py 解析顺序：
 *   1. 环境变量 AGENT_MCP_MCP_SERVER
 *   2. 从本包目录向上走（monorepo 仓库根 / vendored 邻接副本 agent-mcp/mcp_server.py）
 *   3. ~/.agent-mcp/mcp_server.py（install.py 安装副本）
 *   4. 失败：stderr 清晰报错并 exit 1
 *      （failOnStartupError=false 时 DSH 不阻塞插件激活，重连循环仍会重试）
 *
 * 不写死任何用户主路径；解释器候选见 findPython()。
 */
import { spawn, spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const PACKAGE_DIR = path.dirname(path.dirname(fileURLToPath(import.meta.url)))

/** 挑一个可执行的 Python：win32 优先 py launcher，类 Unix 优先 python3。 */
function findPython() {
  const candidates =
    process.platform === 'win32'
      ? ['py', 'python', 'python3']
      : ['python3', 'python', 'py']
  for (const cmd of candidates) {
    try {
      const probe = spawnSync(cmd, ['-c', 'import sys; print(sys.version_info[0])'], {
        encoding: 'utf8',
        timeout: 5000,
        windowsHide: true,
      })
      // 至少能跑起来；agent-mcp 要求 3.10+，过旧的在这里不硬拦（mcp_server 会再校验）
      if (probe.status === 0) return cmd
    } catch {
      // 命令不在 PATH，试下一个候选
    }
  }
  return null
}

/** 按序定位 mcp_server.py，找不到返回 null。 */
function findServer() {
  const fromEnv = process.env.AGENT_MCP_MCP_SERVER
  if (fromEnv && existsSync(fromEnv)) return fromEnv

  // 从包目录向上找：packages/dsh-plugin → packages → 仓库根（mcp_server.py 在根）
  let dir = PACKAGE_DIR
  for (let i = 0; i < 6; i++) {
    const atDir = path.join(dir, 'mcp_server.py')
    if (existsSync(atDir)) return atDir
    // vendored 邻接副本（包旁边就放一份 agent-mcp）
    const vendored = path.join(dir, 'agent-mcp', 'mcp_server.py')
    if (existsSync(vendored)) return vendored
    const parent = path.dirname(dir)
    if (parent === dir) break
    dir = parent
  }

  // 用户安装副本（install.py / install.sh 落点）
  const homeCopy = path.join(os.homedir(), '.agent-mcp', 'mcp_server.py')
  if (existsSync(homeCopy)) return homeCopy

  return null
}

function fail(message) {
  process.stderr.write(`[dsh-plugin-agentmcp] ${message}\n`)
  process.exit(1)
}

const python = findPython()
if (!python) {
  fail(
    '未找到 Python 解释器（试过 python3 / python / py）。\n' +
      '  请安装 Python ≥ 3.10 并确保其在 PATH 中。'
  )
}

const server = findServer()
if (!server) {
  fail(
    '未找到 mcp_server.py。按以下任一方式提供后重试：\n' +
      '  1) 设置环境变量 AGENT_MCP_MCP_SERVER=/path/to/mcp_server.py\n' +
      '  2) 把 agent-mcp 仓库放在本包上级目录（monorepo 根可见 mcp_server.py）\n' +
      '  3) 安装用户副本到 ~/.agent-mcp/（仓库根执行 python3 install.py）\n' +
      `  搜索起点：${PACKAGE_DIR}`
  )
}

const child = spawn(python, [server], {
  stdio: 'inherit',
  env: process.env,
  windowsHide: true,
})

// 把宿主信号转给 mcp_server 子进程，避免 DSH 断开时留下孤儿
for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
  process.on(sig, () => {
    if (!child.killed) child.kill(sig)
  })
}

child.on('error', (err) => {
  fail(`spawn ${python} 失败：${err.message}`)
})

child.on('exit', (code, signal) => {
  if (signal) {
    // 跟子进程同归于尽，让 DSH 的重连循环按信号语义处理
    process.kill(process.pid, signal)
    return
  }
  process.exit(code ?? 0)
})
