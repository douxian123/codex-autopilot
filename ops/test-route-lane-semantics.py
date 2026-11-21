#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：路由归并必须同 kind，且 inbox 的 lane 字段只放泳道名。

为什么有这条（2026-11-17 真实现场）：
  两条 arch 输入都引用了 `ops/central_daemon.py`，而账本里已有一条 gate 记录也引用
  这个文件。旧 `_candidates` 只看「文件重叠」，于是 arch 需求被并进 gate 台账
  `L-0001`：泳道显示 gate、证据挂在门禁下面，用户看到的是「架构需求变成了门禁」。

  同一次现场还暴露了第二个问题：`route --go` / `promote` 把台账 ID 写进了 inbox 的
  `lane` 字段（`lane: "L-0001"`），于是面板按泳道过滤时整条记录消失。

这条门禁锁住：
  1) kind 不同时，即使引用同一个公共文件，也**不许**归并到旧台账；
  2) 新建 arch 台账的泳道是 `arch`；
  3) route 之后 inbox 的 `lane` 是固定泳道名，`ledger_id` 才指向台账 ID；
  4) 同 kind、同文件、同语义的输入仍能正常归并（修 bug 不许把正常归并也关掉）；
  5) promote 回写同样只写 `ledger_id`，不污染 `lane`。

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
LANES = {"design", "bug", "arch", "gate"}
fails = 0


def check(label, ok, detail=""):
    global fails
    if ok:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}" + (f"（{detail}）" if detail else ""))
        fails += 1


def make_state_root(base: Path) -> Path:
    root = base / "project"
    (root / "ops" / "tasks").mkdir(parents=True)
    (root / "ops" / "truth").mkdir(parents=True)
    for rel in ("ops/tasks/inbox.jsonl", "ops/truth/ledger.jsonl",
                "ops/truth/progress.jsonl", "ops/truth/decisions.jsonl",
                "ops/truth/acceptance.jsonl"):
        (root / rel).write_text("", encoding="utf-8")
    (root / "ops" / "truth" / "mainline.json").write_text("{}\n", encoding="utf-8")
    return root


