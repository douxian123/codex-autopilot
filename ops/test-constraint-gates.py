#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：标了 enforceable=gate 的约束必须真的变成落盘门禁，不是一句空话。

为什么有这条（2026-11-17 实测）：

  上下文管家把用户说过的预期收成 `constraints.jsonl`，其中能机器验证的标
  `enforceable=gate`。CLI 与面板据此渲染出「有门禁」——**可落盘门禁
  `_run_verify_gates()` 只读 central.config.json 的 verify 段，从不读约束台账**。
  于是所有标了 gate 的约束一条都没被执行过，面板却告诉用户「有机器兜底」。
  knowledge-graph 的 C-0001（taboo/gate、连命令都没写）就是活的假绿灯。

修法：`enforceable=gate` 且写了 cmd 的约束，按本次改动路径转成真门禁；
只标 gate 没写命令的走 `gate_gaps()` 单独如实报出，并让 `truthctl constraints add`
在 gate 缺命令时直接拒绝，从源头堵住「声称有门禁却没有东西可跑」。

本门禁锁四件事：
  1. gate + cmd 的约束真被 `_run_verify_gates` 执行（marker 文件是物证）；
  2. 该命令失败时落盘门禁整体判失败（不许把红说成绿）；
  3. 只标 gate、没写命令的约束会被如实列为缺口，而不是混进「有门禁」；
  4. `truthctl constraints add` 在 gate 缺 cmd 时拒绝写入。

退出码 = 未通过项数。
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
REPO = OPS.parent
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}" + (f"（{detail}）" if detail else ""))


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True)


def seed_repo(root: Path, name: str) -> Path:
    """最小可识别状态根：ops/tasks + ops/truth 同时在，且有 commit。"""
    repo = root / name
    (repo / "ops" / "tasks").mkdir(parents=True)
    (repo / "ops" / "truth").mkdir(parents=True)
    (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "truth" / "progress.jsonl").write_text("", encoding="utf-8")
    # 显式 verify=[] ：本测试只想看约束门禁，不想被项目自带门禁干扰。
    (repo / "ops" / "central.config.json").write_text(
        json.dumps({"verify": [], "main_branch": "main"}) + "\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def write_constraints(repo: Path, rows):
    path = repo / "ops" / "truth" / "constraints.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")
    return path


