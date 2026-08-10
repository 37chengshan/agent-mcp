"""agent-mcp 共享常量。"""

# session 不匹配错误的触发短语：daemon 错误文案与 MCP 层检测共用一个来源，
# 避免任一侧改写后另一侧静默失效（echo 空转复现）。
SESSION_MISMATCH_MARK = "does not belong to session"
