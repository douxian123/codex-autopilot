#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真相层 active 去重：同一决策/判断/约束/进展不许因重复落盘出现两份。

背景（2026-11-18）：PM/验收闭环落盘两次后，decisions、judgments、constraints
和 progress 都出现过同内容不同 id 的双份 active 行。`PRODUCT_MAINLINE.md`
于是把同一条架构决策、同一件已完成的事写两遍；约束注入也会把同一条要求
给执行者两遍。JSONL 是 append-only，不能删历史行，只能追加 `status=retired`
的状态覆盖行。

本门禁锁三件事：
  1) 当前仓库的 active 投影里，决策/判断/约束/进展没有同内容重复；
  2) `truthctl retire` 真能按 id 追加停用行，历史物理行不丢，active 投影只剩一份；
  3) 停用后的进展不再渲染进产品主线和项目状态，避免人看到重复账。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
REPO = OPS.parent


def read_rows(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def active_rows(path: Path) -> list:
    latest = {}
    for row in read_rows(path):
        rid = row.get("id")
        if rid:
            latest[rid] = row
    return [row for row in latest.values()
            if (row.get("status") or "active") == "active"]


def duplicate_values(path: Path, key: str) -> list:
    seen = set()
    dup = []
    for row in active_rows(path):
        value = " ".join(str(row.get(key) or row.get("title") or "").split())
        if not value:
            continue
        if value in seen:
            dup.append((row.get("id"), value))
        seen.add(value)
    return dup


def main() -> int:
    fails = 0

    def check(label, ok, detail=""):
        nonlocal fails
        if ok:
            print(f"  ok   {label}")
        else:
            print(f"  FAIL {label}" + (f"（{detail}）" if detail else ""))
            fails += 1

    checks = [
        ("决策", "ops/truth/decisions.jsonl", "title"),
        ("判断逻辑", "ops/truth/judgments.jsonl", "statement"),
        ("约束", "ops/truth/constraints.jsonl", "statement"),
        ("进展", "ops/truth/progress.jsonl", "summary"),
    ]
    for label, rel, key in checks:
        path = REPO / rel
        dups = duplicate_values(path, key)
        check(f"{label} active 投影没有重复", not dups, dups)

    with tempfile.TemporaryDirectory(prefix="truth-dedupe-gate-") as tmp:
        root = Path(tmp)
        (root / "ops" / "truth").mkdir(parents=True)
        (root / "ops" / "tasks").mkdir(parents=True)
        (root / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
        (root / "ops" / "truth" / "product_state.json").write_text(
            json.dumps({"updated": "2026-11-18", "modules": []}), encoding="utf-8")
        for name in ("ledger", "progress", "decisions", "acceptance",
                     "judgments", "constraints"):
            (root / "ops" / "truth" / f"{name}.jsonl").write_text("", encoding="utf-8")

        decisions = root / "ops" / "truth" / "decisions.jsonl"
        decisions.write_text(
            json.dumps({"id": "D-0001", "type": "decision", "title": "同一架构决策",
                        "status": "active"}, ensure_ascii=False) + "\n"
            + json.dumps({"id": "D-0002", "type": "decision", "title": "同一架构决策",
                          "status": "active"}, ensure_ascii=False) + "\n",
            encoding="utf-8")

        env = {**os.environ, "KG_REPO": str(root),
               "KG_TRUTH_ID_STATE_ROOT": str(root / "truth-ids")}
        run = subprocess.run(
            [sys.executable, str(OPS / "truthctl.py"), "retire", "decision", "D-0001"],
            cwd=str(REPO), env=env, capture_output=True, text=True)
        check("retire 命令成功", run.returncode == 0,
              (run.stdout + run.stderr)[-200:])
        check("历史物理行没被删", len(read_rows(decisions)) == 3,
              len(read_rows(decisions)))
        active = active_rows(decisions)
        check("active 投影只剩一条", len(active) == 1, active)
        check("保留的是未停用的那条", bool(active) and active[0].get("id") == "D-0002",
              active)

        judgments = root / "ops" / "truth" / "judgments.jsonl"
        judgments.write_text(
            json.dumps({"id": "J-0001", "type": "judgment", "title": "同一判断",
                        "statement": "同一判断", "status": "active"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        run = subprocess.run(
            [sys.executable, str(OPS / "truthctl.py"), "retire", "judgment", "J-0001"],
            cwd=str(REPO), env=env, capture_output=True, text=True)
        check("判断 retire 成功", run.returncode == 0,
              (run.stdout + run.stderr)[-200:])
        check("判断 active 投影为空", not active_rows(judgments),
              active_rows(judgments))

        constraints = root / "ops" / "truth" / "constraints.jsonl"
        constraints.write_text(
            json.dumps({"id": "C-0001", "type": "constraint", "statement": "同一约束",
                        "status": "active"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        run = subprocess.run(
            [sys.executable, str(OPS / "truthctl.py"), "retire", "constraint", "C-0001"],
            cwd=str(REPO), env=env, capture_output=True, text=True)
        check("约束 retire 成功", run.returncode == 0,
              (run.stdout + run.stderr)[-200:])
        check("约束 active 投影为空", not active_rows(constraints),
              active_rows(constraints))

        progress = root / "ops" / "truth" / "progress.jsonl"
        progress.write_text(
            json.dumps({"id": "P-0001", "type": "progress", "summary": "停用的进展",
                        "status": "done"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        run = subprocess.run(
            [sys.executable, str(OPS / "truthctl.py"), "retire", "progress", "P-0001"],
            cwd=str(REPO), env=env, capture_output=True, text=True)
        check("进展 retire 成功", run.returncode == 0,
              (run.stdout + run.stderr)[-200:])
        check("进展历史物理行没被删", len(read_rows(progress)) == 2,
              len(read_rows(progress)))
        check("进展 active 投影为空", not active_rows(progress),
              active_rows(progress))

        snap = subprocess.run(
            [sys.executable, str(OPS / "truthctl.py"), "snapshot"],
            cwd=str(REPO), env=env, capture_output=True, text=True)
        check("snapshot 成功", snap.returncode == 0,
              (snap.stdout + snap.stderr)[-200:])
        status_path = root / "docs" / "PROJECT_STATUS.md"
        status_text = status_path.read_text(encoding="utf-8") if status_path.exists() else ""
        check("停用的进展不进项目状态", "停用的进展" not in status_text,
              status_text[-200:])

        synth = subprocess.run(
            [sys.executable, str(OPS / "truthctl.py"), "synthesize"],
            cwd=str(REPO), env=env, capture_output=True, text=True)
        check("synthesize 成功", synth.returncode == 0,
              (synth.stdout + synth.stderr)[-200:])
        mainline_path = root / "docs" / "PRODUCT_MAINLINE.md"
        mainline_text = mainline_path.read_text(encoding="utf-8") if mainline_path.exists() else ""
        check("停用的进展不进产品主线", "停用的进展" not in mainline_text,
              mainline_text[-200:])

        acceptance = root / "ops" / "truth" / "acceptance.jsonl"
        acceptance.write_text(
            json.dumps({"id": "A-0001", "type": "acceptance", "title": "误登记的验收项",
                        "status": "blocked", "needs_human": False}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        void_run = subprocess.run(
            [sys.executable, str(OPS / "truthctl.py"), "accept", "--id", "A-0001",
             "--status", "void", "--note", "误登记，作废"],
            cwd=str(REPO), env=env, capture_output=True, text=True)
        check("误登记验收项可作废", void_run.returncode == 0,
              (void_run.stdout + void_run.stderr)[-200:])
        check("作废保留历史物理行", len(read_rows(acceptance)) == 2,
              len(read_rows(acceptance)))

        nxt = subprocess.run(
            [sys.executable, str(OPS / "truthctl.py"), "next"],
            cwd=str(REPO), env=env, capture_output=True, text=True)
        check("作废项不进 next 卡点", "误登记的验收项" not in nxt.stdout,
              nxt.stdout[-300:])
        snap = subprocess.run(
            [sys.executable, str(OPS / "truthctl.py"), "snapshot"],
            cwd=str(REPO), env=env, capture_output=True, text=True)
        status_text = status_path.read_text(encoding="utf-8") if status_path.exists() else ""
        check("作废项不进项目状态卡点", "误登记的验收项" not in status_text,
              status_text[-300:])
        synth = subprocess.run(
            [sys.executable, str(OPS / "truthctl.py"), "synthesize"],
            cwd=str(REPO), env=env, capture_output=True, text=True)
        mainline_text = mainline_path.read_text(encoding="utf-8") if mainline_path.exists() else ""
        check("作废项不进产品主线卡点", "误登记的验收项" not in mainline_text,
              mainline_text[-300:])

    print()
    if fails:
        print(f"结果：{fails} 项未通过")
    else:
        print("selftest ok: 真相层 active 无重复，retire/void 只追加不停删历史。")
    return fails


if __name__ == "__main__":
    raise SystemExit(main())
