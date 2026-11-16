#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：detached HEAD 的 worktree 里，独占提交绝不能被当成「已合并且干净」。

为什么有这条门禁（2026-11-17 实测）：

  回收判据 `workspace_guard.worktree_removable` 里原本只有一行

      ahead = branch_ahead(repo, branch, main) if branch else 0

  调用方（`disk_reap._remove_one_kg_tmp`）读分支名时用的是
  `git rev-parse --abbrev-ref HEAD`，对 detached HEAD 得到字符串 `"HEAD"`，
  于是它把 branch 置成空串再传进来。branch 为空 -> `ahead` 直接读成 0，
  下面「有独占提交就拒绝回收（unique-commits）」这道闸根本不触发。

  最小复现（同一棵 worktree、同一个提交）：
    * 传 `branch="HEAD"` -> ok=True  reason=clean          ahead=0   ← 会被回收
    * 传真实 sha          -> ok=False reason=unique-commits ahead=1   ← 被拦住

  后果不是理论：`git rev-list --all` 把 worktree 的 HEAD 也算作可达，所以
  只要树还在、提交就还看得见；一旦回收器删掉这棵树，这些提交就变成任何
  ref 都够不到的对象，`git gc` 两周后静默清掉。本机实测有 18 棵 detached
  worktree 带着独占提交（最多 ahead=8171），正是这条路径的暴露面。

  本门禁钉住两个方向：
    1. 带独占提交的 detached 树 -> 判据必须拒绝（unique-commits），且 ahead>=1；
    2. 无独占提交的 detached 树 -> 仍要允许回收（不把回收功能一刀切废掉）。
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


def git(cwd, *args, check=True):
    p = subprocess.run(["git", "-C", str(cwd), *args],
                       capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError((args, p.stderr))
    return p.stdout.strip()


def make_repo(base):
    repo = base / "kg-main"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def main():
    wg = load("wg_detached_guard", "workspace_guard.py")
    base = Path(tempfile.mkdtemp(prefix="reap-detached-head-"))
    try:
        repo = make_repo(base)

        # ---- 一号树：detached HEAD 且带独占提交 ----
        wt = base / "wt-unique"
        git(repo, "worktree", "add", "-q", "-b", "feat", str(wt))
        (wt / "b.txt").write_text("unique work\n")
        git(wt, "add", "b.txt")
        git(wt, "commit", "-q", "-m", "unique commit")
        head = git(wt, "rev-parse", "HEAD")
        git(wt, "checkout", "-q", "--detach", "HEAD")
        git(repo, "branch", "-D", "feat")   # 现在只有该 worktree 的 HEAD 指向它

        abbrev = git(wt, "rev-parse", "--abbrev-ref", "HEAD")
        check("前置：该树确实是 detached HEAD", abbrev == "HEAD", abbrev)
        real_ahead = int(git(repo, "rev-list", "--count", f"main..{head}"))
        check("前置：该树确实有独占提交", real_ahead >= 1, real_ahead)

        # 调用方的真实传法：detached -> branch 被置成 "HEAD"
        assessed = wg.worktree_removable(repo, wt, abbrev, "main")
        check("带独占提交的 detached 树被拒绝回收",
              assessed.get("ok") is False, assessed)
        check("拒绝原因是 unique-commits（而不是 dirty/missing 等旁路）",
              assessed.get("reason") == "unique-commits", assessed)
        check("ahead 如实反映独占提交数（不再读成 0）",
              assessed.get("ahead") == real_ahead, assessed)
        # 直接传 sha 也必须拒绝（防止别的调用方走这条路漏掉）
        assessed_sha = wg.worktree_removable(repo, wt, head, "main")
        check("传 HEAD sha 时同样被拒绝",
              assessed_sha.get("ok") is False
              and assessed_sha.get("reason") == "unique-commits", assessed_sha)

        # ---- 二号树：detached HEAD 但无独占提交（不误伤回收） ----
        wt2 = base / "wt-clean"
        main_tip = git(repo, "rev-parse", "main")
        git(repo, "worktree", "add", "-q", "--detach", str(wt2), main_tip)
        check("前置：二号树停在 main 上、无独占提交",
              git(repo, "rev-list", "--count", f"main..{git(wt2, 'rev-parse', 'HEAD')}") == "0")
        clean = wg.worktree_removable(repo, wt2, "HEAD", "main",
                                      restore_regenerable=False)
        check("无独占提交的 detached 树仍可回收（回收功能没被一刀切废掉）",
              clean.get("ok") is True, clean)
        check("二号树的 ahead 仍为 0", clean.get("ahead") == 0, clean)

        # ---- 端到端：让回收器自己扫，必须跳过一号树 ----
        os.environ["KG_WORKSPACE_ROOT"] = str(base)
        sys.path.insert(0, str(OPS))
        try:
            import central_daemon as cd
        except Exception as exc:
            check("可导入 central_daemon（端到端断言前置）", False,
                  f"{type(exc).__name__}: {exc}")
            cd = None
        if cd is not None:
            picked = []
            try:
                cd._workspace_root = lambda: base
                cd._disk_free_gb = lambda: 0.0          # 强制走紧张回收节奏
                cd._live_exec_slugs = lambda *a, **k: frozenset()
                cd.REAP_INTERVAL_PRESSURE_S = 0
                cd.REAP_MIN_INTERVAL_S = 0
                cd._reap_dead_worktrees(dry=True, candidates=picked)
            except Exception as exc:
                check("端到端扫描可执行", False, f"{type(exc).__name__}: {exc}")
            check("端到端：带独占提交的 detached 树不在回收候选里",
                  wt not in picked, picked)
    finally:
        os.environ.pop("KG_WORKSPACE_ROOT", None)
        shutil.rmtree(base, ignore_errors=True)

    if failures:
        print("\n失败项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\nreap detached-head guard unit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