def load_kgctl(repo: Path):
    sys.path.insert(0, str(OPS))
    os.environ["KG_REPO"] = str(repo)
    spec = importlib.util.spec_from_file_location("kgctl_constraint_gate_probe",
                                                  OPS / "kgctl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.REPO = repo
    return mod


def main():
    with tempfile.TemporaryDirectory(prefix="autopilot-constraint-gate-") as tmp:
        root = Path(tmp)
        repo = seed_repo(root, "state")
        marker = root / "gate-ran.marker"
        bad_marker = root / "bad-gate-attempted.marker"

        write_constraints(repo, [
            # 真的门禁：跑起来会创建 marker。
            {"id": "C-0001", "type": "constraint", "statement": "改代码必须留下验证物证",
             "kind": "expectation", "enforceable": "gate", "scope": [],
             "cmd": f"/usr/bin/touch {marker}", "status": "active"},
            # 只标 gate、没写命令：必须被如实报成缺口，不许混进「有门禁」。
            {"id": "C-0002", "type": "constraint", "statement": "声称有门禁但没命令",
             "kind": "taboo", "enforceable": "gate", "scope": [], "status": "active"},
        ])

        kg = load_kgctl(repo)

        # 1) 门禁真被执行：marker 是物证（旧实现这里必然没有 marker）。
        ok, results, out = kg._run_verify_gates(repo)
        check("标了 gate 且写了 cmd 的约束真的被执行", marker.exists(),
              "(marker 不存在，说明约束门禁仍未接线)")
        names = [r.get("name") for r in results]
        check("结果里带 constraint-C-0001 这一条", "constraint-C-0001" in names, names)
        check("该门禁执行成功时整体判 ok", ok is True, out[-300:])

        # 2) 命令失败时必须整体判失败——不许把红说成绿。
        write_constraints(repo, [
            {"id": "C-0003", "type": "constraint", "statement": "这条门禁故意失败",
             "kind": "expectation", "enforceable": "gate", "scope": [],
             "cmd": f"/usr/bin/touch {bad_marker}; exit 3", "status": "active"},
        ])
        ok2, results2, out2 = kg._run_verify_gates(repo)
        check("失败的门禁确实被执行", bad_marker.exists())
        check("门禁失败时整体判失败（不把红说成绿）", ok2 is False, out2[-200:])
        failed = [r for r in results2 if r.get("name") == "constraint-C-0003"]
        check("失败结果如实记录 exit != 0", failed and failed[0].get("ok") is False, results2)

        # 3) 只标 gate、没命令的不算「有门禁」：标签如实、单列缺口。
        sys.path.insert(0, str(OPS))
        import constraints as cm
        path = write_constraints(repo, [
            {"id": "C-0009", "type": "constraint", "statement": "没命令的 gate",
             "kind": "taboo", "enforceable": "gate", "scope": [], "status": "active"},
        ])
        check("gate 无命令不被转成门禁", cm.gate_commands(path, []) == [],
              cm.gate_commands(path, []))
        gaps = cm.gate_gaps(path, [])
        check("gate 无命令如实列为缺口", [g["id"] for g in gaps] == ["C-0009"], gaps)
        gap_line = cm.render_line({"id": "C-0009", "kind": "taboo",
                                   "enforceable": "gate", "statement": "没命令的 gate",
                                   "scope": []})
        check("gate 无命令渲染成「门禁未接」而非「有门禁」",
              "门禁未接" in gap_line and "有门禁" not in gap_line, gap_line)
        wired_line = cm.render_line({"id": "C-0010", "kind": "expectation",
                                     "enforceable": "gate", "cmd": "pytest -q",
                                     "statement": "有命令的 gate", "scope": []})
        check("gate 有命令仍渲染成「有门禁」", "有门禁" in wired_line, wired_line)

    # 4) 写入端闸门：gate 缺命令直接拒绝，从源头堵住假绿灯。
    with tempfile.TemporaryDirectory(prefix="autopilot-constraint-add-") as tmp2:
        repo2 = seed_repo(Path(tmp2), "state")
        (repo2 / "ops" / "truth" / "constraints.jsonl").write_text("", encoding="utf-8")
        env = {**os.environ, "KG_REPO": str(repo2),
               "KG_TRUTH_ID_STATE_ROOT": str(repo2 / "truth-ids")}
        bad = subprocess.run(
            [sys.executable, str(OPS / "truthctl.py"), "constraints", "add", "缺命令的 gate"],
            cwd=str(REPO), env={**env, "KG_CONSTRAINT_ENFORCEABLE": "gate"},
            capture_output=True, text=True)
        check("gate 缺命令时 add 被拒绝", bad.returncode != 0, bad.stdout[-200:])
        check("拒绝理由说清要补 KG_CONSTRAINT_CMD",
              "KG_CONSTRAINT_CMD" in (bad.stderr + bad.stdout),
              (bad.stderr + bad.stdout)[-200:])
        good = subprocess.run(
            [sys.executable, str(OPS / "truthctl.py"), "constraints", "add", "带命令的 gate"],
            cwd=str(REPO),
            env={**env, "KG_CONSTRAINT_ENFORCEABLE": "gate",
                 "KG_CONSTRAINT_CMD": "python3 ops/test-constraint-gates.py"},
            capture_output=True, text=True)
        check("gate 带命令时正常写入", good.returncode == 0, good.stderr[-200:])
        rows = [json.loads(l) for l in
                (repo2 / "ops" / "truth" / "constraints.jsonl").read_text(
                    encoding="utf-8").splitlines() if l.strip()]
        check("写入的记录带上了 cmd", rows and rows[-1].get("cmd") ==
              "python3 ops/test-constraint-gates.py", rows[-1] if rows else None)

    print()
    if failures:
        print(f"结果：{len(failures)} 项未通过")
    else:
        print("selftest ok: 标了 gate 的约束真的进落盘门禁，没命令的如实报缺口。")
    return len(failures)


if __name__ == "__main__":
    sys.exit(main())
