#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""route_review —— 路线异常的复核状态机（append-only，stdlib only）。

它不自动纠偏，也不给 worker 发指令；只把「异常被发现 → 下一拍复测 →
恢复或仍异常」做成可核对的闭环。路线判断仍来自 `route_health_runtime`，
本模块不复制第二套判据，也不要求用户为复核再点一次按钮。

状态：
  needs_review    新异常，等待下一拍自动复测
  still_attention 复测仍是 attention，继续提示复核
  cleared         后续巡检已不再发现该异常（恢复或施工对象结束）

账本放在 daemon 的 runtime 根，不写项目账本：路线巡检是本机控制面的只读事实，
不能因为一个窗口正在讨论别的项目就把异常落到那个项目的账里。
所有变更都追加事件，不覆盖历史。unknown 只保留现状，不冒充恢复。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

LEDGER_NAME = "route_reviews.jsonl"
ACTIVE = {"needs_review", "still_attention"}
KNOWN_STATUSES = ACTIVE | {"cleared"}
VALID_LEVELS = {"attention", "ok", "unknown"}


class ReviewError(ValueError):
    pass


def runtime_root(root: Path | None = None) -> Path:
    if root is not None:
        return Path(root)
    return Path(os.environ.get("KG_DAEMON_HOME")
                or Path.home() / ".codex-autopilot")


def ledger_path(root: Path | None = None) -> Path:
    return runtime_root(root) / LEDGER_NAME


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise ReviewError(f"路线复核账本坏行：{exc}") from exc
        if not isinstance(row, dict):
            raise ReviewError("路线复核账本行不是对象")
        rows.append(row)
    return rows


