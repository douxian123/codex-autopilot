#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收管家的交付前预验收闸。

人工验收不是第一道测试。这个模块把「是否已经值得打扰用户」收敛成一份
可审计的质量包：工程闭环、真实前端、用户可理解性、PRD/预期对齐四类证据
都必须明确通过；任一项失败或取不到，都只能停在 ``blocked``/``unknown``。

模块只读消费已有证据，不替代具体门禁，也不把截图本身当成通过证据。这样
既能复用项目已有的测试和浏览器门禁，也不会因为新增一层投影而制造第二套
真相台账。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import acceptance_dictionary


CHECKS = (
    {
        "id": "engineering",
        "title": "工程与后端链路",
        "reason": "实现、接口、数据、权限、错误和回滚必须先由机器验收。",
        "required_evidence": ("machine", "backend", "integration", "rollback"),
    },
    {
        "id": "frontend",
        "title": "前端视觉与交互",
        "reason": "真实页面必须完成加载、交互、响应、错误和多视口检查，不能只看源码或 fixture。",
        "required_evidence": ("browser", "screenshot", "interaction", "responsive"),
    },
    {
        "id": "comprehension",
        "title": "小白用户可理解性",
        "reason": "产品负责人、设计师和第一次使用的人都应能从截图和路径看懂发生了什么、下一步是什么。",
        "required_evidence": ("comprehension", "user_walkthrough"),
    },
    {
        "id": "alignment",
        "title": "PRD 与用户预期对齐",
        "reason": "所有需求、反馈、验收标准、人工边界和未决风险必须已对齐并回写。",
        "required_evidence": ("prd", "expectations", "acceptance", "risk"),
    },
    {
        "id": "evidence",
        "title": "证据审计",
        "reason": "每条必检项必须能回到当前版本、真实入口和可复核现场。",
        "required_evidence": ("evidence", "version", "provenance"),
    },
)

_CHECK_IDS = {item["id"] for item in CHECKS}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _rows(root: Path) -> list[dict[str, Any]]:
    path = root / "ops" / "truth" / "pre_acceptance.jsonl"
    if not path.exists():
        return []
    result = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                result.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        return []
    return [row for row in result if isinstance(row, dict)]


