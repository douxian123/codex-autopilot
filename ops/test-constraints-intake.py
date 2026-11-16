#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上下文管家：约束台账必须能写、能按 scope 精准调取、能注入执行者 prompt。

背景（2026-11-17）：用户要的「上下文管家」是——任何入口提到的预期/返修/决策/禁忌
自动收成项目约束，**说过一次就不再犯**。此前只有 `judgments.jsonl`（判断逻辑，
几乎为空、且没有 scope 也没有强制回路），写了也不会被后续实现自动引用。

本门禁锁四件事：
  1) 约束能写进追加式台账，且按 id 收敛（同 id 状态覆盖，不重复计条）；
  2) scope 匹配是「精准调取」：目录/glob/文件名/全局四种写法都对，且**不该命中的
     就不带上** —— 不是把全部上下文喂进去；
  3) 执行者 prompt 里确实带上了匹配约束（这是「不是提醒、是直接给」的强制回路），
     且不匹配的约束不会混进去；
  4) 停用后的条数不许算虚（曾把 1 条报成 2 条）。

退出码 = 未通过项数。
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
REPO = OPS.parent


def main():
    fails = 0

    def check(label, ok, detail=""):
        nonlocal fails
        if ok:
            print(f"  ok   {label}")
        else:
            print(f"  FAIL {label}" + (f"（{detail}）" if detail else ""))
            fails += 1

    with tempfile.TemporaryDirectory(prefix="constraints-gate-") as tmp:
        root = Path(tmp)
        (root / "ops" / "truth").mkdir(parents=True)
        (root / "ops" / "tasks").mkdir(parents=True)
        (root / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
        for name in ("ledger", "progress", "decisions", "acceptance",
                     "judgments", "constraints"):
            (root / "ops" / "truth" / f"{name}.jsonl").write_text("", encoding="utf-8")
        (root / "ops" / "truth" / "product_state.json").write_text(
            json.dumps({"updated": "2026-11-17", "modules": []}), encoding="utf-8")

        env = {**os.environ, "KG_REPO": str(root),
               "KG_TRUTH_ID_STATE_ROOT": str(root / "truth-ids")}

        def truthctl(*args, extra=None):
            e = dict(env)
            if extra:
                e.update(extra)
            return subprocess.run(
                [sys.executable, str(OPS / "truthctl.py"), *args],
                cwd=str(REPO), env=e, capture_output=True, text=True)

        r1 = truthctl("constraints", "add", "永远不要 force push",
                      extra={"KG_CONSTRAINT_KIND": "taboo",
                             "KG_CONSTRAINT_ENFORCEABLE": "gate",
                             "KG_CONSTRAINT_CMD": "python3 ops/selftest.py"})
        check("constraints add（全局）成功", r1.returncode == 0, r1.stderr[-200:])
        check("返回了 C- 编号", "C-0001" in r1.stdout, r1.stdout[-200:])

        # 反转样本：标了 gate 却写不出命令，就是「声称有门禁、实际没有」。
        # 这曾经是合法写法，CLI/面板会渲染成「有门禁」却从不执行（2026-11-17 实测）。
        # 现在必须在写入端直接拒绝，从源头堵住假绿灯。
        r_bad = truthctl("constraints", "add", "标了 gate 却没命令",
                         extra={"KG_CONSTRAINT_KIND": "taboo",
                                "KG_CONSTRAINT_ENFORCEABLE": "gate"})
        check("gate 缺命令被拒（不再允许假绿灯入账）",
              r_bad.returncode != 0, r_bad.stdout[-200:])

        r2 = truthctl("constraints", "add", "backend 不要加隐式网络依赖",
                      extra={"KG_CONSTRAINT_KIND": "expectation",
                             "KG_CONSTRAINT_SCOPE": "backend"})
        check("constraints add（带 scope）成功", r2.returncode == 0, r2.stderr[-200:])

        r3 = truthctl("constraints", "add", "面板别堆内部记账读数",
                      extra={"KG_CONSTRAINT_KIND": "rework",
                             "KG_CONSTRAINT_SCOPE": "plugin/ui/*.html",
                             "KG_CONSTRAINT_ENFORCEABLE": "checklist"})
        check("constraints add（第三条）成功", r3.returncode == 0, r3.stderr[-200:])

        rows = [json.loads(l) for l in
                (root / "ops" / "truth" / "constraints.jsonl").read_text(
                    encoding="utf-8").splitlines() if l.strip()]
        check("台账里真写了 3 条", len(rows) == 3, len(rows))
        check("type 都是 constraint", all(r.get("type") == "constraint" for r in rows))

        sys.path.insert(0, str(OPS))
        import constraints as cm

        path = root / "ops" / "truth" / "constraints.jsonl"
        check("全局约束对任意文件都命中",
              cm.scope_matches([], ["whatever/x.ts"]) is True)
        check("目录 scope 命中子文件",
              cm.scope_matches(["backend"], ["backend/src/x.ts"]) is True)
        check("目录 scope 不命中别的目录",
              cm.scope_matches(["backend"], ["frontend/x.ts"]) is False)
        check("glob scope 命中",
              cm.scope_matches(["plugin/ui/*.html"], ["plugin/ui/panel.html"]) is True)
        check("glob scope 不误命中",
              cm.scope_matches(["plugin/ui/*.html"], ["backend/x.ts"]) is False)
        check("文件名写法命中", cm.scope_matches(["panel.html"],
                                            ["plugin/ui/panel.html"]) is True)

        backend_hits = cm.matching_constraints(path, ["backend/src/x.ts"])
        ids = sorted(r["id"] for r in backend_hits)
        check("backend 改动只带全局 + backend 这两条", ids == ["C-0001", "C-0002"], ids)
        ui_hits = cm.matching_constraints(path, ["plugin/ui/panel.html"])
        ids_ui = sorted(r["id"] for r in ui_hits)
        check("UI 改动只带全局 + UI 这条", ids_ui == ["C-0001", "C-0003"], ids_ui)
        check("不相干的约束不会一起带上（backend 那一轮没有 UI 约束）",
              "C-0003" not in [r["id"] for r in backend_hits])

        # 注入：执行者 prompt 里必须真带上匹配约束；不匹配的不许混进去。
        import importlib
        os.environ["KG_REPO"] = str(root)
        sys.modules.pop("central_daemon", None)
        dm = importlib.import_module("central_daemon")
        prompt = dm._exec_prompt({"id": "L-9001", "kind": "bug", "title": "面板降噪",
                                  "files": ["plugin/ui/panel.html"]})
        check("prompt 带上了约束段", "本项目已确认的约束" in prompt)
        check("prompt 里有命中的全局约束", "C-0001" in prompt)
        check("prompt 里有命中的 UI 约束", "C-0003" in prompt)
        check("prompt 里没有不相干的 backend 约束", "C-0002" not in prompt)

        # 停用：条数不许算虚（曾把 1 条报成 2 条）。
        r4 = truthctl("constraints", "retire", "C-0002")
        check("retire 成功", r4.returncode == 0, r4.stderr[-200:])
        r5 = truthctl("constraints")
        check("停用后报 2 条生效", "2 条生效" in r5.stdout, r5.stdout[-200:])
        check("停用后报 1 条已停用", "1 条已停用" in r5.stdout, r5.stdout[-200:])
        check("停用的条目不再出现在生效列表",
              "C-0002" not in r5.stdout.split("调取约定")[0])

    print()
    if fails:
        print(f"结果：{fails} 项未通过")
    else:
        print("selftest ok: 约束能写、能按 scope 精准调取、能注入执行者，停用计数不虚。")
    return fails


if __name__ == "__main__":
    raise SystemExit(main())
