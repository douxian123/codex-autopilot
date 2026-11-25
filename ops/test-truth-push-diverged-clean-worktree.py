#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：状态根 HEAD 分叉且共享检出脏时，真相推送必须在干净 worktree 合并。

2026-11-25 现场：`tick` 的 [7/8] 先把真相提交到旧分支 HEAD，但 `origin/main` 已经前进；
旧实现随后试图在脏共享检出里 `git merge origin/main`，被用户文件挡住后静默返回。
结果台账显示「真相层已提交」，远端却永远收不到。

本夹具用本地 bare remote 锁死四件事：
  1. 分叉的纯真相提交确实被推送到 origin/main；
  2. 远端与本地两侧的 JSONL 记录都保留；
  3. 共享检出里的非真相脏文件一个字节不动；
  4. 临时 worktree 用完后清理干净。
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


def git(cwd, *args, check_rc=True):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          check=check_rc, capture_output=True, text=True)


def load_kgctl(repo):
    os.environ["KG_REPO"] = str(repo)
    os.environ["KG_TRUNK"] = str(repo / "unused-trunk")
    os.environ["KG_MODEL_PROBE"] = "off"
    spec = importlib.util.spec_from_file_location(
        "kgctl_truth_push_diverged", OPS / "kgctl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.REPO = repo
    mod.MAIN = "main"
    return mod


def seed(root):
    remote = root / "remote.git"
    repo = root / "repo"
    main_wt = root / "main-wt"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    for d in ("ops/truth", "ops/tasks", "ops/context", "docs"):
        (repo / d).mkdir(parents=True)
    progress = repo / "ops" / "truth" / "progress.jsonl"
    progress.write_text('{"id":"P-BASE"}\n', encoding="utf-8")
    (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
    (repo / "docs" / "PROJECT_STATUS.md").write_text("# status\n", encoding="utf-8")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "push", "-q", "-u", "origin", "main")

    # 状态根 HEAD 停在旧分支。
    git(repo, "checkout", "-q", "-b", "codex/old")

    # origin/main 在另一个 worktree 上前进，制造真正的分叉。
    git(repo, "worktree", "add", "-q", str(main_wt), "main")
    remote_progress = main_wt / "ops" / "truth" / "progress.jsonl"
    remote_progress.write_text(remote_progress.read_text(encoding="utf-8")
                               + '{"id":"P-REMOTE"}\n', encoding="utf-8")
    git(main_wt, "add", "ops/truth/progress.jsonl")
    git(main_wt, "commit", "-q", "-m", "remote truth advance")
    git(main_wt, "push", "-q", "origin", "main")
    git(repo, "worktree", "remove", "--force", str(main_wt))

    # 旧分支本地也有一条真相提交。
    progress.write_text(progress.read_text(encoding="utf-8")
                        + '{"id":"P-LOCAL"}\n', encoding="utf-8")
    git(repo, "add", "ops/truth/progress.jsonl")
    git(repo, "commit", "-q", "-m", "local truth advance")

    # 现场那种非真相脏文件：原地 merge 会被它挡住，干净 worktree 必须绕开。
    (repo / "app.txt").write_text("dirty user edit\n", encoding="utf-8")
    return repo, remote


def main():
    with tempfile.TemporaryDirectory(prefix="truth-push-diverged-") as raw:
        root = Path(raw)
        repo, remote = seed(root)
        kg = load_kgctl(repo)

        remote_before = git(remote, "rev-parse", "main").stdout.strip()
        local_before = git(repo, "rev-parse", "HEAD").stdout.strip()
        check("夹具前提：状态根已分叉",
              git(repo, "merge-base", "--is-ancestor", "origin/main", "HEAD",
                  check_rc=False).returncode != 0)

        kg._push_truth_state_locked()

        remote_after = git(remote, "rev-parse", "main").stdout.strip()
        check("分叉真相提交已推送到 origin/main", remote_before != remote_after,
              f"({remote_before[:8]} -> {remote_after[:8]})")
        remote_text = git(remote, "show", "main:ops/truth/progress.jsonl").stdout
        check("远端 P-REMOTE 记录保留", "P-REMOTE" in remote_text)
        check("本地 P-LOCAL 记录进入远端", "P-LOCAL" in remote_text)
        check("共享检出 HEAD 未被偷偷改写",
              git(repo, "rev-parse", "HEAD").stdout.strip() == local_before)
        check("非真相脏文件保持原样",
              (repo / "app.txt").read_text(encoding="utf-8") == "dirty user edit\n")
        check("本地 main 引用指向已推送提交",
              git(repo, "rev-parse", "refs/heads/main").stdout.strip() == remote_after)
        worktrees = git(repo, "worktree", "list", "--porcelain").stdout
        check("临时 worktree 已清理",
              "autopilot-truth-push-" not in worktrees, f"(worktrees={worktrees[:200]!r})")

    if failures:
        print(f"\ntruth push diverged clean worktree: FAIL ({len(failures)})")
        return 1
    print("\ntruth push diverged clean worktree: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
