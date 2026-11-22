#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上下文档案：只读取并投影自动收录、确认、驳回和生效记录。

这里不是第二套真相，也不是第二个审批入口。候选的「算 / 不算」仍走
`constraint_candidates` 的确认闸；本模块只回答用户三个问题：
  1. 自动收了什么；
  2. 来源是入口原话还是跨会话自学习；
  3. 现在停在待确认、已确认、已驳回还是已生效。

台账目录读不到、文件损坏一律返回 unknown；空目录才是真正的 0 条。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

OPS = Path(__file__).resolve().parent
REPO = OPS.parent

KNOWN_STATUSES = {"proposed", "promoted", "dismissed", "active"}


def truth_dir() -> Path:
    repo = os.environ.get("KG_REPO") or ""
    return Path(repo).expanduser() / "ops" / "truth" if repo else OPS / "truth"


def _read_jsonl(path: Path, errors: list) -> list | None:
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"{path.name}: 读不到（{exc.__class__.__name__}）")
        return None
    rows = []
    for line_no, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            errors.append(f"{path.name}:{line_no}: JSON 损坏")
            return None
        if isinstance(row, dict):
            rows.append(row)
        else:
            errors.append(f"{path.name}:{line_no}: 不是对象行")
            return None
    return rows


def _latest(rows: list) -> dict:
    out = {}
    for row in rows:
        rid = row.get("id")
        if rid:
            out[rid] = row
    return out


def _status_of(row: dict) -> str:
    status = str(row.get("status") or "proposed").strip()
    return status if status in KNOWN_STATUSES else "unknown"


def _item(row: dict, status: str, bucket: str) -> dict:
    evidence = []
    for item in (row.get("evidence") or [])[:3]:
        if isinstance(item, dict):
            evidence.append({
                "thread": item.get("thread") or item.get("thread_id"),
                "text": str(item.get("text") or item.get("source_sample") or "")[:240],
            })
        elif item:
            evidence.append({"text": str(item)[:240]})
    return {
        "id": row.get("id"),
        "bucket": bucket,
        "status": status,
        "text": row.get("text") or row.get("statement") or row.get("title") or "",
        "title": row.get("title") or "",
        "kind": row.get("kind"),
        "source": row.get("source") or "intake",
        "source_topic": row.get("source_topic"),
        "source_sample": str(row.get("source_sample") or "")[:240] or None,
        "scope": row.get("scope") or [],
        "scope_class": row.get("scope_class"),
        "priority": row.get("priority"),
        "signals": row.get("signals") or [],
        "mentions": row.get("mentions"),
        "threads": row.get("threads"),
        "evidence": evidence,
        "reason": row.get("reason"),
        "constraint_id": row.get("constraint_id"),
        "enforceable": row.get("enforceable"),
    }


def collect(root: Path | None = None) -> dict:
    """聚合上下文档案；失败只读上报 unknown，绝不写任何台账。"""
    base = Path(root) if root else truth_dir()
    errors = []
    if not base.exists() or not base.is_dir():
        return {
            "status": "unknown",
            "reason": f"档案目录读不到：{base}（不把不知道当成 0 条）",
            "counts": {},
            "items": [],
            "unknown": [f"档案目录读不到：{base}"],
            "note": "上下文档案只读；取不到就明确说取不到。",
        }

    cand_rows = _read_jsonl(base / "constraint_candidates.jsonl", errors)
    cons_rows = _read_jsonl(base / "constraints.jsonl", errors)
    if cand_rows is None or cons_rows is None:
        return {
            "status": "unknown",
            "reason": "上下文档案源文件损坏，已停止投影；不把读失败当成空档案",
            "counts": {},
            "items": [],
            "unknown": errors,
            "note": "上下文档案只读；取不到就明确说取不到。",
        }

    items = []
    for row in _latest(cand_rows).values():
        status = _status_of(row)
        if status in ("proposed", "promoted", "dismissed"):
            items.append(_item(row, status, "candidate"))
    for row in _latest(cons_rows).values():
        status = _status_of(row)
        if status == "active":
            items.append(_item(row, "active", "constraint"))

    order = {"proposed": 0, "promoted": 1, "active": 2, "dismissed": 3}
    items.sort(key=lambda r: (order.get(r["status"], 9), str(r.get("id") or "")))
    counts = {
        status: len([r for r in items if r["status"] == status])
        for status in ("proposed", "promoted", "dismissed", "active")
    }
    return {
        "status": "ok",
        "reason": None,
        "counts": counts,
        "items": items,
        "unknown": [],
        "note": ("自动收录只生成候选，不自动生效；待确认项在「需要你决策」区"
                 "逐条点「算 / 不算」，本页只负责查看与追溯。"),
    }


def human(data: dict) -> str:
    if data.get("status") != "ok":
        return "上下文档案取不到：" + (data.get("reason") or "；".join(data.get("unknown") or []))
    c = data.get("counts") or {}
    lines = [
        "=== 上下文档案 ===",
        "待确认 {proposed} · 已确认 {promoted} · 已驳回 {dismissed} · 生效 {active}".format(
            proposed=c.get("proposed", 0), promoted=c.get("promoted", 0),
            dismissed=c.get("dismissed", 0), active=c.get("active", 0)),
    ]
    labels = {"proposed": "待确认", "promoted": "已确认",
              "dismissed": "已驳回", "active": "生效"}
    for row in data.get("items") or []:
        src = row.get("source") or "intake"
        extra = ""
        if row.get("mentions") is not None and row.get("threads") is not None:
            extra = f"；出现 {row.get('mentions')} 次 / 跨 {row.get('threads')} 个会话"
        lines.append(f"  [{labels.get(row.get('status'), row.get('status'))}] "
                     f"{row.get('id')}（{src}{extra}）{row.get('text')}")
        if row.get("reason"):
            lines.append(f"      驳回原因：{row.get('reason')}")
        if row.get("constraint_id"):
            lines.append(f"      生效约束：{row.get('constraint_id')}")
    lines.append("")
    lines.append(data.get("note") or "")
    return "\n".join(lines)


def main(argv=None) -> int:
    argv = list(argv or [])
    data = collect()
    if argv and argv[0] == "--json":
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(human(data))
    return 0 if data.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
