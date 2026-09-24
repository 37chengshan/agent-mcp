"""v4 Refine 三段式管道（roadmap §8.3）：reviewer → proposal → validation →
authorization → conflict check → diff → transactional commit → revision → event。

禁止 reviewer 直写 harness；base system prompt 由 models.assert_refine_target_allowed
硬守卫（Invariant 7）；同指纹 cooldown / same-failure suppression 由本地判定。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import models as m
from .store_v4 import StoreV4

MAX_OPS_PER_PROPOSAL = 50
DEFAULT_COOLDOWN_SECONDS = 600


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def validate_proposal(ops: Any) -> list[dict[str, Any]]:
    """结构化校验 + 规范化。非法 op / 越权目标 / 非法 kind 一律 ValueError。"""
    if not isinstance(ops, list):
        raise ValueError("refinement proposal must be a list")
    if len(ops) > MAX_OPS_PER_PROPOSAL:
        raise ValueError("refinement proposal exceeds %d ops" % MAX_OPS_PER_PROPOSAL)
    out: list[dict[str, Any]] = []
    for op in ops:
        if not isinstance(op, dict):
            raise ValueError("proposal entries must be objects")
        kind = op.get("op")
        if kind not in ("create", "update", "delete"):
            raise ValueError("invalid op: %s" % kind)
        item = op.get("item") or {}
        target = item.get("id") or op.get("target")
        if not target:
            raise ValueError("op %s missing target id" % kind)
        if kind == "create":
            m.assert_refine_target_allowed(None, op="create", target_id=str(target))
            m.assert_harness_kind(str(item.get("kind", "")))
        out.append({"op": kind, "target": str(target), "item": dict(item)})
    return out


def apply_proposal(
    store: StoreV4,
    ops: list[dict[str, Any]],
    *,
    session_id: str,
    author: str | None = None,
) -> list[dict[str, Any]]:
    """逐条应用（乐观并发；base prompt 守卫；global 条目需显式 scope 授权）。"""
    provenance = author or session_id
    applied: list[dict[str, Any]] = []
    for op in ops:
        item = op["item"]
        target = op["target"]
        scope = item.get("scope", "local")
        if scope not in ("local", "global"):
            raise ValueError("invalid scope: %s" % scope)
        if scope == "global" and not item.get("authorized_global"):
            raise ValueError("global harness modification requires explicit authorization")
        if op["op"] == "create":
            existing = store.harness_get(target)
            if existing is not None:
                raise ValueError("harness item already exists: %s" % target)
            store.harness_create(
                item_id=target,
                kind=str(item["kind"]),
                title=str(item.get("title") or target),
                content=item.get("content"),
                content_ref=item.get("content_ref"),
                path=str(item.get("path") or "general"),
                scope=scope,
                provenance=provenance,
            )
        elif op["op"] == "update":
            existing = store.harness_get(target)
            m.assert_refine_target_allowed(existing, op="update")
            if existing is None:
                raise ValueError("harness item not found: %s" % target)
            expected = int(item.get("expected_version", existing["version"]))
            store.harness_update(
                item_id=target,
                expected_version=expected,
                content=item.get("content"),
                title=item.get("title"),
                path=item.get("path"),
                content_ref=item.get("content_ref"),
                author=provenance,
            )
        elif op["op"] == "delete":
            existing = store.harness_get(target)
            m.assert_refine_target_allowed(existing, op="delete")
            if existing is None:
                raise ValueError("harness item not found: %s" % target)
            store.harness_delete(
                item_id=target,
                expected_version=int(item.get("expected_version", existing["version"])),
                author=provenance,
            )
        applied.append({"op": op["op"], "target": target})
    return applied


def run_preview(
    *,
    trajectory: list[dict[str, Any]],
    harness_items: list[dict[str, Any]],
    reviewer: Callable[[list[dict[str, Any]], list[dict[str, Any]]], Any],
    session_id: str,
) -> dict[str, Any]:
    """reviewer 只产出 proposal（绝不直写）。结构化结果经 validate_proposal 校验。"""
    try:
        raw = reviewer(trajectory, harness_items)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("refine reviewer failed: %s" % exc) from exc
    if isinstance(raw, dict) and "ops" in raw:
        raw = raw.get("ops")
    if raw is None:
        raw = []
    ops = validate_proposal(raw)
    return {
        "session_id": session_id,
        "proposal": ops,
        "proposal_count": len(ops),
        "reviewer": "injected",
    }


def commit_proposal(
    store: StoreV4,
    ops: Any,
    *,
    session_id: str,
    fingerprint: str | None = None,
    evidence: str | None = None,
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    author: str | None = None,
) -> dict[str, Any]:
    """事务性提交；同 fingerprint cooldown 生效时拒绝（same-failure suppression）。"""
    ops = validate_proposal(ops)
    now = _utcnow().isoformat()
    if fingerprint:
        if store.refinement_cooldown_active(fingerprint, now):
            raise ValueError("refinement cooldown active for fingerprint")
    applied = apply_proposal(store, ops, session_id=session_id, author=author)
    cooldown_until = (datetime.fromisoformat(now) + timedelta(seconds=cooldown_seconds)).isoformat()
    store.refinement_record(
        trigger="refine_commit",
        session_id=session_id,
        fingerprint=fingerprint,
        changes=ops,
        evidence=evidence,
        cooldown_until=cooldown_until,
    )
    return {"applied": applied, "count": len(applied), "cooldown_until": cooldown_until}


def extract_ops_from_text(text: str) -> list[dict[str, Any]]:
    """从 reviewer 终态文本提取 JSON ops 列表（低仪表化；找不到则返回空列表）。"""
    start = text.find("[")
    if start < 0:
        return []
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return []
                return parsed if isinstance(parsed, list) else []
    return []

