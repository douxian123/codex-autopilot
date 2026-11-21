#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：回收日志不许把「演练」和「没删掉」说成「已回收」。

为什么有这条（用户实测 2026-11-11）：
  用户看到的是「工作做着做着被回收清空」，去查日志又看到一行行「回收闲置 worktree
  xxx（分支本身保留）」，于是无从判断到底有没有东西被真删。查证后发现日志有两种失真：

  1) dry-run 演练写进了生产日志。`ops/disk_reap.py --dry-run` 会猴补一个假的
     `remove_worktree`（返回 True 但不删任何东西），然后调用
     `central_daemon._reap_dead_worktrees()`；后者内部用的是 `log()`，写的是
     daemon.log。于是「会回收」被写成「回收闲置 worktree ...」。
     实测：daemon.log 53853 / 53870 两行写着已回收 ca-fix-fileurl-20261111，
     而该目录至今仍在。
  2) `remove_worktree` 的返回值就是 git 的退出码，它不校验目录是否真的消失。
     git 报成功而目录仍在时，日志照样记「已回收」。

  这两条合起来制造了「被回收」的错觉，也让真实发生的回收无法被信任。
  本门禁锁死三件事：演练不写生产日志、演练措辞是「会回收」、真删后必须复核目录消失。
"""
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
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
    (repo / "README.md").write_text("x")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "remote", "add", "origin", str(repo))
    return repo


def add_worktree(repo, path, branch):
    git(repo, "worktree", "add", "-b", branch, str(path), "main")


def main():
    wg = load("wg_loghon", "workspace_guard.py")
    cd = load("cd_loghon", "central_daemon.py")

    base = Path(tempfile.mkdtemp(prefix="reap-loghon-"))
    runtime = base / "runtime"
    runtime.mkdir()
    cd.RUNTIME = runtime
    cd.REAP_STATE = runtime / "reap_state.json"
    cd.LOG = runtime / "daemon.log"
    os.environ["KG_DAEMON_HOME"] = str(runtime)
    os.environ["KG_WORKSPACE_ROOT"] = str(base)
    os.environ["KG_REAP_WORKTREES"] = "1"
    os.environ["KG_REAP_PRESSURE_FREE_GB"] = "100000"
    cd.REAP_PRESSURE_FREE_GB = 100000.0
    cd.REAP_CREATION_GRACE_H = 0.0
    cd.REAP_MIN_IDLE_H = 0.0
    cd.REAP_IDLE_PRESSURE_H = 0.0
    cd.REAP_MAX_PER_ROUND = 40
    cd.REAP_SCAN_SLICE = 400

    def daemon_log():
        p = runtime / "daemon.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    try:
        repo = make_repo(base)
        cd._save_reap_state({})
        cd.workspace_guard = wg
        cd.workspace_guard.worktree_removable = (
            lambda *a, **k: {"ok": True, "ahead": 0, "backup": "", "reason": "test"})

        # ---- 1. dry-run：不许往生产 daemon.log 写任何「回收」字样 ----
        dry_dir = base / "kg-dry-probe"
        add_worktree(repo, dry_dir, "codex/dry-probe")
        cd._save_reap_state({})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            n_dry = cd._reap_dead_worktrees(dry=True)
        echoed = buf.getvalue()
        written = daemon_log()
        check("dry-run 不往生产日志写「回收闲置 worktree」",
              "回收闲置 worktree" not in written, written[-200:])
        check("dry-run 也不写「已回收」", "已回收" not in written, written[-200:])
        check("dry-run 真的什么都没删（现场还在）", dry_dir.exists())
        check("dry-run 回显里带 [dry-run] 标记", "[dry-run]" in echoed, echoed[:200])
        check("dry-run 措辞是「会回收」而不是「已回收」",
              "会回收" in echoed and "已回收" not in echoed, echoed[:200])
        check("dry-run 计数仍然报出候选数", n_dry == 1, f"n_dry={n_dry}")

        # ---- 2. 真删把目录删掉了：这时才允许写「回收闲置 worktree」 ----
        # 先撤掉上面那棵演练探针树，否则它会被算进这一轮的回收数。
        git(repo, "worktree", "remove", "--force", str(dry_dir))
        check("演练后的探针树被清理掉", not dry_dir.exists())
        real_dir = base / "kg-real-gone"
        add_worktree(repo, real_dir, "codex/real-gone")
        cd._save_reap_state({})
        orig_rm = wg.remove_worktree
        wg.remove_worktree = lambda repo_, wt, **kw: (
            shutil.rmtree(wt, ignore_errors=True) or (True, ""))
        try:
            n_real = cd._reap_dead_worktrees(dry=False)
        finally:
            wg.remove_worktree = orig_rm
        check("真删成功时登记「回收闲置 worktree」",
              "回收闲置 worktree kg-real-gone" in daemon_log(), daemon_log()[-300:])
        check("真删成功时现场确实消失", not real_dir.exists())
        check("真删成功计数为 1", n_real == 1, f"n_real={n_real}")

        # ---- 3. git 报成功但目录仍在：必须记「未生效」，不许记「已回收」 ----
        ghost_dir = base / "kg-ghost-report"
        add_worktree(repo, ghost_dir, "codex/ghost-report")
        cd._save_reap_state({})
        before = len(daemon_log())
        wg.remove_worktree = lambda repo_, wt, **kw: (True, "")   # 报成功，但不删
        try:
            n_ghost = cd._reap_dead_worktrees(dry=False)
        finally:
            wg.remove_worktree = orig_rm
        tail = daemon_log()[before:]
        check("报成功但目录仍在时记为「未生效」", "未生效" in tail, tail[-300:])
        check("报成功但目录仍在时不许写「回收闲置 worktree kg-ghost-report」",
              "回收闲置 worktree kg-ghost-report" not in tail, tail[-300:])
        check("报成功但目录仍在时不计入回收数", n_ghost == 0, f"n_ghost={n_ghost}")
        check("报成功但目录仍在时现场确实还在", ghost_dir.exists())

    finally:
        shutil.rmtree(base, ignore_errors=True)

    if failures:
        print("")
        print("失败项：")
        for item in failures:
            print(f"  - {item}")
        print(f"")
        print(f"reap log honesty unit: FAIL ({len(failures)})")
        return 1
    print("")
    print("reap log honesty unit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
