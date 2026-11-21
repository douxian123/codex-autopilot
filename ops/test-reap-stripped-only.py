#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：剥离态（脏项全在可再生目录内的删除）必须可被手工 reap 回收。

历史缺陷（2026-11-13 实测）：
  六十分钟内磁盘从 24G 掉到 17G，用户反复「又满了」。`disk_reap` 的剥离通道
  会删掉每棵 worktree 里与 HEAD 逐字节相同的 `backend/evidence` / `evidence`
  副本（各 ~235MB，190 棵树合计十几 GB 纯重复）。剥离之后 `git status` 显示
  ` D evidence/...`，而手工 `kgctl reap --go` 只认 `status` 完全为空 —— 于是
  剥离过的树永远显示脏、永远回收不掉，分支指针和整棵目录常年压在盘上。

  判据本身不能放松：只有「每一条脏项都是可再生目录内、且都是删除」才算零工作。
  一个未跟踪文件、一处真实改动、一条非删除，都必须继续按脏保留。
"""
import importlib.util
import os
import shutil
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


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, OPS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True)


def make_repo(base):
    repo = base / "kg-main"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "backend" / "evidence" / "20261020").mkdir(parents=True)
    (repo / "backend" / "evidence" / "20261020" / "gate.json").write_text("{}\n")
    (repo / "evidence" / "20261020").mkdir(parents=True)
    (repo / "evidence" / "20261020" / "note.md").write_text("note\n")
    (repo / "README.md").write_text("x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "remote", "add", "origin", str(repo))
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def add_worktree(repo, path, branch):
    git(repo, "worktree", "add", "-b", branch, str(path), "main")


def main():
    wg = load("wg_stripped", "workspace_guard.py")
    kg = load("kg_stripped", "kgctl.py")
    base = Path(tempfile.mkdtemp(prefix="reap-stripped-"))
    try:
        repo = make_repo(base)
        kg.REPO = repo

        # ---- 1. 剥离态：全部脏项都是可再生目录内的删除 -> 必须判为可回收 ----
        stripped = base / "kg-stripped-only"
        add_worktree(repo, stripped, "codex/stripped-only")
        shutil.rmtree(stripped / "backend" / "evidence")
        shutil.rmtree(stripped / "evidence")
        lines = git(stripped, "status", "--porcelain").stdout.splitlines()
        check("构造出剥离态脏项", len(lines) == 2, lines)
        check("剥离态被判为可回收",
              kg._reap_stripped_only(stripped) is True)
        check("与 guard 同一判据", wg.only_regenerable_deletions(stripped) is True)

        # ---- 2. 混入一个未跟踪的人间文件 -> 必须继续按脏保留 ----
        dirty = base / "kg-stripped-untracked"
        add_worktree(repo, dirty, "codex/stripped-untracked")
        shutil.rmtree(dirty / "backend" / "evidence")
        shutil.rmtree(dirty / "evidence")
        (dirty / "my-work.md").write_text("human\n")
        check("有未跟踪人间产物时不回收",
              kg._reap_stripped_only(dirty) is False)

        # ---- 3. 混入一处真实改动 -> 必须继续按脏保留 ----
        edited = base / "kg-stripped-edited"
        add_worktree(repo, edited, "codex/stripped-edited")
        shutil.rmtree(edited / "backend" / "evidence")
        (edited / "README.md").write_text("human edit\n")
        check("有真实改动时不回收",
              kg._reap_stripped_only(edited) is False)

        # ---- 4. 完全干净的树照旧可回收（不依赖剥离态判据）----
        clean = base / "kg-stripped-clean"
        add_worktree(repo, clean, "codex/stripped-clean")
        check("干净树不命中剥离态（走原有 clean 分支）",
              kg._reap_stripped_only(clean) is False)

        # ---- 5. 保护名仍然不可回收 ----
        live = base / "kg-live-3000"
        add_worktree(repo, live, "codex/live-3000")
        shutil.rmtree(live / "backend" / "evidence")
        shutil.rmtree(live / "evidence")
        check("剥离态保护名仍被识别", kg._reap_protected_name("kg-live-3000") is True)
        check("普通名不误判保护", kg._reap_protected_name("kg-normal-tree") is False)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    if failures:
        print("\n失败项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\nreap stripped-only unit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