def latest(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按问题键取最新事件；同一问题的签名变化不能生成第二个待复核对象。"""
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        issue = str(row.get("issue_key") or "")
        if issue and row.get("status") in KNOWN_STATUSES:
            out[issue] = row
    return out


def current(root: Path | None = None) -> dict[str, Any]:
    try:
        rows = _read(ledger_path(root))
    except (OSError, ReviewError) as exc:
        return {"status": "error", "level": "unknown", "count": None,
                "items": [], "headline": "路线复核账本取不到（%s）" % exc,
                "note": "取不到不能当成没有待复核项"}
    open_items = sorted(
        (r for r in latest(rows).values() if r.get("status") in ACTIVE),
        key=lambda r: str(r.get("ts") or ""))
    if not open_items:
        return {"status": "ok", "level": "ok", "count": 0, "items": [],
                "headline": "没有待复核的路线异常",
                "note": "路线异常只记录复核状态，不会自动改 worker、路线或配置"}
    return {"status": "ok", "level": "attention", "items": open_items,
            "count": len(open_items),
            "headline": "%s 项路线与效能异常待复核" % len(open_items),
            "note": "路线异常只记录复核状态，不会自动改 worker、路线或配置"}


def _append(path: Path, events: list[dict[str, Any]]) -> None:
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def _belongs_to(key: str, owner: str) -> bool:
    """ok 时的 owner 可能是 worker id；只清这个 worker 名下的复核项。"""
    return key == owner or key.startswith(owner + "|")


def _owner_of(key: str) -> str:
    """问题键形如 `worker|原因`；前缀就是它所属的执行对象。"""
    return str(key).split("|", 1)[0]


def sync(summary: dict[str, Any] | None, signature: str | None,
         issue_keys: Any = None, issue_key: str | None = None,
         root: Path | None = None, now: str | None = None,
         observed_owners: Any = None,
         owner_levels: Any = None) -> dict[str, Any]:
    """每拍复测入口：只追加状态事件，不产生任何可执行动作。

    `issue_keys` 是当前仍在 attention 的问题键集合；分钟数、改动数继续增长
    但问题没变时，键保持不变。这样「复测仍是同一问题」收敛成 still_attention，
    不会因为数字增长反复生成新异常。多 worker 中有一个恢复时，只清除它名下
    不再出现的复核项。`observed_owners` / `owner_levels` 用来区分「真的复测成
    ok」和「这一拍判据没取到」：后者必须保留旧复核项，不能冒充恢复。
    """
    summary = summary or {}
    sig = str(signature or "").strip()
    level = str(summary.get("level") or "unknown")
    if level not in VALID_LEVELS:
        return {"status": "error", "reason": "拒绝写入：未知路线状态",
                "written": False, "writes": []}
    if level == "unknown":
        # 判据取不到只说明复测没做成，不能据此关闭异常。
        return {"status": "ok", "written": False, "writes": [],
                "signature": sig, "issue_key": str(issue_key or ""),
                "review_status": "unknown"}
    keys: list[str] = []
    for raw in (issue_keys or []):
        key = str(raw).strip()
        if key and key not in keys:
            keys.append(key)
    single = str(issue_key or "").strip()
    if single and single not in keys:
        keys.append(single)

    if level == "attention" and not keys:
        return {"status": "error", "reason": "拒绝写入：attention 缺少问题键",
                "written": False, "writes": []}
    if not sig:
        return {"status": "error", "reason": "拒绝写入：路线签名缺失",
                "written": False, "writes": []}

    observed = [str(x).strip() for x in (observed_owners or [])
                if str(x).strip()]
    levels = {str(k).strip(): str(v).strip()
              for k, v in (owner_levels or {}).items()
              if str(k).strip()}

    path = ledger_path(root)
    try:
        by_issue = latest(_read(path))
    except (OSError, ReviewError) as exc:
        return {"status": "error", "reason": "路线复核账本取不到（%s）" % exc,
                "written": False, "writes": []}

    writes: list[dict[str, Any]] = []
    ts = now or _now()
    headline = str(summary.get("headline") or "")

    def add(key: str, status: str) -> None:
        writes.append({
            "ts": ts, "type": "route_review", "signature": sig,
            "issue_key": key, "status": status, "level": level,
            "headline": headline, "action": "none",
        })

    active_keys = set(keys)

    def clearable(key: str) -> bool:
        if key in active_keys:
            return False
        if not observed:
            # 全局空闲或施工对象已结束：旧的复核项才算恢复。
            return True
        owner = _owner_of(key)
        if owner in observed:
            return levels.get(owner, "unknown") == "ok"
        # 这一拍没看见这个对象，说明它已不在 daemon 的活施工集合里；
        # 施工对象结束本身就是恢复。判据缺失的对象仍会被 observed 报成 unknown，
        # 不会走到这里被误清。
        return True

    if level == "attention":
        for key in keys:
            row = by_issue.get(key)
            if not row or row.get("status") == "cleared":
                add(key, "needs_review")
            elif row.get("status") == "needs_review":
                add(key, "still_attention")
        # 某个 worker 已恢复但另一个仍在 attention：只清真正复测为 ok 的对象。
        for key, row in by_issue.items():
            if row.get("status") in ACTIVE and clearable(key):
                add(key, "cleared")
    else:
        # 聚合层 ok 只说明它选中的结果 ok；逐个对象再核一次，避免
        # 判据缺失的对象被同拍其他对象的 ok 误标成恢复。
        for key, row in by_issue.items():
            if row.get("status") in ACTIVE and clearable(key):
                add(key, "cleared")

    _append(path, writes)
    first = keys[0] if keys else ""
    last = next((w["status"] for w in reversed(writes)
                 if w.get("issue_key") == first), None)
    return {"status": "ok", "written": bool(writes), "writes": writes,
            "signature": sig, "issue_keys": keys,
            "review_status": last or (by_issue.get(first, {}).get("status")
                                      if level == "attention" else "not_applicable")}


def observe(summary: dict[str, Any] | None, signature: str | None,
            issue_key: str | None = None, root: Path | None = None,
            now: str | None = None) -> dict[str, Any]:
    """兼容单问题调用；实际语义就是一次完整复测同步。"""
    return sync(summary, signature, issue_key, root, now)
