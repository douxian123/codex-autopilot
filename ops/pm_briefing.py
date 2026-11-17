#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pm_briefing —— 项目经理的只读简报层（stdlib only，不写任何台账）。

为什么要有这一层
----------------
自动挡已经有上下文候选、overlap_hold、验收截图和 native follow_up，但它们散在
不同台账/运行面字段里：用户得自己把「预期打磨到哪、谁在给谁让路、哪条该追进度」
拼起来。这不是面板美观问题，是 PM 职责缺了一块。

本模块只做聚合：
  * expectations  待确认预期 + 已生效约束 + 产品主线当前阶段；
  * overlap_holds 施工让路：等谁、撞了哪些文件，明确「正常排队，不是卡住」；
  * follow_up     有真实 thread 绑定但长时间无新进展的进度追问候选。

硬边界：
  * 不自动发 follow_up，不调用 codex_native，不改 control/ledger/acceptance；
  * 没有文件线索时不假装「不重叠」，overlap 只展示调度层已经算出的结论；
  * 台账文件缺失返回 unknown，空台账才是「0 条」，绝不拿 0 冒充正常；
  * 所有输出都是面板可直接照抄的小结构，不把内部 Python 字面量抛给读者。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

OPS = Path(__file__).resolve().parent

try:
    import constraints as constraints_mod
except Exception:  # pragma: no cover - 单测拷贝模块时的兜底
    constraints_mod = None

try:
    import constraint_candidates as candidates_mod
except Exception:  # pragma: no cover
    candidates_mod = None


DEFAULT_STALE_MINUTES = 30
ACTIVE_STATUSES = ("open", "doing", "todo", "wip", "blocked")


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_jsonl(path):
    out = []
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _latest(rows):
    out = {}
    for row in rows:
        rid = row.get("id")
        if rid:
            out[rid] = row
    return out


