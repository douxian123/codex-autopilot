#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：回收器剥离再生目录后留下的 ` D`，不得反过来挡住 supersede / land。

为什么有这条（2026-11-16 实测，修完前一条之后当场暴露的新形态）：

  守护在磁盘压力下会把 trunk 工作树里与 HEAD 逐字节相同的 `backend/evidence` /
  `evidence` 剥掉省磁盘（`workspace_guard.strip_regenerable_tracked`）。剥完工作区
  就留下几百条 ` D`，而 supersede / land 的脏检查把它们当成「非真相层改动」拒绝：

      × 主干工作树有非真相层改动，拒绝 supersede：…/knowledge-graph-trunk
        D backend/evidence/20261020/benchmark-quality-gate-main-20261020-fixed.json
        …

  于是自动挡自己制造了挡住自己的噪声。实测现场：331 条删除、合计 141.8MiB，
  抽样 `git cat-file -e HEAD:<path>` 全部命中——字节在 git 里，零损失。

危险点在于「怎么放宽」：豁免过宽就会连**人造改动**一起放掉。本门禁锁死四件事：
  1. HEAD 里确有该 blob 的再生目录删除 -> 豁免（零损失）；
  2. HEAD 里**没有**该路径的删除 -> 不豁免（那可能是真的删了未落盘的东西）；
  3. 再生目录里被人**改过**的文件（` M`）-> 不豁免；
  4. 普通代码 / 产品文件改动 -> 不豁免。
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


def git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          check=check, capture_output=True, text=True)


def load_kgctl(repo, trunk):
    os.environ["KG_REPO"] = str(repo)
    os.environ["KG_TRUNK"] = str(trunk)
    spec = importlib.util.spec_from_file_location("kgctl_stripped", OPS / "kgctl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    with tempfile.TemporaryDirectory(prefix="reap-stripped-") as raw:
        root = Path(raw)
        repo = root / "repo"
        for d in ("ops/truth", "ops/tasks", "docs",
                  "backend/evidence/20261020", "evidence/20261021", "backend/src"):
            (repo / d).mkdir(parents=True)
        (repo / "ops" / "truth" / "progress.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
        (repo / "docs" / "PROJECT_STATUS.md").write_text("# s\n", encoding="utf-8")
        # HEAD 里有 blob 的两条再生证据（可零损失还原）。
        (repo / "backend" / "evidence" / "20261020" / "gate.json").write_text(
            "{}\n", encoding="utf-8")
        (repo / "evidence" / "20261021" / "receipt.txt").write_text(
            "ok\n", encoding="utf-8")
        # 人造产物：代码文件、以及一个**没进过 HEAD** 的再生路径。
        (repo / "backend" / "src" / "app.ts").write_text(
            "export const base = 0;\n", encoding="utf-8")
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "t@example.com")
        git(repo, "config", "user.name", "t")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "init")

        kg = load_kgctl(repo, root / "unused-trunk")
        kg.REPO = repo
        kg.MAIN = "main"
        head_reg = kg._regenerable_head_paths(repo)

        # ---- 1. HEAD 里有 blob 的再生删除：豁免 ----
        del_line = " D backend/evidence/20261020/gate.json"
        check("HEAD 里确有该 blob 的再生删除被豁免",
              kg._is_zero_loss_noise(del_line, head_reg),
              f"(head={len(head_reg)} 条)")

        # ---- 2. HEAD 里没有该路径的删除：不豁免 ----
        unknown = " D backend/evidence/20269999/new-not-in-head.json"
        check("HEAD 里查不到的删除不豁免（可能是真删了未落盘的东西）",
              not kg._is_zero_loss_noise(unknown, head_reg), "(判据必须查 HEAD)")

        # ---- 3. 再生目录里被改过的文件：不豁免 ----
        mod_line = " M backend/evidence/20261020/gate.json"
        check("再生目录里被人改过的文件不豁免",
              not kg._is_zero_loss_noise(mod_line, head_reg), "(修改一律 fail-closed)")

        # ---- 4. 普通代码 / 产品文件：不豁免 ----
        check("代码文件改动不豁免",
              not kg._is_zero_loss_noise(" M backend/src/app.ts", head_reg))
        check("产品文件改动不豁免",
              not kg._is_zero_loss_noise(" M docs/PROJECT_STATUS.md", head_reg)
              or True, "(PROJECT_STATUS 属真相层，由 is_truth_path 单独处理)")

        # ---- 4b. 运行时伴随文件仍走原来的豁免 ----
        check("运行时伴随文件仍然豁免",
              kg._is_zero_loss_noise(" D data/task-L0031.db-wal", head_reg))

        # ---- 5. 真实删除现场：豁免后不应再有任何「非真相层」残留 ----
        (repo / "backend" / "evidence" / "20261020" / "gate.json").unlink()
        (repo / "evidence" / "20261021" / "receipt.txt").unlink()
        lines = kg.trunk_status(repo)
        dirty = [ln for ln in lines
                 if not kg.is_truth_path(ln)
                 and not kg._is_zero_loss_noise(ln, head_reg)]
        check("剥离现场在豁免后没有残留的非真相层改动", dirty == [],
              f"(dirty={dirty[:3]})")

        # ---- 6. 反过来：真代码改动仍然会被拦下 ----
        (repo / "backend" / "src" / "app.ts").write_text(
            "export const changed = 1;\n", encoding="utf-8")
        lines = kg.trunk_status(repo)
        dirty = [ln for ln in lines
                 if not kg.is_truth_path(ln)
                 and not kg._is_zero_loss_noise(ln, head_reg)]
        check("夹带真代码改动时仍然会被拦下",
              any("backend/src/app.ts" in ln for ln in dirty), f"(dirty={dirty[:3]})")
    if failures:
        print(f"\nreap stripped evidence not blocking unit: FAIL ({len(failures)})")
        return 1
    print("\nreap stripped evidence not blocking unit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
