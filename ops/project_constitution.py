#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目宪法页：把用户确认过的纪律投影成一页给人看的文档。

这里不是第二套真相，也不是第二个审批入口：
  * 生效约束仍只来自 `ops/truth/constraints.jsonl`；
  * 候选、驳回仍只来自 `ops/truth/constraint_candidates.jsonl`；
  * 判断逻辑仍只来自 `ops/truth/judgments.jsonl`；
  * 书面宪法只是引用 `docs/PROJECT_GLOBAL_RULES.md` 与
    `docs/experience/GENERAL_EXPERIENCE_CONSTITUTION.md` 的原文。

源台账读不到或 JSON 损坏时返回 unknown，调用方不得覆盖旧页；空目录才是真的 0 条。
候选当然可以自动进候选区，但只有人确认后才会出现在「生效约束」区。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

OPS = Path(__file__).resolve().parent
REPO = OPS.parent

SCOPE_LABEL = {
    "global": "全局", "workflow": "工作流程", "module": "模块", "project": "项目",
}
KIND_LABEL = {
    "expectation": "预期", "rework": "返修", "decision": "决策", "taboo": "禁忌",
}
ENFORCEABLE_LABEL = {
    "none": "无机器门禁", "gate": "有机器门禁", "checklist": "人验收",
}
PRIORITY_LABEL = {"high": "高", "normal": "普通", "low": "低"}
WRITTEN = (
    "docs/PROJECT_GLOBAL_RULES.md",
    "docs/experience/GENERAL_EXPERIENCE_CONSTITUTION.md",
)


def repo_path(repo=None) -> Path:
    value = repo or os.environ.get("KG_REPO") or REPO
    return Path(value).expanduser().resolve()


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
        if not isinstance(row, dict):
            errors.append(f"{path.name}:{line_no}: 不是对象行")
            return None
        rows.append(row)
    return rows


def _latest(rows: list) -> dict:
    out = {}
    for row in rows:
        rid = row.get("id")
        if rid:
            out[rid] = row
    return out


def _status(row: dict, default: str) -> str:
    return str(row.get("status") or default).strip()


def _text(row: dict) -> str:
    return str(row.get("statement") or row.get("text") or row.get("title") or "")


def _where(row: dict) -> str:
    scope = row.get("scope") or []
    if isinstance(scope, str):
        scope = [scope]
    return "、".join(str(s) for s in scope) if scope else "全局"


def _read_written(base: Path, relative: str, errors: list) -> dict:
    path = base / relative
    if not path.exists():
        return {"path": relative, "text": None, "missing": True}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"{relative}: 读不到（{exc.__class__.__name__}）")
        return {"path": relative, "text": None, "missing": False}
    return {"path": relative, "text": text, "missing": False}