def read_jsonl(path: Path) -> list:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="route-lane-") as tmp:
        base = Path(tmp)
        root = make_state_root(base)
        env = {**os.environ, "KG_REPO": str(root),
               "KG_AUTOPILOT_HOME": str(base / "autopilot-home"),
               "KG_TRUTH_ID_STATE_ROOT": str(base / "truth-ids")}

        def truthctl(*args, extra=None):
            e = dict(env)
            if extra:
                e.update(extra)
            return subprocess.run(
                [sys.executable, str(OPS / "truthctl.py"), *args],
                cwd=str(REPO), env=e, capture_output=True, text=True)

        def inbox() -> list:
            return read_jsonl(root / "ops" / "tasks" / "inbox.jsonl")

        def ledger() -> list:
            return read_jsonl(root / "ops" / "truth" / "ledger.jsonl")

        # 既有账本：一条 gate，引用公共文件 central_daemon.py。
        (root / "ops" / "truth" / "ledger.jsonl").write_text(
            json.dumps({
                "id": "L-0001", "ts": "2026-11-17 09:00:00", "type": "ledger",
                "kind": "gate", "title": "门禁断言失败", "detail": "门禁断言失败：selftest 挂了",
                "status": "open", "lane": "gate",
                "files": ["ops/central_daemon.py"], "evidence": [],
                "source": "inbox#1",
            }, ensure_ascii=False) + "\n", encoding="utf-8")

        arch_text = "架构：拆分 central_daemon 的依赖，减少耦合"
        (root / "ops" / "tasks" / "inbox.jsonl").write_text(
            json.dumps({
                "ts": "2026-11-17 10:00:00", "text": arch_text, "source": "user",
                "files": ["ops/central_daemon.py"], "kind": "unknown",
                "routed": False, "lane": None,
            }, ensure_ascii=False) + "\n", encoding="utf-8")

        # ---- 1) arch 不许并进 gate ----
        res = truthctl("route", "--go")
        check("route --go 成功", res.returncode == 0, res.stderr[-300:])
        rows = ledger()
        check("arch 输入新建了台账，而不是并进 gate",
              len(rows) == 2, f"{[(r.get('id'), r.get('kind')) for r in rows]}")
        arch = [r for r in rows if r.get("kind") == "arch"]
        gate = [r for r in rows if r.get("id") == "L-0001"]
        check("新建台账 kind=arch", len(arch) == 1,
              f"{[(r.get('id'), r.get('kind')) for r in rows]}")
        check("arch 台账泳道是 arch", arch and arch[0].get("lane") == "arch",
              f"{arch[0].get('lane') if arch else 'missing'}")
        check("旧 gate 台账没有被挂上 arch 证据",
              gate and not gate[0].get("evidence"),
              f"{gate[0].get('evidence') if gate else 'missing'}")

        items = inbox()
        check("inbox lane 是固定泳道名，不是台账 ID",
              items and items[0].get("lane") in LANES,
              f"{items[0].get('lane') if items else 'missing'}")
        check("inbox lane 与新建台账一致",
              items and arch and items[0].get("lane") == arch[0].get("lane"),
              f"{items[0].get('lane') if items else 'missing'}")
        check("inbox ledger_id 指向真实台账 ID",
              items and arch and items[0].get("ledger_id") == arch[0].get("id"),
              f"{items[0].get('ledger_id') if items else 'missing'}")

        # ---- 2) 同 kind、同文件、同语义仍要归并 ----
        gate_text = "门禁失败：selftest 的 central_daemon 检查挂了"
        with (root / "ops" / "tasks" / "inbox.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": "2026-11-17 11:00:00", "text": gate_text, "source": "user",
                "files": ["ops/central_daemon.py"], "kind": "unknown",
                "routed": False, "lane": None,
            }, ensure_ascii=False) + "\n")

        res = truthctl("route", "--go")
        check("同 kind 归并时 route --go 成功", res.returncode == 0, res.stderr[-300:])
        rows = ledger()
        check("同 kind 输入没有新建第二条 gate",
              sum(1 for r in rows if r.get("kind") == "gate") == 1,
              f"{[(r.get('id'), r.get('kind')) for r in rows]}")
        gate = [r for r in rows if r.get("id") == "L-0001"][0]
        check("同 kind 归并确实把证据挂回 L-0001",
              any("门禁失败" in ev for ev in (gate.get("evidence") or [])),
              f"{gate.get('evidence')}")
        items = inbox()
        check("归并项的 inbox lane 是 gate",
              items[1].get("lane") == "gate", f"{items[1].get('lane')}")
        check("归并项的 inbox ledger_id 是 L-0001",
              items[1].get("ledger_id") == "L-0001", f"{items[1].get('ledger_id')}")

        # ---- 3) promote 回写同样不许把台账 ID 写进 lane ----
        with (root / "ops" / "tasks" / "inbox.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": "2026-11-17 12:00:00", "text": "手工提升的一条架构项",
                "source": "user", "files": [], "kind": "unknown",
                "routed": False, "lane": None,
            }, ensure_ascii=False) + "\n")
        res = truthctl("promote", "3", "arch")
        check("promote 成功", res.returncode == 0, res.stderr[-300:])
        items = inbox()
        lane = items[2].get("lane")
        check("promote 不把台账 ID 写进 lane",
              not (isinstance(lane, str) and lane.startswith("L-")), f"{lane}")
        check("promote 把台账 ID 写进 ledger_id",
              isinstance(items[2].get("ledger_id"), str)
              and items[2]["ledger_id"].startswith("L-"),
              f"{items[2].get('ledger_id')}")

    return fails


if __name__ == "__main__":
    code = main()
    print(f"\nroute-lane-semantics: {'PASS' if code == 0 else str(code) + ' 项未通过'}")
    raise SystemExit(code)
