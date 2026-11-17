#!/usr/bin/env python3
# -*- coding: utf-8 -*-
<<<<<<< HEAD
"""回归：`kgctl reap --go` 删分支前，必须先把分支 tip 钉成 preserve ref。

为什么有这条（2026-11-16 实测）：
  自动挡插件里那份 `kgctl reap` 删的是**分支指针**，删之前不做任何保全。守护
  （launchd `com.codex-autopilot.daemon`）一旦拉起，磁盘压力下第一拍就会真的执行
  `reap --go`——实测当时磁盘 92.8%（阈值 85%），只读预演列出 34 条待删分支。
  指针一删、远端又没有影子 ref，tip 就成了不可达对象，`git gc` 随后连整条祖先链
  一起清掉。本机 knowledge-graph 仓历史上正是这么攒下一万多条游离提交。

  顺带澄清一个容易混的点：`knowledge-graph` 仓自己那份 `ops/kgctl.py` 有这道保护，
  但守护实际调用的是**插件里**的那一份（`current_ops()/kgctl.py`），两份同名不同源。
  所以「仓库里有保护」不等于「跑着的这条路径有保护」。

本门禁锁死四件事：
  1. 有保护：删分支后 preserve ref 存在，且指向被删前的同一个 sha；
  2. 真防回收：`git gc --prune=now` 之后那个 commit 对象仍在（这才是保护的目的）；
  3. 可还原：用 preserve ref 能把分支整条还原回原 sha；
  4. 钉不上就不删：钉 ref 失败时宁可留着分支，也不裸删。
=======
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
>>>>>>> codex/reap-branch-preserve-20261111
"""
import contextlib
import importlib.util
import io
<<<<<<< HEAD
=======
import os
>>>>>>> codex/reap-branch-preserve-20261111
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
<<<<<<< HEAD
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "remote", "add", "origin", str(repo))
    return repo


def preserve_refs(repo):
    out = git(repo, "for-each-ref", "--format=%(refname) %(objectname)",
              "refs/preserve/local").stdout
    return [line for line in out.splitlines() if line.strip()]


def main():
    kg = load("kg_reap_preserve", "kgctl.py")
    base = Path(tempfile.mkdtemp(prefix="reap-branch-preserve-"))
    # 刚建好的分支不因「创建宽限」被跳过——本条门禁测的是删除路径，不是判活。
    import os
    os.environ["KG_REAP_CREATION_GRACE_H"] = "0"
    try:
        repo = make_repo(base)
        kg.REPO = repo
        kg.MAIN = "main"
        kg.BRANCH_PREFIX = "codex/"

        # ---- 1. 有保护：删分支前先钉 preserve ref，且指向同一个 sha ----
        git(repo, "branch", "codex/pin-only-a", "main")
        sha_a = git(repo, "rev-parse", "codex/pin-only-a").stdout.strip()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kg.cmd_reap(["--go"])
        echoed = out.getvalue()

        check("已合入分支被删掉（ahead=0 的正常回收路径）",
              git(repo, "rev-parse", "--verify", "refs/heads/codex/pin-only-a")
              .returncode != 0, echoed[-300:])
        refs = preserve_refs(repo)
        check("删除前钉出了 preserve ref", len(refs) >= 1, refs)
        check("preserve ref 指向被删前的同一个 sha",
              any(sha_a in line for line in refs), f"sha_a={sha_a} refs={refs}")
        check("回显里说明了钉 ref 这件事",
              "preserve/local" in echoed, echoed[-300:])

        # ---- 2. 真防回收：gc --prune=now 之后对象仍在 ----
        git(repo, "reflog", "expire", "--expire=now", "--all")
        git(repo, "gc", "--prune=now", "--quiet")
        check("gc --prune=now 之后 commit 对象仍在（preserve ref 让它 ref 可达）",
              git(repo, "cat-file", "-t", sha_a).stdout.strip() == "commit",
              git(repo, "cat-file", "-t", sha_a).stderr)

        # ---- 3. 可还原：用 preserve ref 把分支整条还原 ----
        ref_a = next(line.split()[0] for line in refs if sha_a in line)
        git(repo, "branch", "codex/restored-a", ref_a)
        check("用 preserve ref 能还原出原 sha",
              git(repo, "rev-parse", "codex/restored-a").stdout.strip() == sha_a)

        # ---- 4. 钉不上就不删：钉 ref 失败时分支必须留下 ----
        git(repo, "branch", "codex/pin-only-c", "main")
        orig_pin = kg._pin_branch_ref
        kg._pin_branch_ref = lambda branch, stamp: ""
        out_c = io.StringIO()
        try:
            with contextlib.redirect_stdout(out_c):
                kg.cmd_reap(["--go"])
        finally:
            kg._pin_branch_ref = orig_pin
        check("钉 ref 失败时分支仍然存在（宁可留着，不裸删）",
              git(repo, "rev-parse", "--verify", "refs/heads/codex/pin-only-c")
              .returncode == 0, out_c.getvalue()[-300:])
        check("钉 ref 失败时回显里写了「跳过分支删除」",
              "跳过分支删除" in out_c.getvalue(), out_c.getvalue()[-300:])
=======
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

>>>>>>> codex/reap-branch-preserve-20261111
    finally:
        shutil.rmtree(base, ignore_errors=True)

    if failures:
<<<<<<< HEAD
        print("\n失败项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\nreap branch preserve unit: PASS")
=======
        print("")
        print("失败项：")
        for item in failures:
            print(f"  - {item}")
        print("")
        print(f"reap branch preserve unit: FAIL ({len(failures)})")
        return 1
    print("")
    print("reap branch preserve unit: PASS")
>>>>>>> codex/reap-branch-preserve-20261111
    return 0


if __name__ == "__main__":
    sys.exit(main())
