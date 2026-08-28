"""P2 DoD：bounded autonomous（Invariant 5：任一预算不突破；gate 指纹抑制）。"""

from __future__ import annotations

import pytest

from agent_mcp import models as m
from agent_mcp.db import DB
from agent_mcp.execution import ExecutionManager
from agent_mcp.store_v4 import StoreV4
from tests._v4_fakes import FakeDispatcher


@pytest.fixture()
def env(tmp_path):
    db = DB(tmp_path / "t.db")
    store = StoreV4(db)
    dispatcher = FakeDispatcher()
    em = ExecutionManager(dispatcher, store)
    return {"db": db, "store": store, "dispatcher": dispatcher, "em": em}


def _waiter(dispatcher):
    def wait_for_terminal(agent_id):
        # 真实环境是阻塞到终态；fake 里直接回到终结态
        dispatcher.mark_terminal(agent_id, "terminated")
        return {"status": "terminated", "stop_reason": "end_turn"}
    return wait_for_terminal


def test_autonomous_gate_pass_then_done(env):
    dispatcher, store, em = env["dispatcher"], env["store"], env["em"]
    gate_results = iter([(False, "check failed"), (True, "ok")])

    def run_gate(cmd, cwd):
        return next(gate_results)

    policy = m.AutonomousPolicy(max_continuations=2, max_turns=10, gates=("check",))
    run = em.autonomous_run(
        params={"target_cli": "omp", "prompt": "do it", "cwd": "/tmp", "session_id": "s1"},
        policy=policy,
        wait_for_terminal=_waiter(dispatcher),
        run_gate=run_gate,
    )
    assert run["status"] == m.RUN_COMPLETED
    assert len(dispatcher.followups) == 1  # 一次 failed-gate 续播
    assert len(dispatcher.spawned) == 1


def test_autonomous_budget_exhausted_is_incomplete(env):
    dispatcher, store, em = env["dispatcher"], env["store"], env["em"]
    calls = {"n": 0}

    def run_gate(cmd, cwd):
        calls["n"] += 1
        return False, "always fails"

    policy = m.AutonomousPolicy(max_continuations=0, max_turns=10, gates=("check",))
    run = em.autonomous_run(
        params={"target_cli": "omp", "prompt": "do it", "cwd": "/tmp", "session_id": "s1"},
        policy=policy,
        wait_for_terminal=_waiter(dispatcher),
        run_gate=run_gate,
    )
    assert run["status"] == m.RUN_INCOMPLETE
    assert run["stop_reason"] == "budget_exhausted"
    assert calls["n"] == 1
    assert len(dispatcher.followups) == 0


def test_autonomous_same_failure_not_repeat(env):
    dispatcher, store, em = env["dispatcher"], env["store"], env["em"]
    calls = {"n": 0}

    def run_gate(cmd, cwd):
        calls["n"] += 1
        return False, "still fails"

    # 工作区指纹恒定：同一失败 gate 只跑一次，之后 suppression → INCOMPLETE
    policy = m.AutonomousPolicy(max_continuations=5, max_turns=10, gates=("check",))
    run = em.autonomous_run(
        params={"target_cli": "omp", "prompt": "do it", "cwd": "/tmp", "session_id": "s1"},
        policy=policy,
        wait_for_terminal=_waiter(dispatcher),
        run_gate=run_gate,
        workspace_fp_of=lambda gate: "workspace-fp",
    )
    assert run["status"] == m.RUN_INCOMPLETE
    assert calls["n"] == 1  # 不重跑同一失败 gate
    assert len(dispatcher.followups) == 1  # 首次失败允许一次修复续播，之后工作区未变 → 停止