def collect(repo=None) -> dict:
    """只读投影；任何源台账读失败都返回 unknown，不返回半份宪法。"""
    base = repo_path(repo)
    truth = base / "ops" / "truth"
    errors: list = []
    candidates = _read_jsonl(truth / "constraint_candidates.jsonl", errors)
    constraints = _read_jsonl(truth / "constraints.jsonl", errors)
    judgments = _read_jsonl(truth / "judgments.jsonl", errors)
    if candidates is None or constraints is None or judgments is None:
        return {"status": "unknown", "reason": "项目宪法源台账损坏：" + "；".join(errors),
                "unknown": errors}

    written = [_read_written(base, rel, errors) for rel in WRITTEN]
    if errors and any("读不到" in e for e in errors):
        return {"status": "unknown", "reason": "书面宪法读失败：" + "；".join(errors),
                "unknown": errors}

    active_constraints = [r for r in _latest(constraints).values()
                          if _status(r, "active") == "active"]
    latest_candidates = _latest(candidates)
    proposed = [r for r in latest_candidates.values() if _status(r, "proposed") == "proposed"]
    promoted = [r for r in latest_candidates.values() if _status(r, "proposed") == "promoted"]
    dismissed = [r for r in latest_candidates.values() if _status(r, "proposed") == "dismissed"]
    active_judgments = [r for r in _latest(judgments).values()
                        if _status(r, "active") == "active"]

    priority_order = {"high": 0, "normal": 1, "low": 2}
    proposed.sort(key=lambda r: (priority_order.get(str(r.get("priority")), 9),
                                 str(r.get("id") or "")))

    gaps = []
    for row in active_constraints:
        if row.get("enforceable") == "gate" and not (row.get("cmd") or "").strip():
            gaps.append({"id": row.get("id"), "text": _text(row),
                         "reason": "标了 gate 但没有验证命令，属于假门禁"})
        elif row.get("enforceable") != "gate":
            gaps.append({"id": row.get("id"), "text": _text(row),
                         "reason": "未接机器门禁，靠人验收或纯记录"})
    for row in proposed:
        if str(row.get("priority")) == "high" and not row.get("constraint_id"):
            gaps.append({"id": row.get("id"), "text": _text(row),
                         "reason": "高优先候选仍待人确认，尚未生效"})
    for doc in written:
        if doc.get("missing"):
            gaps.append({"id": None, "text": doc["path"], "reason": "书面宪法文件不存在"})

    return {
        "status": "ok", "reason": None, "unknown": [],
        "constraints": active_constraints, "proposed": proposed,
        "promoted": promoted, "dismissed": dismissed,
        "judgments": active_judgments, "written": written, "gaps": gaps,
        "counts": {
            "active": len(active_constraints), "proposed": len(proposed),
            "promoted": len(promoted), "dismissed": len(dismissed),
            "judgments": len(active_judgments),
        },
    }


def _scope_line(row: dict) -> str:
    bits = [_where(row)]
    if row.get("scope_class"):
        bits.append(SCOPE_LABEL.get(str(row["scope_class"]), str(row["scope_class"])))
    if row.get("source"):
        bits.append("来源 " + str(row["source"]))
    return " · ".join(bits)