def _parse_ts(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _text(value, limit=120):
    return " ".join(str(value or "").split())[:limit]


def _expectations(truth):
    constraints_path = truth / "constraints.jsonl"
    candidates_path = truth / "constraint_candidates.jsonl"
    mainline_path = truth / "mainline.json"
    # constraint_candidates.jsonl 是守护扫到候选才生成的可选运行台账。干净项目
    # 首装没有它，等于「0 条待确认」，不是探针坏了；constraints / mainline 缺失
    # 才是真相取不到，必须继续报 unknown。
    missing = [p.name for p in (constraints_path, mainline_path)
               if not p.exists()]

    active = []
    if constraints_mod is not None and constraints_path.exists():
        try:
            active = constraints_mod.active_constraints(constraints_path)
        except Exception:
            missing.append("constraints.json（读取失败）")

    pending = []
    if candidates_path.exists():
        try:
            pending = _latest(_read_jsonl(candidates_path))
            pending = [r for r in pending.values() if r.get("status") == "proposed"]
        except Exception:
            missing.append("constraint_candidates.jsonl（读取失败）")

    ml = _read_json(mainline_path) if mainline_path.exists() else None
    focus = ml.get("current_focus") if isinstance(ml, dict) else None
    focus_rows = []
    if isinstance(focus, list):
        for item in focus[:4]:
            focus_rows.append(_text(item if isinstance(item, str)
                                    else (item.get("title") or item.get("text") or "")))
        focus_rows = [r for r in focus_rows if r]

    return {
        "status": "unknown" if missing else "ok",
        "missing": missing,
        "pending": [{
            "id": r.get("id"),
            "text": _text(r.get("text") or r.get("statement") or r.get("title")),
            "kind": r.get("kind") or "",
        } for r in pending[:6]],
        "pending_count": len(pending),
        "active_count": len(active) if constraints_path.exists() else None,
        "active": [{
            "id": r.get("id"),
            "text": _text(r.get("statement") or r.get("text") or r.get("title")),
            "scope": r.get("scope") or [],
        } for r in active[:6]],
        "mainline_stage": _text((ml or {}).get("stage")) if ml else "",
        "focus": focus_rows,
    }


def _overlap_holds(runtime):
    blocked = (runtime or {}).get("blocked")
    if not isinstance(blocked, dict) or "exec_waiting" not in blocked:
        return {"status": "unknown", "items": [],
                "note": "调度层的让路信号取不到，不假装没有排队。"}
    rows = blocked.get("exec_waiting") or []
    items = []
    for row in rows[:8]:
        if not isinstance(row, dict):
            continue
        holders = []
        for h in row.get("holders") or []:
            if isinstance(h, dict):
                holders.append({"id": h.get("id"), "files": h.get("files") or []})
        items.append({
            "id": row.get("id"),
            "title": _text(row.get("title") or row.get("reason") or "待落地工作项"),
            "holders": holders[:4],
            "files": [str(f) for f in (row.get("files") or [])][:6],
            "note": "正常排队，不计入卡住；前一项落地后自动重算。",
        })
    return {"status": "ok", "items": items,
            "note": "正常排队，不计入卡住；前一项落地后自动重算。"}


def _native_bindings(truth):
    path = truth / "native_routes.jsonl"
    if not path.exists():
        # 原生路由是可选的运行台账：从未派过活的干净项目本来就不会有它。
        # 这里返回空绑定，表达「0 条窗口绑定」，不能冒充成探针取不到；
        # 问题台账 ledger.jsonl 缺失仍然是 unknown，两者语义不同。
        return {}
    latest = {}
    for row in _read_jsonl(path):
        lid = row.get("ledger_id")
        if lid:
            latest[lid] = row
    return latest


def _follow_up(runtime, truth, now, stale_minutes):
    ledger_path = truth / "ledger.jsonl"
    bindings = _native_bindings(truth)
    if not ledger_path.exists():
        missing = []
        if not ledger_path.exists():
            missing.append("ledger.jsonl")
        return {"status": "unknown", "items": [], "missing": missing,
                "note": "追问候选取不到；不自动向任何窗口发消息。"}

    threshold = timedelta(minutes=int(stale_minutes or DEFAULT_STALE_MINUTES))
    latest = _latest(_read_jsonl(ledger_path))
    items = []
    for rid, row in latest.items():
        if row.get("status") not in ACTIVE_STATUSES:
            continue
        binding = bindings.get(rid) or {}
        tid = binding.get("thread_id") or row.get("thread_id")
        if not tid:
            continue
        touched = _parse_ts(row.get("ts") or row.get("updated") or binding.get("ts"))
        if touched is None:
            continue
        if now - touched < threshold:
            continue
        items.append({
            "id": rid,
            "title": _text(row.get("title") or row.get("detail") or "未命名工作项"),
            "thread_id": str(tid),
            "status": row.get("status"),
            "idle_minutes": max(0, int((now - touched).total_seconds() // 60)),
            "suggested_prompt": "继续处理 %s：回报当前卡点和下一步。" % rid,
        })
    items.sort(key=lambda r: (-r["idle_minutes"], r["id"] or ""))
    return {"status": "ok", "items": items[:8], "missing": [],
            "note": "只生成候选，不会自动追问；你点头才会发消息。"}


def compute(repo, runtime=None, now=None, stale_minutes=DEFAULT_STALE_MINUTES):
    """把现有真相与运行面聚合成 PM 简报。纯函数，不写文件。"""
    truth = Path(repo).expanduser() / "ops" / "truth"
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    expectations = _expectations(truth)
    overlap = _overlap_holds(runtime)
    follow_up = _follow_up(runtime, truth, now, stale_minutes)
    statuses = [expectations["status"], overlap["status"], follow_up["status"]]
    if "unknown" in statuses:
        overall = "unknown"
    elif expectations["pending_count"] or follow_up["items"]:
        overall = "attention"
    else:
        overall = "ok"
    return {
        "status": overall,
        "expectations": expectations,
        "overlap_holds": overlap,
        "follow_up_candidates": follow_up,
        "source": "只读聚合：constraints / candidates / mainline / ledger / native_routes / 运行面",
    }


if __name__ == "__main__":
    root = os.environ.get("KG_REPO") or str(OPS.parent)
    print(json.dumps(compute(root), ensure_ascii=False, indent=2))
