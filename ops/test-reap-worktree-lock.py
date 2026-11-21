#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression: explicit Git worktree locks veto every destructive path."""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


OPS = Path(__file__).resolve().parent
failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, OPS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
    )


def make_repo(base: Path) -> Path:
    repo = base / "kg-main"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("seed\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def add_worktree(repo: Path, path: Path, branch: str) -> None:
    result = git(repo, "worktree", "add", "-q", "-b", branch, str(path), "main")
    if result.returncode != 0:
        raise RuntimeError(result.stderr)


def main():
    wg = load("wg_worktree_lock", "workspace_guard.py")
    cd = load("cd_worktree_lock", "central_daemon.py")
    base = Path(tempfile.mkdtemp(prefix="reap-worktree-lock-"))
    runtime = base / "runtime"
    runtime.mkdir()
    repo = make_repo(base)
    tree = base / "kg-locked-worktree"
    branch = "codex/locked-worktree"
    lock_reason = "source-provenance-active"

    cd.RUNTIME = runtime
    cd.REAP_STATE = runtime / "reap_state.json"
    cd.LOG = runtime / "daemon.log"
    os.environ["KG_DAEMON_HOME"] = str(runtime)
    os.environ["KG_WORKSPACE_ROOT"] = str(base)
    os.environ["KG_REAP_WORKTREES"] = "1"
    os.environ["KG_REAP_PRESSURE_FREE_GB"] = "100000"
    os.environ["KG_REAP_CREATION_GRACE_H"] = "0"
    cd.REAP_PRESSURE_FREE_GB = 100000.0
    cd.REAP_CREATION_GRACE_H = 0.0
    cd.REAP_MIN_IDLE_H = 0.0
    cd.REAP_IDLE_PRESSURE_H = 0.0
    cd.REAP_MIN_INTERVAL_S = 0.0
    cd.REAP_INTERVAL_PRESSURE_S = 0.0
    cd.REAP_MAX_PER_ROUND = 20
    cd.REAP_SCAN_SLICE = 100

    try:
        add_worktree(repo, tree, branch)

        # The fixture must be eligible before locking; otherwise the lock case
        # could pass merely because the tree failed some unrelated safety gate.
        unlocked = wg.worktree_removable(
            repo, tree, branch, "main", commands="", live_guard_mode="all")
        check("普通干净闲置 worktree 可评估回收", unlocked.get("ok") is True, unlocked)

        cd._save_reap_state({})
        before = []
        original_slice = cd.REAP_SCAN_SLICE
        cd.REAP_SCAN_SLICE = len(list(base.iterdir()))
        cd._reap_dead_worktrees(dry=True, candidates=before)
        check("未锁定的闲置 worktree 会进入 dry-run 候选",
              tree.name in before, before)

        locked = git(repo, "worktree", "lock", str(tree), "--reason", lock_reason)
        check("真实 Git lock 创建成功", locked.returncode == 0, locked.stderr)

        # A symlink and the platform's real temp path must resolve to the same
        # record. On macOS this also covers /var -> /private/var.
        repo_link = base / "repo-link"
        tree_link = base / "worktree-link"
        repo_link.symlink_to(repo, target_is_directory=True)
        tree_link.symlink_to(tree, target_is_directory=True)
        reason = wg.worktree_lock_reason(repo_link, tree_link)
        check("锁定原因通过 symlink 归一化后仍能识别",
              lock_reason in reason, reason)

        blocked = wg.worktree_removable(
            repo, tree, branch, "main", commands="", live_guard_mode="all")
        check("锁定后评估层拒绝回收",
              blocked.get("ok") is False and lock_reason in blocked.get("reason", ""),
              blocked)

        sentinel = tree / "frontend" / "node_modules" / "sentinel.txt"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("keep\n")
        removed, detail = wg.remove_worktree(repo, tree, force_regenerable=True)
        check("删除层在清理目录前拒绝锁定 worktree",
              removed is False and lock_reason in detail, detail)
        check("锁定 worktree 及其中目录仍存在",
              tree.exists() and sentinel.exists(), f"tree={tree.exists()}")

        cd._save_reap_state({})
        locked_candidates = []
        cd._reap_dead_worktrees(dry=True, candidates=locked_candidates)
        check("dry-run 扫描不再把锁定 worktree 列为候选",
              tree.name not in locked_candidates, locked_candidates)

        unlocked_again = git(repo, "worktree", "unlock", str(tree))
        check("Git unlock 成功", unlocked_again.returncode == 0, unlocked_again.stderr)
        after_unlock = wg.worktree_removable(
            repo, tree, branch, "main", commands="", live_guard_mode="all")
        check("解锁后同一棵树恢复可评估",
              after_unlock.get("ok") is True, after_unlock)

        removed_after_unlock, detail_after_unlock = wg.remove_worktree(repo, tree)
        check("解锁后可以真实回收",
              removed_after_unlock is True and not tree.exists(), detail_after_unlock)
    finally:
        if "original_slice" in locals():
            cd.REAP_SCAN_SLICE = original_slice
        shutil.rmtree(base, ignore_errors=True)

    if failures:
        print("\n失败项：")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nreap worktree lock unit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
