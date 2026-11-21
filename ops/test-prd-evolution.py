#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRD 持续迭代门禁：候选、提问、决策、执行闸与损坏台账都要锁住。"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))
SPEC = importlib.util.spec_from_file_location("prd_evolution_under_test",
                                              OPS / "prd_evolution.py")
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"  ok   {label}")


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")


def main():
    with tempfile.TemporaryDirectory(prefix="prd-evolution-") as tmp:
        root = Path(tmp)
        truth = root / "ops" / "truth"
        write_jsonl(truth / "constraint_candidates.jsonl", [{
            "id": "CC-1",
            "status": "proposed",
            "text": "PM 管家必须持续更新 PRD，并基于全量反馈迭代产品方案",
            "source": "user",
            "evidence": [{"thread": "thread-prd", "text": "用户要求持续更新 PRD"}],
            "scope": ["docs/PRODUCT_CONTRACT.md"],
        }, {
            "id": "CC-2",
            "status": "proposed",
            "text": "今天午饭吃什么",
            "source": "user",
        }])

        candidates = mod.collect_context_candidates(root)
        check("上下文档案投影 PRD 候选",
              candidates["status"] == "ok" and len(candidates["items"]) == 1,
              candidates)
        check("候选只提不自动改",
              "PM 决策后才写入 PRD" in candidates["note"], candidates)

        asked = mod.propose(
            root, text="是否把验收截图作为所有人工项必填？",
            source="panel", question="这条是否就是你要的验收口径？",
            confidence="medium", severity="high")
        check("不确定项进入 ask_user",
              asked["status"] == "ask_user" and asked["question"], asked)
        gate = mod.execution_gate(root)
        check("待用户确认项挡 execute",
              gate["status"] == "blocked" and asked["revision_id"] in gate["reason"],
              gate)

        impossible = None
        try:
            mod.propose(root, text="高置信项", confidence="high", question="还要问？")
        except ValueError as exc:
            impossible = exc
        check("高置信与提问不能同时成立", bool(impossible), impossible)

        updated = mod.decide(
            root, revision_id=asked["revision_id"], decision="adopt",
            why="用户已确认这条验收口径", prd_updated=True,
            prd_evidence="docs/PRODUCT_CONTRACT.md:5.3 已更新")
        check("adopt 有 PRD 证据才 decided",
              updated["status"] == "decided" and updated["prd_updated"] is True,
              updated)
        check("收敛后不再挡 execute",
              mod.execution_gate(root)["status"] == "pass",
              mod.execution_gate(root))

        missing_evidence = mod.propose(
            root, text="PM 管家需要持续更新 PRD",
            source="user", severity="high")
        blocked = mod.decide(
            root, revision_id=missing_evidence["revision_id"], decision="adapt",
            why="方向对，但要落到职责", prd_updated=False, prd_evidence="")
        check("adapt 没更新 PRD 会卡住",
              blocked["status"] == "needs_prd_update", blocked)
        check("未更新 PRD 仍挡 execute",
              mod.execution_gate(root)["status"] == "blocked",
              mod.execution_gate(root))

        deferred = mod.decide(
            root, revision_id=missing_evidence["revision_id"], decision="defer",
            why="当前阶段先不动 PRD")
        check("defer 不改 PRD 也能收敛",
              deferred["status"] == "decided"
              and mod.execution_gate(root)["status"] == "pass",
              (deferred, mod.execution_gate(root)))

        summary = mod.summary(root)
        check("面板可读到修订摘要",
              summary["status"] == "attention" or summary["status"] == "ok",
              summary)
        check("摘要不把 unknown 写成 0",
              summary["count"] is not None, summary)

        broken = root / "broken"
        (broken / "ops" / "truth").mkdir(parents=True)
        (broken / mod.TRUTH_FILE).write_text("{坏行\n", encoding="utf-8")
        broken_result = mod.summary(broken)
        check("台账损坏写 unknown，不写 0",
              broken_result["status"] == "unknown"
              and broken_result["count"] is None, broken_result)
        check("损坏台账挡 execute",
              mod.execution_gate(broken)["status"] == "blocked",
              mod.execution_gate(broken))

    print("prd-evolution: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
