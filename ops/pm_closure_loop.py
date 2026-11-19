#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PM 施工闭环：从入口原话和现有台账投影出可执行的施工路径。

这里不新增一套多 agent 框架，也不替代 truthctl 的路由/关闭职责。它只把
`inbox.jsonl` 的原话、`ledger.jsonl` 的施工项、`mainline.json` 的路线和运行时
让路/绑定信号拼成一条 PM 能汇报、用户能核对的施工路径。

硬边界：
  * 只读；不改 mainline.json、不写验收台账、不发追问、不调用 worker；
  * 文件占地未知就是 unknown，不把「没有文件线索」说成「不重叠」；
  * 关闭必须指向真实证据或已合入 commit，不把 open 项说成 done；
  * 台账读取失败返回 unknown，不拿 0 冒充正常。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_STALE_MINUTES = 30
ACTIVE_STATUSES = ("open", "doing", "todo", "wip", "blocked")
DEPENDENCY_WORDS = ("先", "依赖", "等", "之后", "再", "前一步", "前一项")


def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_jsonl(path: Path):
    """返回 (rows, error)。文件缺失由调用方决定是否算 optional。"""
    if not path.exists():
        return [], "missing"
    rows = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [], str(exc)
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            return [], str(exc)
        if isinstance(row, dict):
            rows.append(row)
    return rows, ""


def _latest(rows):
    out = {}
    for row in rows:
        rid = row.get("id")
        if rid:
            out[rid] = row
    return out


def _ts(value):
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


def _text(value, limit=160):
    return " ".join(str(value or "").split())[:limit]


def _roadmap_status(ml, text):
    """只投影路线状态，不修改 mainline.json；匹配不到就保留 unknown。"""
    rows = ml.get("roadmap") if isinstance(ml, dict) else None
    if not isinstance(rows, list):
        return {"step": None, "status": "unknown", "note": "产品路线取不到"}
    for raw in rows:
        step = raw.get("step") if isinstance(raw, dict) else raw
        if not step:
            continue
        if str(step) in text or text in str(step):
            return {
                "step": _text(step),
                "status": raw.get("status", "todo") if isinstance(raw, dict) else "todo",
                "expected": _text(raw.get("expected")) if isinstance(raw, dict) else "",
                "note": "只读投影；路线方向仍由人拍板。",
            }
    return {"step": None, "status": "unknown",
            "note": "还没匹配到产品路线步骤，不替用户改写主线。"}


def _dependency(item, ledger):
    """依赖只从显式文字和已声明的文件占地推断；没有线索就是 unknown。"""
    text = " ".join(str(item.get(k) or "") for k in ("title", "detail", "text"))
    deps = []
    for other_id, other in ledger.items():
        if other_id == item.get("id"):
            continue
        title = _text(other.get("title"))
        if title and title in text:
            deps.append({"id": other_id, "reason": "原话点名先做/依赖"})
            continue
        shared = sorted(set(item.get("files") or []) & set(other.get("files") or []))
        if shared:
            deps.append({"id": other_id, "reason": "文件占地重叠：" + "、".join(shared[:3])})
    if any(w in text for w in DEPENDENCY_WORDS) and not deps:
        return {"status": "unknown", "items": [],
                "note": "原话提到先后/依赖，但没有可核对的依赖对象。"}
    if not item.get("files"):
        return {"status": "unknown", "items": deps,
                "note": "没有文件线索，不能判断是否重叠；不假装不冲突。"}
    return {"status": "ok", "items": deps[:6],
            "note": "依赖来自原话点名或已声明文件占地。"}


def _hold(item_id, runtime):
    blocked = (runtime or {}).get("blocked") or {}
    rows = blocked.get("exec_waiting") or []
    for row in rows:
        if not isinstance(row, dict) or row.get("id") != item_id:
            continue
        holders = [h.get("id") for h in (row.get("holders") or []) if isinstance(h, dict)]
        return {"status": "ok", "waiting_for": holders[:4],
                "files": [str(f) for f in (row.get("files") or [])][:6],
                "note": "正常排队，不计入卡住；前一项落地后自动重算。"}
    if "exec_waiting" not in blocked:
        return {"status": "unknown", "waiting_for": [], "files": [],
                "note": "让路信号取不到，不假装没有排队。"}
    return {"status": "ok", "waiting_for": [], "files": [],
            "note": "当前没有为这项登记让路。"}


