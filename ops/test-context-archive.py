#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上下文档案门禁：自动收录可见、来源可追溯、读失败 unknown、确认闸不旁路。"""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("context_archive_under_test",
                                              OPS / "context_archive.py")
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"  ok   {label}")


def write(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")


def main():
    with tempfile.TemporaryDirectory(prefix="context-archive-") as tmp:
        root = Path(tmp) / "ops" / "truth"
        root.mkdir(parents=True)
        write(root / "constraint_candidates.jsonl", [
            {"id": "CC-0001", "status": "proposed", "text": "以后都不要做破坏性修改",
             "source": "intake", "signals": ["以后都", "不要"], "scope": ["AGENTS.md"],
             "evidence": [{"thread": "t-1", "text": "以后都不要做破坏性修改"}]},
            {"id": "CC-0002", "status": "promoted", "text": "面板只展示值得关注的信息",
             "source": "context_learning", "mentions": 5, "threads": 3,
             "scope": ["plugin/ui/panel.html"], "constraint_id": "C-0004"},
            {"id": "CC-0003", "status": "dismissed", "text": "这条不算",
             "source": "intake", "reason": "用户说不是那个意思"},
            {"id": "CC-0004", "status": "proposed", "text": "旧状态不应覆盖",
             "source": "intake"},
            {"id": "CC-0004", "status": "dismissed", "text": "旧状态不应覆盖",
             "source": "intake", "reason": "已驳回"},
        ])
        write(root / "constraints.jsonl", [
            {"id": "C-0004", "status": "active", "statement": "面板只展示值得关注的信息",
             "source": "context_learning", "scope": ["plugin/ui/panel.html"],
             "enforceable": "checklist"},
            {"id": "C-0005", "status": "active", "statement": "取不到就说取不到"},
            {"id": "C-0006", "status": "retired", "statement": "旧约束"},
        ])

        data = mod.collect(root)
        check("档案可读", data.get("status") == "ok", data)
        c = data.get("counts") or {}
        check("四类状态都保留", c == {"proposed": 1, "promoted": 1,
                                      "dismissed": 2, "active": 2}, c)
        by_id = {r.get("id"): r for r in data.get("items") or []}
        check("自动收录原话可见",
              "以后都不要做破坏性修改" in str(by_id.get("CC-0001", {}).get("text")))
        check("来源与命中信号保留",
              by_id.get("CC-0001", {}).get("source") == "intake"
              and "以后都" in by_id.get("CC-0001", {}).get("signals", []))
        check("证据线程与原话保留",
              by_id.get("CC-0001", {}).get("evidence", [{}])[0].get("thread") == "t-1")
        learned = by_id.get("CC-0002") or {}
        check("自学习重复证据保留",
              learned.get("source") == "context_learning"
              and learned.get("mentions") == 5 and learned.get("threads") == 3)
        check("确认与生效可追溯",
              learned.get("constraint_id") == "C-0004"
              and by_id.get("C-0004", {}).get("status") == "active")
        check("驳回原因保留", by_id.get("CC-0003", {}).get("reason") == "用户说不是那个意思")
        check("同 id 以最新状态为准", by_id.get("CC-0004", {}).get("status") == "dismissed")
        check("停用约束不混进生效档案", "C-0006" not in by_id)
        check("确认入口没有被旁路",
              "在「需要你决策」区" in (data.get("note") or ""))

        broken_dir = Path(tmp) / "missing-truth"
        broken = mod.collect(broken_dir)
        check("目录读不到报 unknown", broken.get("status") == "unknown"
              and broken.get("counts") == {}, broken)
        check("unknown 文案不写成 0 条",
              "不把不知道当成 0 条" in (broken.get("reason") or ""))

        bad_root = Path(tmp) / "bad-truth"
        bad_root.mkdir()
        (bad_root / "constraint_candidates.jsonl").write_text("{", encoding="utf-8")
        (bad_root / "constraints.jsonl").write_text("", encoding="utf-8")
        bad = mod.collect(bad_root)
        check("JSON 损坏 fail closed", bad.get("status") == "unknown"
              and bool(bad.get("unknown")), bad)
        check("读失败不返回半份档案", bad.get("items") == [], bad)

        text = mod.human(data)
        check("CLI 人话输出含四类状态",
              all(word in text for word in ("待确认", "已确认", "已驳回", "生效")), text)

    print("context-archive: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
