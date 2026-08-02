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


class GrokAdapter(ClaudeAdapter):
    cli_name = "grok"
    _BIN = ["grok", str(HOME / ".grok/bin/grok")]
    PERMISSION_FLAGS = {
        "plan": ["--permission-mode", "plan"],
        "acceptEdits": ["--permission-mode", "acceptEdits"],
        "fullAccess": ["--permission-mode", "bypassPermissions", "--always-approve"],
    }
    def build_command(self, *, prompt, cwd, model=None, permission_mode="plan",
                      max_turns=8, resume=None) -> list[str]:
        cmd = [self.binary(), "--cwd", str(cwd), "--output-format",
               "streaming-messages-json", "--max-turns", str(max_turns)]
        cmd += self.PERMISSION_FLAGS.get(permission_mode, self.PERMISSION_FLAGS["plan"])
        if model:
            cmd += ["--model", model]
        if resume:
            cmd += ["--resume", resume]
        cmd += ["--single", prompt]
        return cmd
    def parse_stream(self, lines) -> tuple[list[dict], dict]:
        # grok streaming-messages-json 实测（0.2.118）：assistant/result 行与
        # claude 同构（snake_case）；assistant.message.content 为 thinking/text 块数组
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
                               "payload": {"text": _extract_text(msg.get("content"))}})
            elif typ == "result":
                # grok 实测：usage/stop_reason/session_id/total_cost_usd 在顶层，
                # result 字段只是最终输出文本（与 claude 的嵌套 result 不同）
                res = raw
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
    def extract_session_id(self, raw: dict) -> str | None:
        # system init / assistant / result 行均带顶层 session_id（实测）
        sid = raw.get("session_id") if isinstance(raw, dict) else None
        return str(sid) if sid else None


class OpencodeAdapter(ClaudeAdapter):
    cli_name = "opencode"
    _BIN = ["opencode"]
    # opencode 无 permission-mode CLI flag（权限走配置文件 allow 规则），
    # 仅 fullAccess 对应 --dangerously-skip-permissions（实测 1.14.51）
    PERMISSION_FLAGS = {
        "fullAccess": ["--dangerously-skip-permissions"],
    }
    def build_command(self, *, prompt, cwd, model=None, permission_mode="plan",
                      max_turns=8, resume=None) -> list[str]:
        cmd = [self.binary(), "run", "--format", "json", "--dir", str(cwd)]
        cmd += self.PERMISSION_FLAGS.get(permission_mode, [])
        if model:
            cmd += ["--model", model]
        if resume:
            cmd += ["--session", resume]
        cmd.append(prompt)
        return cmd
    def parse_stream(self, lines) -> tuple[list[dict], dict]:
        # opencode run --format json 实测（1.14.51）：事件仅
        # step_start/text/tool_use/step_finish；usage 在 step_finish.part.tokens
        events: list[dict] = []
        usage: dict[str, Any] = {}
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
            part = raw.get("part") if isinstance(raw.get("part"), dict) else {}
            if typ == "text":
                events.append({"type": "agent.message",
                               "payload": {"text": part.get("text", "")}})
            elif typ == "tool_use":
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                events.append({"type": "agent.tool_use", "payload": {
                    "name": part.get("tool", ""),
                    "input": state.get("input") or {},
                    "output": state.get("output", ""),
                }})
            elif typ == "step_finish":
                tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
                cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
                usage = _merge_usage(usage, {
                    "input_tokens": tokens.get("input", 0),
                    "output_tokens": tokens.get("output", 0),
                    "cache_creation": cache.get("write", 0),
                    "cache_read": cache.get("read", 0),
                    "reasoning_tokens": tokens.get("reasoning", 0),
                    "cost_usd": part.get("cost", 0.0) or 0.0,
                })
                events.append({"type": "agent.usage", "payload": dict(usage)})
        return events, usage
    def extract_session_id(self, raw: dict) -> str | None:
        # 实测：所有事件带顶层 sessionID（camelCase）
        sid = raw.get("sessionID") if isinstance(raw, dict) else None
        return str(sid) if sid else None


