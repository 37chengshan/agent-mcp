from __future__ import annotations
import json
import shutil
from pathlib import Path
from typing import Any

HOME = Path.home()


class BaseAdapter:
    cli_name = ""
    def build_command(self, *, prompt: str, cwd: str, model: str | None,
                      permission_mode: str, max_turns: int, resume: str | None) -> list[str]:
        raise NotImplementedError
    def parse_stream(self, lines: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """返回 (规范化事件列表, 累计 usage dict)"""
        raise NotImplementedError
    def extract_session_id(self, raw: dict) -> str | None:
        return None
    def binary(self) -> str | None:
        return None


class ClaudeAdapter(BaseAdapter):
    cli_name = "claude"
    _BIN = ["claude", str(HOME / ".local/bin/claude")]
    PERMISSION_FLAGS = {
        "plan": ["--permission-mode", "plan"],
        "acceptEdits": ["--permission-mode", "acceptEdits"],
        "fullAccess": ["--dangerously-skip-permissions"],
    }
    def binary(self) -> str | None:
        for cand in self._BIN:
            found = shutil.which(cand)
            if found:
                return found
        return None
    def build_command(self, *, prompt, cwd, model=None, permission_mode="plan",
                      max_turns=8, resume=None) -> list[str]:
        cmd = [self.binary(), "-p", "--output-format", "stream-json", "--verbose",
               "--cwd", str(cwd), "--max-turns", str(max_turns)]
        cmd += self.PERMISSION_FLAGS.get(permission_mode, self.PERMISSION_FLAGS["plan"])
        if model:
            cmd += ["--model", model]
        if resume:
            cmd += ["--resume", resume]
        cmd.append(prompt)
        return cmd
    def parse_stream(self, lines) -> tuple[list[dict], dict]:
        events: list[dict] = []
        usage: dict[str, Any] = {}
        seen_ids: set[str] = set()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            typ = raw.get("type")
            if typ == "assistant" and isinstance(raw.get("message"), dict):
                msg = raw["message"]
                mid = msg.get("id")
                if mid and mid not in seen_ids:
                    seen_ids.add(mid)
                    if isinstance(msg.get("usage"), dict):
                        u = msg["usage"]
                        usage = _merge_usage(usage, {
                            "input_tokens": u.get("input_tokens", 0),
                            "output_tokens": u.get("output_tokens", 0),
                            "cache_creation": u.get("cache_creation_input_tokens", 0),
                            "cache_read": u.get("cache_read_input_tokens", 0),
                            "cost_usd": 0.0,
                        })
                events.append({"type": "agent.message",
                               "payload": {"text": msg.get("content", "")}})
            elif typ == "result" and isinstance(raw.get("result"), dict):
                # result.usage 是会话最终权威值，直接覆盖（而非累加）
                res = raw["result"]
                u = res.get("usage") or {}
                usage = {
                    "input_tokens": u.get("input_tokens", 0),
                    "output_tokens": u.get("output_tokens", 0),
                    "cache_creation": u.get("cache_creation_input_tokens", 0),
                    "cache_read": u.get("cache_read_input_tokens", 0),
                    "cost_usd": res.get("total_cost_usd", 0.0) or 0.0,
                }
                events.append({"type": "agent.usage", "payload": dict(usage)})
                sid = res.get("session_id")
                if sid:
                    events.append({"type": "agent.terminated",
                                   "payload": {"stop_reason": res.get("stop_reason", "end_turn"),
                                               "session_id": sid}})
        return events, usage


def _merge_usage(base: dict, add: dict) -> dict:
    out = dict(base)
    for k, v in add.items():
        out[k] = out.get(k, 0) + (v if isinstance(v, (int, float)) else 0)
    return out


_CLAUDE = ClaudeAdapter()
_ADAPTERS: dict[str, BaseAdapter] = {"claude": _CLAUDE}


def get_adapter(name: str) -> BaseAdapter:
    if name not in _ADAPTERS:
        raise ValueError(f"unknown target_cli: {name}")
    return _ADAPTERS[name]


def register_adapter(adapter: BaseAdapter) -> None:
    _ADAPTERS[adapter.cli_name] = adapter
