"""P2 测试共享 fake：可注入的 Dispatcher 替身（不启进程、不碰真实适配器）。"""

from __future__ import annotations

from typing import Any


class FakeDispatcher:
    """记录 spawn/followup 调用并可手动翻转 agent 状态；spawn 返回递增 agent_id。"""

    def __init__(self):
        self.spawned: list[tuple[int, dict[str, Any]]] = []
        self.followups: list[dict[str, Any]] = []
        self._agents: dict[int, str] = {}
        self._next = 1
        self.raise_on_followup: Exception | None = None

    def spawn(self, params: dict[str, Any]) -> dict[str, Any]:
        aid = self._next
        self._next += 1
        self._agents[aid] = "running"
        self.spawned.append((aid, dict(params)))
        return {"agent_id": aid, "status": "running"}

    def followup(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.raise_on_followup is not None:
            raise self.raise_on_followup
        p = dict(params)
        aid = p.get("agent_id")
        if p.get("interrupt"):
            self._agents[aid] = "running"
        self.followups.append(p)
        return {"agent_id": aid, "status": "running"}

    def list_agents(self, _body: dict[str, Any] | None = None) -> dict[str, Any]:
        # 与真实 Dispatcher.list_agents 同形状：{"agents": [...]}
        return {"agents": [{"id": aid, "status": st} for aid, st in sorted(self._agents.items())]}

    def mark_terminal(self, agent_id: int, status: str = "terminated") -> None:
        self._agents[agent_id] = status