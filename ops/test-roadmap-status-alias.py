#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""产品路线的状态词必须归一，否则「当前阶段」会静默指错。

背景（2026-11-16 实测）：项目自己的 `ops/truthctl.py` 把 `in_progress` 文档化为
合法的「进行中」（`ROADMAP_LABEL` 里与 `doing` 同义），ThoughLoom 的 mainline.json
里 5 条路线也确实全用 `in_progress`。但面板 / 文本状态 / 主线摘要只认 `doing`，
于是：

  * 「当前阶段」永远匹配不到正在做的那一步，只能退到 `next` / 首个未完成项；
  * 用户看到的「现在在哪个步骤」是错的，而系统不报任何错。

这条门禁锁住：常见状态别名都要归一到 done|doing|next|todo，且 `_roadmap()` 与
`_mainline_summary()` 两个消费点用的是同一套归一结果（不各写一遍）。

退出码 = 未通过项数。
"""

import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))


def main():
    fails = 0

    def check(label, ok, detail=""):
        nonlocal fails
        if ok:
            print(f"  ok   {label}")
        else:
            print(f"  FAIL {label}" + (f"（{detail}）" if detail else ""))
            fails += 1

    with tempfile.TemporaryDirectory(prefix="roadmap-alias-") as tmp:
        root = Path(tmp)
        (root / "ops" / "tasks").mkdir(parents=True)
        (root / "ops" / "truth").mkdir(parents=True)
        (root / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
        for name in ("ledger.jsonl", "progress.jsonl", "decisions.jsonl", "acceptance.jsonl"):
            (root / "ops" / "truth" / name).write_text("", encoding="utf-8")
        (root / "ops" / "truth" / "mainline.json").write_text(json.dumps({
            "product": "测试产品", "stage": "阶段 1",
            "roadmap": [
                {"step": "第一步", "status": "done"},
                {"step": "第二步", "status": "in_progress"},
                {"step": "第三步", "status": "todo"},
            ],
        }, ensure_ascii=False), encoding="utf-8")

        os.environ["KG_REPO"] = str(root)
        # 模块在 import 期会解析状态根，这里必须重新载入一次。
        sys.modules.pop("central_mcp", None)
        m = importlib.import_module("central_mcp")

        norm = m._normalize_roadmap_status
        check("in_progress → doing", norm("in_progress") == "doing", norm("in_progress"))
        check("in-progress → doing", norm("in-progress") == "doing", norm("in-progress"))
        check("wip → doing", norm("wip") == "doing", norm("wip"))
        check("completed → done", norm("completed") == "done", norm("completed"))
        check("doing 原样保留", norm("doing") == "doing")
        check("done 原样保留", norm("done") == "done")
        check("next 原样保留", norm("next") == "next")
        check("todo 原样保留", norm("todo") == "todo")
        check("认不出的状态归 todo（不猜成进行中）", norm("zzz-unknown") == "todo")
        check("None 归 todo", norm(None) == "todo")
        check("大小写不敏感", norm("IN_PROGRESS") == "doing")

        rows = m._roadmap()
        check("_roadmap 把 in_progress 归一成 doing",
              [r["status"] for r in rows] == ["done", "doing", "todo"],
              [r["status"] for r in rows])
        cur = next((r for r in rows if r["status"] == "doing"), None)
        check("能定位到真正在做的那一步", (cur or {}).get("step") == "第二步", cur)

        # 两个消费点必须用同一套结果，不能各写一遍。
        summary = m._mainline_summary()
        srows = summary.get("roadmap") or []
        check("_mainline_summary 与 _roadmap 状态一致",
              [r.get("status") for r in srows] == [r["status"] for r in rows],
              [r.get("status") for r in srows])
        check("主线摘要里能定位到真正在做的那步",
              next((r.get("step") for r in srows if r.get("status") == "doing"), None)
              == "第二步")

    print()
    if fails:
        print(f"结果：{fails} 项未通过")
    else:
        print("selftest ok: 产品路线状态词已归一，当前阶段不再指错。")
    return fails


if __name__ == "__main__":
    raise SystemExit(main())