def render(data: dict, generated_at: str) -> str:
    if data.get("status") != "ok":
        return "# 项目宪法取不到\n\n%s\n" % (data.get("reason") or "源台账读取失败")
    c = data.get("counts") or {}
    L = [
        "# 项目宪法（自动投影，勿手改）", "",
        "> 生成时间 %s　·　由 `python3 ops/truthctl.py constitution` 从约束/候选/判断台账投影" % generated_at,
        "> 只有人确认过的约束才生效；候选与驳回项保留原话供追溯，不自动参与执行。", "",
        "生效 %d · 待确认 %d · 已确认待落账 %d · 已驳回 %d · 判断逻辑 %d" % (
            c.get("active", 0), c.get("proposed", 0), c.get("promoted", 0),
            c.get("dismissed", 0), c.get("judgments", 0)),
        "",
        "## 一、生效约束", "",
    ]
    if data.get("constraints"):
        for row in data["constraints"]:
            tag = "%s/%s/%s" % (
                KIND_LABEL.get(str(row.get("kind")), str(row.get("kind") or "预期")),
                _scope_line(row),
                ENFORCEABLE_LABEL.get(str(row.get("enforceable")), "状态取不到"))
            L.append("- **%s** [%s] %s" % (row.get("id"), tag, _text(row)))
            cmd = str(row.get("cmd") or "").strip()
            if cmd:
                L.append("  - 验证命令：`%s`" % cmd)
    else:
        L.append("- （还没有人确认过的生效约束）")
    L += ["", "## 二、待确认候选（用户说过，但尚未生效）", ""]
    if data.get("proposed"):
        for row in data["proposed"]:
            priority = PRIORITY_LABEL.get(str(row.get("priority")), "普通")
            tag = "%s优先/%s/%s" % (
                priority,
                SCOPE_LABEL.get(str(row.get("scope_class")), str(row.get("scope_class") or "项目")),
                KIND_LABEL.get(str(row.get("kind")), str(row.get("kind") or "预期")))
            L.append("- **%s** [%s] %s" % (row.get("id"), tag, _text(row)))
            signals = row.get("signals") or []
            if signals:
                L.append("  - 命中：%s" % "、".join(str(s) for s in signals))
            if row.get("mentions") is not None and row.get("threads") is not None:
                L.append("  - 重复证据：出现 %s 次 / 跨 %s 个会话" % (
                    row.get("mentions"), row.get("threads")))
    else:
        L.append("- （没有待确认候选）")

    L += ["", "## 三、已确认待落账", ""]
    if data.get("promoted"):
        for row in data["promoted"]:
            L.append("- **%s** → `%s` %s" % (
                row.get("id"), row.get("constraint_id") or "C-id 取不到", _text(row)))
    else:
        L.append("- （没有已确认但还没在生效约束里看到的候选）")

    L += ["", "## 四、已驳回（不自动复活）", ""]
    if data.get("dismissed"):
        for row in data["dismissed"]:
            reason = str(row.get("reason") or "").strip() or "未写原因"
            L.append("- **%s** %s　—— 驳回原因：%s" % (row.get("id"), _text(row), reason))
    else:
        L.append("- （没有已驳回条目）")

    L += ["", "## 五、判断逻辑", ""]
    if data.get("judgments"):
        for row in data["judgments"]:
            scope = "、".join(row.get("scope") or []) if isinstance(row.get("scope"), list) else str(row.get("scope") or "")
            L.append("- **%s** [%s] %s" % (row.get("id"), scope or "process",
                                           row.get("statement") or row.get("title") or ""))
            if row.get("why"):
                L.append("  - 为什么：%s" % row.get("why"))
    else:
        L.append("- （还没有蒸馏出的判断逻辑）")

    L += ["", "## 六、未接线与缺口", ""]
    if data.get("gaps"):
        for gap in data["gaps"]:
            label = str(gap.get("id") or "书面文档")
            L.append("- `%s` %s —— %s" % (label, gap.get("text"), gap.get("reason")))
    else:
        L.append("- （没有已识别缺口）")

    L += ["", "## 七、书面宪法引用", ""]
    for doc in data.get("written") or []:
        L.append("### `%s`" % doc["path"])
        if doc.get("missing"):
            L.append("")
            L.append("- （文件不存在；上面「未接线与缺口」已记一笔）")
            continue
        text = doc.get("text") or ""
        lines = text.splitlines() or [""]
        shown = lines[:160]
        L.append("")
        L += ["> " + line if line else ">" for line in shown]
        if len(lines) > len(shown):
            L.append("")
            L.append(">（原文 %d 行，这里只投影前 %d 行；完整内容以源文件为准）" % (
                len(lines), len(shown)))
        L.append("")
    return "\n".join(L).rstrip() + "\n"


def write_page(repo=None, if_changed=False, generated_at=None) -> int:
    base = repo_path(repo)
    data = collect(base)
    if data.get("status") != "ok":
        print("项目宪法取不到：" + str(data.get("reason") or "源台账读取失败"), file=sys.stderr)
        return 2
    stamp = generated_at or time.strftime("%Y-%m-%d %H:%M:%S")
    text = render(data, stamp)
    path = base / "docs" / "PROJECT_CONSTITUTION.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    wrote = True
    if if_changed and path.exists():
        marker = "> 生成时间 "
        def without_stamp(value: str) -> list:
            return [line for line in value.splitlines() if not line.startswith(marker)]
        wrote = without_stamp(path.read_text(encoding="utf-8")) != without_stamp(text)
    if wrote:
        path.write_text(text, encoding="utf-8")
        print("已写出 %s" % path.relative_to(base))
    else:
        print("项目宪法内容未变化，保留原生成时间：%s" % path.relative_to(base))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成项目宪法页（只读投影，不写源台账）")
    ap.add_argument("--if-changed", action="store_true", help="内容不变时不重写生成时间")
    args = ap.parse_args(list(sys.argv[1:] if argv is None else argv))
    return write_page(if_changed=args.if_changed)


if __name__ == "__main__":
    raise SystemExit(main())