class OmpAdapter(ClaudeAdapter):
    cli_name = "omp"
    _BIN = ["omp", str(HOME / ".bun/bin/omp")]
    # omp 无 max-turns flag（有 --max-time），max_turns 参数按接口保留但忽略；
    # 权限映射基于 --approval-mode (always-ask|write|yolo) / --auto-approve
    PERMISSION_FLAGS = {
        "plan": ["--approval-mode", "always-ask"],
        "acceptEdits": ["--approval-mode", "write"],
        "fullAccess": ["--auto-approve"],
    }
    def build_command(self, *, prompt, cwd, model=None, permission_mode="plan",
                      max_turns=8, resume=None) -> list[str]:
        cmd = [self.binary(), "--print", "--mode", "json", "--cwd", str(cwd)]
        cmd += self.PERMISSION_FLAGS.get(permission_mode, self.PERMISSION_FLAGS["plan"])
        if model:
            cmd += ["--model", model]
        if resume:
            cmd += ["--resume", resume]
        cmd.append(prompt)
        return cmd
    def parse_stream(self, lines) -> tuple[list[dict], dict]:
        # omp -p --mode=json 实测（17.2.4）：session/agent_start/turn_start/
        # message_start/message_update(text_delta)/message_end/turn_end/agent_end；
        # usage 权威值在 assistant message_end（message_start 为 0 占位），
        # 字段 camelCase（input/output/cacheRead/cacheWrite/cost.total）
        events: list[dict] = []
        usage: dict[str, Any] = {}
        session_id = ""
        last_stop_reason = ""
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
            if typ == "session" and raw.get("id"):
                session_id = str(raw["id"])
            elif typ == "message_update":
                ev = raw.get("assistantMessageEvent")
                if isinstance(ev, dict) and ev.get("type") == "text_delta":
                    events.append({"type": "agent.message_delta",
                                   "payload": {"delta": ev.get("delta", "")}})
            elif typ == "message_end" and isinstance(raw.get("message"), dict):
                msg = raw["message"]
                events.append({"type": "agent.message",
                               "payload": {"text": _extract_text(msg.get("content"))}})
                stop = msg.get("stopReason")
                if stop:
                    last_stop_reason = str(stop)
                if isinstance(msg.get("usage"), dict):
                    # message_end.usage 是会话最终权威值，直接覆盖（同 claude result 语义）
                    u = msg["usage"]
                    cost = u.get("cost") if isinstance(u.get("cost"), dict) else {}
                    usage = {
                        "input_tokens": u.get("input", 0),
                        "output_tokens": u.get("output", 0),
                        "cache_creation": u.get("cacheWrite", 0),
                        "cache_read": u.get("cacheRead", 0),
                        "reasoning_tokens": u.get("reasoningTokens", 0),
                        "cost_usd": cost.get("total", 0.0) or 0.0,
                    }
                    events.append({"type": "agent.usage", "payload": dict(usage)})
            elif typ == "agent_end":
                stop = last_stop_reason or ("end_turn" if raw.get("isTerminal") else "unknown")
                events.append({"type": "agent.terminated",
                               "payload": {"stop_reason": stop,
                                           "session_id": session_id}})
        return events, usage
    def extract_session_id(self, raw: dict) -> str | None:
        # 实测：session 事件顶层 id
        sid = raw.get("id") if isinstance(raw, dict) else None
        return str(sid) if sid else None


def _extract_text(content) -> str:
    """content 为字符串或内容块数组（Anthropic Messages 风格）时提取可见文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _merge_usage(base: dict, add: dict) -> dict:
    out = dict(base)
    for k, v in add.items():
        out[k] = out.get(k, 0) + (v if isinstance(v, (int, float)) else 0)
    return out


_CLAUDE = ClaudeAdapter()
_GROK = GrokAdapter()
_OPENCODE = OpencodeAdapter()
_OMP = OmpAdapter()
_ADAPTERS: dict[str, BaseAdapter] = {"claude": _CLAUDE, "grok": _GROK,
                                     "opencode": _OPENCODE, "omp": _OMP}


def get_adapter(name: str) -> BaseAdapter:
    if name not in _ADAPTERS:
        raise ValueError(f"unknown target_cli: {name}")
    return _ADAPTERS[name]


def register_adapter(adapter: BaseAdapter) -> None:
    _ADAPTERS[adapter.cli_name] = adapter