def _follow_up(item, bindings, now, stale_minutes):
    tid = (bindings.get(item.get("id")) or {}).get("thread_id") or item.get("thread_id")
    if not tid:
        return {"status": "ok", "candidate": None,
                "note": "没有窗口绑定，不生成追问候选。"}
    touched = _ts(item.get("ts") or item.get("updated"))
    if touched is None:
        return {"status": "unknown", "candidate": None,
                "note": "更新时间取不到，不判断空闲多久。"}
    idle = int((now - touched).total_seconds() // 60)
    if idle < int(stale_minutes or DEFAULT_STALE_MINUTES):
        return {"status": "ok", "candidate": None, "note": "仍在正常推进窗口内。"}
    return {"status": "ok", "candidate": {
        "id": item.get("id"), "thread_id": str(tid), "idle_minutes": max(0, idle),
        "prompt": "继续处理 %s：回报当前卡点和下一步。" % item.get("id"),
        "needs_human": True, "auto_sent": False,
    }, "note": "只生成候选；没有逐项授权不会发消息。"}


def _close_path(item):
    rid = item.get("id")
    status = item.get("status")
    evidence = _text(item.get("evidence") or item.get("verified_by"))
    commit = _text(item.get("commit"))
    if status == "done" and (evidence or commit):
        return {"status": "closed", "command": None,
                "evidence": evidence or "commit=" + commit,
                "note": "关闭证据可追溯。"}
    if status == "done":
        return {"status": "unknown", "command": None, "evidence": "",
                "note": "台账写 done 但缺少命令/退出码或 commit，不认作已关闭。"}
    return {"status": "open", "command":
            'KG_TRUTH_EVIDENCE="<命令 + cwd + 退出码>" python3 ops/truthctl.py close %s "<结论>"' % rid,
            "evidence": "", "note": "完成并跑真实验证后，由系统关闭；不替人写结论。"}


def compute(repo, runtime=None, now=None, stale_minutes=DEFAULT_STALE_MINUTES):
    """把入口原话与现有台账投影成施工路径。纯函数，不写文件。"""
    root = Path(repo).expanduser()
    truth = root / "ops" / "truth"
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    ledger, ledger_err = _read_jsonl(truth / "ledger.jsonl")
    inbox, _ = _read_jsonl(root / "ops" / "tasks" / "inbox.jsonl")
    mainline = _read_json(truth / "mainline.json")
    native, _ = _read_jsonl(truth / "native_routes.jsonl")

    missing = []
    if ledger_err == "missing":
        missing.append("ledger.jsonl")
    elif ledger_err:
        missing.append("ledger.jsonl（读取失败）")
    if mainline is None:
        missing.append("mainline.json" if not (truth / "mainline.json").exists()
                       else "mainline.json（读取失败）")
    if missing:
        return {"status": "unknown", "missing": missing, "paths": [],
                "source": "施工路径取不到；不把不知道当成 0 条。"}

    bindings = {}
    for row in native:
        lid = row.get("ledger_id")
        if lid:
            bindings[lid] = row
    inbox_by_id = {}
    inbox_by_text = {}
    for row in inbox:
        lid = row.get("ledger_id")
        if lid:
            inbox_by_id[lid] = row
        text = _text(row.get("text"))
        if text:
            inbox_by_text[text] = row

    latest = _latest(ledger)
    paths = []
    for rid, item in latest.items():
        source = inbox_by_id.get(rid) or {}
        if not source:
            for text, row in inbox_by_text.items():
                title = _text(item.get("title"))
                if title and (title in text or text in title):
                    source = row
                    break
        raw = _text(source.get("text") or item.get("detail") or item.get("title"))
        # 文件占地只认施工项台账声明；inbox 里的 files 是入口线索，不是并发
        # 声明。用它回填会把「没有文件占地」误判成「不冲突」。
        files = [str(f) for f in (item.get("files") or [])]
        work = {
            "id": rid, "title": _text(item.get("title") or raw),
            "kind": item.get("kind") or "",
            "lane": item.get("lane") or "未归属",
            "status": item.get("status") or "",
            "files": files[:8], "occupancy": "known" if files else "unknown",
        }
        paths.append({
            "source_text": raw or "原话取不到",
            "work_item": work,
            "dependencies": _dependency({**item, "files": files, "text": raw}, latest),
            "hold": _hold(rid, runtime),
            "roadmap": _roadmap_status(mainline, raw + " " + work["title"]),
            "follow_up": _follow_up(item, bindings, now, stale_minutes),
            "close": _close_path(item),
        })
    paths.sort(key=lambda p: (p["work_item"]["status"] not in ACTIVE_STATUSES,
                              p["work_item"]["id"] or ""))
    overall = "unknown" if missing else ("attention" if any(
        p["work_item"]["status"] in ACTIVE_STATUSES for p in paths) else "ok")
    return {
        "status": overall, "missing": [], "paths": paths[:20],
        "source": "只读投影：inbox / ledger / mainline / native_routes / 运行面；不改主线、不派活、不发消息。",
    }


def human(summary):
    if summary.get("status") == "unknown":
        return "施工路径取不到：" + "、".join(summary.get("missing") or []) + "；不把不知道当成 0 条。"
    rows = summary.get("paths") or []
    if not rows:
        return "当前没有施工路径；台账已取到。"
    lines = []
    for row in rows[:5]:
        work = row["work_item"]
        lines.append("%s [%s] %s" % (work["id"], work["status"] or "unknown", work["title"]))
    return "；".join(lines)


if __name__ == "__main__":
    repo = os.environ.get("KG_REPO") or str(Path(__file__).resolve().parent.parent)
    print(json.dumps(compute(repo), ensure_ascii=False, indent=2))
