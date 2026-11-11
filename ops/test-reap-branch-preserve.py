#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：`reap --go` 删分支指针之前，必须先把 tip 钉成本地 preserve ref。

为什么有这条（2026-11-11 实测）：
  用户报「工作被清掉了」。查下来 21 条本地 `codex/*` 分支 ref 被剪掉，只剩 3 条；
  它们的成果「恰好」没丢，是因为其中一部分早有 `origin/preserve/*` 影子 ref。
  靠碰巧有备份不是机制 —— 当天 dock-drawer-overlap-20261110 与
  「供应复活与主链复验」两条既无本地 ref 也无影子，只能靠 untracked tgz 拼回来。

  修法：`git branch -D` 之前先 `git update-ref refs/preserve/local/<stamp>/<branch>`。
  本门禁锁死三件事：
    1) 删掉的分支，tip 一定能在 refs/preserve/local/<stamp>/ 下找到，且 sha 相同；
    2) 有进程正在用的 worktree，reap 不许碰（既不删目录也不删它的分支）；
    3) 钉 ref 失败时必须**放弃删分支**（宁可留着，不能裸删）。
"""
import contextlib
import importlib.util
import io
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
    (repo / "README.md").write_text("x\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "remote", "add", "origin", str(repo))
    (repo / "ops" / "tasks").mkdir(parents=True)
    (repo / "ops" / "truth").mkdir(parents=True)
    return repo


def main():
    base = Path(tempfile.mkdtemp(prefix="reap-preserve-"))
    repo = make_repo(base)
    os.environ["KG_REPO"] = str(repo)
    os.environ.setdefault("KG_WORKSPACE_ROOT", str(base))

    kg = load("kg_preserve", "kgctl.py")
    wg = load("wg_preserve", "workspace_guard.py")
    kg.workspace_guard = wg
    kg.REPO = repo
    kg.CFG = dict(kg.CFG)
    kg.MAIN = "main"
    kg.BRANCH_PREFIX = "codex/"

    try:
        # ---- 1. 干净、已合入（ahead=0）的分支：会被回收，但 tip 必须被钉住 ----
        clean_wt = base / "kg-clean"
        git(repo, "worktree", "add", "-b", "codex/clean-merged", str(clean_wt), "main")
        tip = git(repo, "rev-parse", "codex/clean-merged").stdout.strip()

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            kg.cmd_reap(["--go"])
        out = buf.getvalue()

        gone = git(repo, "rev-parse", "--verify", "codex/clean-merged")
        check("已合入分支的分支指针被回收", gone.returncode != 0, gone.stdout)

        # stamp 从输出里取，别猜
        import re as _re
        m = _re.search(r"refs/preserve/local/(\S+?)/", out)
        stamp = m.group(1) if m else ""
        check("输出里报告了 preserve 路径", bool(stamp), out[-300:])

        pinned = ""
        if stamp:
            refs = git(repo, "for-each-ref",
                       f"refs/preserve/local/{stamp}/",
                       "--format=%(refname) %(objectname)").stdout.strip()
            for line in refs.splitlines():
                name, sha = line.rsplit(" ", 1)
                if name.endswith("codex/clean-merged"):
                    pinned = sha
        check("被删分支的 tip 已钉在本地 preserve ref 下", pinned == tip,
              f"pinned={pinned!r} tip={tip!r}")

        # 还原路径必须真的能用
        restored = git(repo, "rev-parse",
                       f"refs/preserve/local/{stamp}/codex/clean-merged")
        check("preserve ref 可直接 rev-parse 还原",
              restored.stdout.strip() == tip, restored.stdout.strip())

        # ---- 2. 有进程在用的 worktree：既不删目录，也不删它的分支 ----
        busy_wt = base / "kg-busy"
        git(repo, "worktree", "add", "-b", "codex/busy", str(busy_wt), "main")

        real_pc = wg.process_commands
        real_pr = wg.process_reference
        wg.process_commands = lambda: f"/bin/zsh -lc cd {busy_wt}\n"
        try:
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                kg.cmd_reap(["--go"])
            out2 = buf2.getvalue()
        finally:
            wg.process_commands = real_pc
            wg.process_reference = real_pr

        check("有进程在用的 worktree 目录还在", busy_wt.exists())
        check("有进程在用的 worktree 未被删分支",
              git(repo, "rev-parse", "--verify", "codex/busy").returncode == 0)
        check("有进程在用时输出里写明跳过", "跳过" in out2, out2[-300:])

        # ---- 3. 钉 ref 失败：必须放弃删分支（宁可留着） ----
        fail_wt = base / "kg-failpin"
        git(repo, "worktree", "add", "-b", "codex/fail-pin", str(fail_wt), "main")
        real_run = kg.proc_run

        def deny_update_ref(cmd, **kw):
            # 真实命令是 ["git", "-C", REPO, "update-ref", ref, sha]，update-ref 在 cmd[3]
            if "update-ref" in cmd:
                return subprocess.CompletedProcess(cmd, 1, "", "denied")
            return real_run(cmd, **kw)

        kg.proc_run = deny_update_ref
        try:
            buf3 = io.StringIO()
            with contextlib.redirect_stdout(buf3):
                kg.cmd_reap(["--go"])
            out3 = buf3.getvalue()
        finally:
            kg.proc_run = real_run

        check("钉 ref 失败时不删分支",
              git(repo, "rev-parse", "--verify", "codex/fail-pin").returncode == 0,
              out3[-300:])

    finally:
        shutil.rmtree(base, ignore_errors=True)

    if failures:
        print("")
        print("失败项：")
        for item in failures:
            print(f"  - {item}")
        print("")
        print(f"reap branch preserve unit: FAIL ({len(failures)})")
        return 1
    print("")
    print("reap branch preserve unit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
