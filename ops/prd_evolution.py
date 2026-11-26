#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRD 持续迭代闭环。

上下文管家从全量反馈里提出 PRD 修订候选，PM 管家基于研究和现场证据做
`adopt / adapt / defer / reject / ask_user` 决策；不确定时必须回到用户。
没有收敛的修订候选会挡住 execute，不能让旧 PRD 继续驱动新施工。

这不是第二个真相层：只读上下文档案，追加式写 `ops/truth/prd_revisions.jsonl`。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

TRUTH_FILE = Path("ops") / "truth" / "prd_revisions.jsonl"
DECISIONS = {"adopt", "adapt", "defer", "reject", "ask_user"}
STATUSES = {"proposed", "ask_user", "needs_prd_update", "decided"}
CONTEXT_MARKERS = (
    "PRD", "产品", "方案", "路线", "职责", "面板", "上下文", "自动挡",
    "管家", "入口", "验收", "预期", "需求",
)


def _repo(repo=None) -> Path:
    return Path(repo or os.environ.get("KG_REPO") or Path.cwd()).expanduser().resolve()


def _target(repo: Path) -> Path:
    return repo / TRUTH_FILE


@contextlib.contextmanager
def _lock(repo: Path):
    lock = repo / "ops" / "truth" / ".truthctl.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
        yield
        return
    with lock.open("a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _read(repo: Path, errors: list | None = None) -> list:
    path = _target(repo)
    if not path.exists():
        return []
    rows = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            if errors is not None:
                errors.append(f"{path.name}:{line_no}: JSON 损坏")
            return []
        if not isinstance(row, dict):
            if errors is not None:
                errors.append(f"{path.name}:{line_no}: 不是对象行")
            return []
        rows.append(row)
    return rows


def _append(repo: Path, row: dict) -> dict:
    target = _target(repo)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _lock(repo):
        data = _read(repo)
        nums = []
        for item in data:
            match = re.match(r"^PR-(\d+)$", str(item.get("revision_id") or ""))
            if match:
                nums.append(int(match.group(1)))
        row = dict(row)
        row.setdefault("revision_id", "PR-%04d" % (max(nums or [0]) + 1))
        row.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
        row.setdefault("type", "prd_revision")
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row


def _latest(rows: list) -> dict:
    out = {}
    for row in rows:
        rid = row.get("revision_id")
        if rid:
            out[rid] = row
    return out


def collect_context_candidates(repo=None) -> dict:
    """从上下文档案投影 PRD 修订候选，只读，不自动写 PRD。"""
    base = _repo(repo)
    truth = base / "ops" / "truth"
    try:
        import context_archive
        data = context_archive.collect(truth)
    except Exception as exc:  # 模块坏了必须说 unknown
        return {"status": "unknown", "items": [], "reason": "上下文档案读取失败：%s" % exc}
    if data.get("status") != "ok":
        return {"status": "unknown", "items": [],
                "reason": data.get("reason") or "上下文档案取不到"}
    items = []
    for row in data.get("items") or []:
        text = str(row.get("text") or "")
        if row.get("status") in ("dismissed",):
            continue
        if not any(marker.lower() in text.lower() for marker in CONTEXT_MARKERS):
            continue
        items.append({
            "source_id": row.get("id"),
            "source": row.get("source") or "context_archive",
            "status": row.get("status"),
            "text": text,
            "evidence": row.get("evidence") or [],
            "scope": row.get("scope") or [],
            "target_section": "docs/PRODUCT_CONTRACT.md",
            "question": "这条反馈是否应更新 PRD 的产品职责或验收标准？",
        })
    return {"status": "ok", "items": items[:20], "count": len(items),
            "note": "上下文管家只提出候选；PM 决策后才写入 PRD。"}


def propose(repo=None, *, text, source="intake", source_id="", target_section="docs/PRODUCT_CONTRACT.md",
            evidence="", confidence="medium", scope=None, question="",
            affects_execution=True, severity="medium") -> dict:
    """提出一条 PRD 修订候选。不确定时必须带 question，交给用户确认。"""
    text = " ".join(str(text or "").split())
    if not text:
        raise ValueError("PRD 修订候选不能为空")
    uncertainty = bool(question.strip())
    if uncertainty and confidence == "high":
        raise ValueError("高置信修订不应同时标为待用户确认；先降低置信度")
    row = {
        "status": "ask_user" if uncertainty else "proposed",
        "text": text,
        "source": source,
        "source_id": source_id,
        "target_section": target_section,
        "evidence": evidence,
        "confidence": confidence,
        "scope": [str(x) for x in (scope or []) if str(x).strip()],
        "question": question.strip(),
        "affects_execution": bool(affects_execution),
        "severity": severity,
        "decision": None,
        "decision_why": "",
        "decision_evidence": "",
        "prd_updated": False,
    }
    return _append(_repo(repo), row)


def decide(repo=None, *, revision_id, decision, why="", evidence="",
           prd_updated=False, prd_evidence="") -> dict:
    """PM 管家对 PRD 修订候选做决策；采用/适配必须证明 PRD 真被更新。"""
    decision = str(decision or "").lower()
    if decision not in DECISIONS:
        raise ValueError("decision 必须是 %s" % ", ".join(sorted(DECISIONS)))
    base = _repo(repo)
    latest = _latest(_read(base)).get(str(revision_id))
    if not latest:
        raise ValueError("PRD 修订候选不存在：%s" % revision_id)
    why = " ".join(str(why or "").split())
    if not why:
        raise ValueError("PM 决策必须写明理由")
    row = dict(latest)
    row.update({
        "decision": decision,
        "decision_why": why,
        "decision_evidence": str(evidence or ""),
        "decided_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "prd_updated": bool(prd_updated),
        "prd_evidence": str(prd_evidence or ""),
    })
    if decision == "ask_user":
        if not row.get("question"):
            row["question"] = "这条 PRD 修订是否符合你的产品预期？"
        row["status"] = "ask_user"
    elif decision in {"adopt", "adapt"}:
        if not prd_updated or "docs/PRODUCT_CONTRACT.md" not in str(prd_evidence or ""):
            row["status"] = "needs_prd_update"
            row["reason"] = "采用/适配必须先更新 docs/PRODUCT_CONTRACT.md，并留下可复核证据"
        else:
            row["status"] = "decided"
            row["reason"] = "PRD 已同步更新"
    else:
        row["status"] = "decided"
        row["reason"] = "PM 已决策 %s，不需要改 PRD" % decision
    return _append(base, row)


def summary(repo=None) -> dict:
    base = _repo(repo)
    errors: list = []
    rows = _read(base, errors)
    if errors:
        return {"status": "unknown", "count": None, "attention_count": None,
                "items": [], "reason": "PRD 修订台账读不到：" + "；".join(errors)}
    latest = _latest(rows)
    items = []
    for row in latest.values():
        items.append({
            "revision_id": row.get("revision_id"),
            "text": row.get("text"),
            "status": row.get("status"),
            "decision": row.get("decision"),
            "question": row.get("question"),
            "target_section": row.get("target_section"),
            "source": row.get("source"),
            "severity": row.get("severity"),
            "prd_updated": bool(row.get("prd_updated")),
            "why": row.get("decision_why") or row.get("reason") or "",
        })
    items.sort(key=lambda r: (str(r.get("revision_id") or ""),))
    attention = [x for x in items
                 if x.get("status") in {"proposed", "ask_user", "needs_prd_update"}]
    return {
        "status": "attention" if attention else "ok",
        "count": len(items),
        "attention_count": len(attention),
        "items": items,
        "reason": None if not attention else "仍有 PRD 修订候选未收敛，不能按旧 PRD 继续施工",
        "note": "上下文管家提候选，PM 管家做决策；不确定项回到用户，采用/适配必须同步更新 PRD。",
    }


def execution_gate(repo=None) -> dict:
    """执行前 PRD 同步闸：未收敛的高影响修订不能继续开工。"""
    data = summary(repo)
    if data.get("status") == "unknown":
        return {"status": "blocked", "reason": data.get("reason"), "prd": data}
    blocking = [x for x in data.get("items") or []
                if x.get("status") in {"ask_user", "needs_prd_update"}
                or (x.get("status") == "proposed"
                    and x.get("severity") in {"high", "critical"})]
    if blocking:
        return {"status": "blocked",
                "reason": "PRD 仍有未收敛修订：%s" % "、".join(
                    str(x.get("revision_id")) for x in blocking),
                "prd": data}
    return {"status": "pass", "reason": "PRD 修订已收敛或暂无高影响待决项", "prd": data}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PRD 持续迭代闭环")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("candidates", help="从上下文档案投影修订候选")
    sub.add_parser("status", help="查看 PRD 修订台账摘要")
    sub.add_parser("gate", help="检查 PRD 同步执行闸")
    p_propose = sub.add_parser("propose", help="提出 PRD 修订候选")
    p_propose.add_argument("--text", required=True)
    p_propose.add_argument("--source", default="intake")
    p_propose.add_argument("--source-id", default="")
    p_propose.add_argument("--target-section", default="docs/PRODUCT_CONTRACT.md")
    p_propose.add_argument("--evidence", default="")
    p_propose.add_argument("--confidence", default="medium")
    p_propose.add_argument("--scope", default="")
    p_propose.add_argument("--question", default="")
    p_propose.add_argument("--severity", default="medium")
    p_propose.add_argument("--no-affects-execution", action="store_true")
    p_decide = sub.add_parser("decide", help="PM 管家决策")
    p_decide.add_argument("revision_id")
    p_decide.add_argument("decision", choices=sorted(DECISIONS))
    p_decide.add_argument("--why", required=True)
    p_decide.add_argument("--evidence", default="")
    p_decide.add_argument("--prd-updated", action="store_true")
    p_decide.add_argument("--prd-evidence", default="")
    args = parser.parse_args(argv)
    repo = _repo()
    if args.cmd == "candidates":
        result = collect_context_candidates(repo)
    elif args.cmd == "status":
        result = summary(repo)
    elif args.cmd == "gate":
        result = execution_gate(repo)
    elif args.cmd == "propose":
        result = propose(
            repo, text=args.text, source=args.source, source_id=args.source_id,
            target_section=args.target_section, evidence=args.evidence,
            confidence=args.confidence, scope=args.scope.split(":") if args.scope else [],
            question=args.question, affects_execution=not args.no_affects_execution,
            severity=args.severity)
    else:
        result = decide(repo, revision_id=args.revision_id, decision=args.decision,
                        why=args.why, evidence=args.evidence,
                        prd_updated=args.prd_updated, prd_evidence=args.prd_evidence)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") not in {"blocked", "unknown"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
