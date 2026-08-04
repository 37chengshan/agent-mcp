"""配套 skill 测试：SKILL.md 完整性 + 内置 agent 预设存在性与去模型化。"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TOOLS = ("spawn_agent", "send_message", "steer_agent", "followup_task", "wait_agent",
         "interrupt_agent", "list_agents", "get_agent_activity", "get_token_usage")

AGENT_NAMES = ("planner", "architect", "code-reviewer", "security-reviewer",
               "tdd-guide", "build-error-resolver", "e2e-runner",
               "refactor-cleaner", "doc-updater", "code-explorer")


def test_skill_docs_exist_and_complete():
    skill = (PROJECT_ROOT / "skill" / "SKILL.md").read_text(encoding="utf-8")
    for tool in TOOLS:
        assert tool in skill
    assert "target_cli" in skill
    for cli in ("claude", "grok", "opencode", "omp", "atomcode"):
        assert cli in skill
    assert "deepseek-v4-flash" in skill  # AtomCode task-only one-shot 指引


def test_builtin_agents_exist():
    agents = {p.stem for p in (PROJECT_ROOT / "skill" / "agents").glob("*.md")}
    for name in AGENT_NAMES:
        assert name in agents


def test_builtin_agents_have_no_model_pinning():
    for p in (PROJECT_ROOT / "skill" / "agents").glob("*.md"):
        text = p.read_text(encoding="utf-8").lower()
        assert "model" not in text.split("---")[2]  # 正文无 model 指定
