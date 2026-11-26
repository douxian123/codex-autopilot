#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：落盘主干的 checkout 预演在旧版 git 上也要安全。

Apple Git 2.50.1 的 `git checkout --dry-run` 返回 unknown option。旧实现把
预演失败当成「主干不可安全切换」，于是每个候选都被丢弃，最后去 checkout
一个还没创建的目录，报出误导性的「No such file or directory」。

这条门禁用真实 worktree 夹具锁三件事：干净主干可切、会覆盖已跟踪改动时拒绝、
会覆盖未跟踪文件时拒绝；同时禁止实现退回 checkout --dry-run。
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


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True)


def seed_repo(root: Path, name: str) -> Path:
    repo = root / name
    (repo / "ops" / "tasks").mkdir(parents=True)
    (repo / "ops" / "truth").mkdir(parents=True)
    (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "truth" / "progress.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "central.config.json").write_text("{}\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("main\n", encoding="utf-8")
    (repo / "only_main.txt").write_text("main only\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "checkout", "-q", "-b", "codex/work")
    (repo / "a.txt").write_text("work\n", encoding="utf-8")
    (repo / "only_main.txt").unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "work")
    git(repo, "checkout", "-q", "main")
    return repo


def load_kgctl(repo: Path):
    os.environ["KG_REPO"] = str(repo)
    os.environ["KG_MODEL_PROBE"] = "off"
    spec = importlib.util.spec_from_file_location(
        "kgctl_trunk_checkout_probe", OPS / "kgctl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.REPO = repo
    return mod


def main():
    with tempfile.TemporaryDirectory(prefix="autopilot-trunk-probe-") as tmp:
        root = Path(tmp)
        state = seed_repo(root, "state")
        wt = root / "probe-wt"
        git(state, "worktree", "add", "-q", "--detach", str(wt), "codex/work")
        kg = load_kgctl(state)
        kg.MAIN = "main"

        check("干净 worktree 可以切到 main",
              kg._trunk_can_checkout_main(wt) is True)

        (wt / "only_main.txt").write_text("untracked\n", encoding="utf-8")
        check("未跟踪文件会被 main 覆盖时拒绝",
              kg._trunk_can_checkout_main(wt) is False)
        (wt / "only_main.txt").unlink()

        (wt / "a.txt").write_text("dirty\n", encoding="utf-8")
        check("已跟踪改动会被 main 覆盖时拒绝",
              kg._trunk_can_checkout_main(wt) is False)
        git(wt, "checkout", "--", "a.txt")

    src = (OPS / "kgctl.py").read_text(encoding="utf-8")
    check("不再使用不支持的 checkout --dry-run",
          '"checkout", "--dry-run' not in src)
    check("预演使用 read-tree -n -m -u",
          '"read-tree", "-n", "-m", "-u"' in src)

    if failures:
        print(f"\nland trunk checkout probe: FAIL ({len(failures)})")
        return 1
    print("\nland trunk checkout probe: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
