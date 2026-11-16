#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：落盘主干必须属于本仓库，不能是另一个 clone。

为什么有这条（2026-11-17 本轮 land 实测）：

  仓库里 `ops/central.config.json` 的 `trunk_dir` 被写死成

      /Users/danta/Documents/Codex/codex-autopilot-native-autopilot-trunk

  那是**另一个 clone** 的主干（`git remote` 相同，但 `git common dir` 不同）。
  于是默认的 `kgctl land` 会跑到别人的检出里做 `checkout main` / `merge` /
  `reset HEAD`。既是跨 clone 的破坏性修改，也必然失败——实测被那边的未跟踪
  runtime 产物挡住：

      × 主干工作树切不到 main：
        error: The following untracked working tree files would be overwritten by checkout:
          ops/tasks/resolve.json
          ops/truth/closure_rate.json

  这条路径只在仓库里配置一份，所有 worktree 都读它，所以它不修，任何人的
  land 都得靠 `KG_TRUNK=` 覆盖才落得了盘。

本门禁锁死两件事：
  1. 判据用仓库身份（git common dir），不是路径或目录名；
  2. 判为 foreign 时 `_trunk_worktree()` 退回本仓库的 `<项目名>-trunk`，
     而不是继续在别人的检出里动手。
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
    """建一个最小可识别仓库：有 commit，git common dir 独立于其它仓库。"""
    repo = root / name
    # 状态根判据要求 ops/tasks + ops/truth 同时在（kgctl 导入时会校验），
    # 缺一个就会被判成「不是编排状态根」而直接退出。
    (repo / "ops" / "tasks").mkdir(parents=True)
    (repo / "ops" / "truth").mkdir(parents=True)
    (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "truth" / "progress.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "central.config.json").write_text("{}\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def load_kgctl(repo: Path, state: Path):
    sys.path.insert(0, str(OPS))
    os.environ["KG_REPO"] = str(state)
    spec = importlib.util.spec_from_file_location("kgctl_trunk_probe", OPS / "kgctl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.REPO = state
    return mod


def main():
    with tempfile.TemporaryDirectory(prefix="autopilot-trunk-foreign-") as tmp:
        root = Path(tmp)
        state = seed_repo(root, "state")
        other = seed_repo(root, "other-clone")

        kg = load_kgctl(state, state)

        # 1) 判据用仓库身份：别的 clone 判 foreign，本仓 worktree / 自身判不算。
        other_trunk = other
        check("别的 clone 判为 foreign", kg._trunk_is_foreign_clone(other_trunk) is True,
              f"(path={other_trunk})")
        check("本仓库自身不算 foreign", kg._trunk_is_foreign_clone(state) is False,
              f"(path={state})")
        check("不存在的路径不猜（返回 False）",
              kg._trunk_is_foreign_clone(root / "nope") is False,
              "(path=%s)" % (root / "nope"))

        # 2) 配置指向 foreign clone 时，_trunk_worktree 必须退回本仓主干，
        #    绝不在别人的检出里做 checkout/merge/reset。
        kg.CFG = {"trunk_dir": str(other), "main_branch": "main"}
        kg.MAIN = "main"
        got = kg._trunk_worktree()
        check("配置指向 foreign clone 时退回本仓 <项目名>-trunk",
              Path(got).resolve() == (state.parent / (state.name + "-trunk")).resolve(),
              f"(got={got})")
        check("退回后不是别人的检出", Path(got).resolve() != other.resolve(),
              f"(got={got})")

    if failures:
        print(f"\nland trunk is same repo unit: FAIL ({len(failures)})")
        return 1
    print("\nland trunk is same repo unit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