def latest(root: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in _rows(root):
        check_id = _text(row.get("check_id"))
        if check_id in _CHECK_IDS:
            result[check_id] = row
    return result


def _evidence_types(row: dict[str, Any]) -> set[str]:
    values = row.get("evidence_types") or row.get("evidence") or []
    if isinstance(values, str):
        values = [values]
    return {_text(value).lower() for value in values if _text(value)}


def _check_view(spec: dict[str, Any], row: dict[str, Any] | None) -> dict[str, Any]:
    row = row or {}
    status = _text(row.get("status")).lower() or "unknown"
    types = _evidence_types(row)
    required = set(spec["required_evidence"])
    missing = sorted(required - types)
    if status == "pass" and not missing:
        state = "pass"
    elif status in ("fail", "blocked"):
        state = "blocked"
    else:
        state = "unknown"
    return {
        "id": spec["id"],
        "title": spec["title"],
        "reason": spec["reason"],
        "state": state,
        "status": status,
        "required_evidence": sorted(required),
        "evidence_types": sorted(types),
        "missing": missing,
        "summary": _text(row.get("summary")) or ("证据齐全" if state == "pass" else "尚未形成可交付证据"),
        "commands": row.get("commands") or [],
        "artifacts": row.get("artifacts") or [],
        "updated_at": _text(row.get("updated_at")),
    }


def evaluate(root: Path) -> dict[str, Any]:
    """返回逐项只读预验收结果；缺少逐项记录时必须是 unknown。"""
    rows = latest(Path(root))
    item_rows: dict[str, dict[str, Any]] = {}
    for row in _rows(Path(root)):
        item_id = _text(row.get("item_id"))
        if item_id in acceptance_dictionary.BY_ID:
            item_rows[item_id] = row
    items = [acceptance_dictionary.evaluate_item(spec, item_rows.get(spec["id"]))
             for spec in acceptance_dictionary.ITEMS]
    categories = []
    for category in acceptance_dictionary.CATEGORIES:
        subset = [item for item in items if item["category"] == category]
        categories.append({
            "id": category,
            "title": next((c["title"] for c in CHECKS if c["id"] == category), category),
            "required": len([x for x in subset if x["required"]]),
            "passed": len([x for x in subset if x["state"] == "pass"]),
            "failed": len([x for x in subset if x["state"] in ("fail", "blocked")]),
            "unknown": len([x for x in subset if x["state"] == "unknown"]),
            "not_applicable": len([x for x in subset if x["state"] == "not_applicable"]),
        })
    if not item_rows:
        state = "unknown"
        label = "验收管家：逐项自检尚未完成，不通知你验收"
    elif any(item["state"] in ("fail", "blocked") for item in items):
        state = "blocked"
        label = "验收管家：有逐项失败，先返修，不通知你验收"
    elif any(item["state"] == "unknown" for item in items):
        state = "unknown"
        label = "验收管家：有逐项缺证据或未判定，不通知你验收"
    else:
        state = "ready"
        label = "验收管家：所有适用必检项通过，可以通知你验收"
    passed = sum(item["state"] == "pass" for item in items)
    return {
        "status": state,
        "label": label,
        "checks": categories,
        "categories": categories,
        "items": items,
        "passed": passed,
        "failed": sum(item["state"] in ("fail", "blocked") for item in items),
        "unknown": sum(item["state"] == "unknown" for item in items),
        "not_applicable": sum(item["state"] == "not_applicable" for item in items),
        "total": len(items),
        "notify_user": state == "ready",
        "policy": "所有适用必检项逐条 pass 后才进入用户验收；截图是复核附件，不是自检替代品。",
    }


def record(root: Path, check_id: str, status: str, evidence_types: list[str],
           summary: str = "", commands: list[str] | None = None,
           artifacts: list[str] | None = None, *, item_id: str | None = None,
           version: str = "", reason: str = "") -> dict[str, Any]:
    """追加一条逐项预验收证据；旧 check_id 仍保留供迁移期兼容。"""
    check_id = _text(check_id)
    status = _text(status).lower()
    if check_id not in _CHECK_IDS:
        raise ValueError(f"unknown pre-acceptance check: {check_id}")
    if status not in {"pass", "fail", "blocked", "unknown"}:
        raise ValueError(f"invalid pre-acceptance status: {status}")
    row = {
        "check_id": check_id,
        "item_id": item_id or "",
        "status": status,
        "evidence_types": sorted({_text(value).lower() for value in evidence_types if _text(value)}),
        "summary": _text(summary),
        "commands": commands or [],
        "artifacts": artifacts or [],
        "updated_at": _now(),
    }
    if version:
        row["version"] = version
    if reason:
        row["reason"] = reason
    path = Path(root) / "ops" / "truth" / "pre_acceptance.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return row


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="验收管家交付前预验收闸")
    parser.add_argument("--repo", default=".")
    parser.add_argument("command", nargs="?", choices=("show", "record"), default="show")
    parser.add_argument("check_id", nargs="?")
    parser.add_argument("status", nargs="?")
    parser.add_argument("evidence_types", nargs="*")
    parser.add_argument("--item-id", default="")
    parser.add_argument("--version", default="")
    parser.add_argument("--reason", default="")
    args = parser.parse_args()
    root = Path(args.repo).expanduser().resolve()
    if args.command == "record":
        if not args.check_id or not args.status or not args.evidence_types:
            parser.error("record 需要 check_id status evidence_types")
        record(root, args.check_id, args.status, args.evidence_types,
               item_id=args.item_id, version=args.version, reason=args.reason)
    print(json.dumps(evaluate(root), ensure_ascii=False, indent=2))
    return 0 if evaluate(root)["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
