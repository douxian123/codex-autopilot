#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：默认状态根不能让停在旧分支的共享检出压过活的主干工作树。

事故证据（2026-11-10 / L-0003）：共享检出停在 1a6c56b4，树里没有
`ops/central.config.json`，但 `ops/tasks` + `ops/truth` 都在版本库里，所以旧逻辑
`for cand in (main, trunk, current)` 仍然选它。接着 central_config 静默退回
`npm run build (cwd=frontend)`，land 在 merge 之后必红，看起来像门禁失败。

本门禁锁死：
  1. KG_REPO 显式指定时仍然最高优先；
  2. 共享检出没配置、trunk 有配置时，必须选 trunk；
  3. 共享检出和 trunk 都有配置时，选 HEAD 更新的那棵，避免陈旧共享检出胜出；
  4. 配置损坏按“不可用”处理，不能让坏配置挡住活状态根。
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


def git(repo, *args, date=None):
    env = os.environ.copy()
    if date:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, env=env)


def make_root(base: Path, name: str, config, date: str):
    repo = base / name
    for rel in ("ops/tasks", "ops/truth"):
        (repo / rel).mkdir(parents=True, exist_ok=True)
    (repo / "ops/tasks/inbox.jsonl").write_text("", encoding="utf-8")
    (repo / "ops/truth/progress.jsonl").write_text("", encoding="utf-8")
    if config is not None:
        (repo / "ops/central.config.json").write_text(config, encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "state-root@example.invalid")
    git(repo, "config", "user.name", "State Root Test")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init", date=date)
    return repo


def load_state_root():
    spec = importlib.util.spec_from_file_location("state_root_freshness", OPS / "state_root.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    old_env = {k: os.environ.get(k) for k in ("KG_REPO", "KG_TRUNK")}
    valid_config = '{"build": {"cmd": "echo OK", "cwd": "."}}\n'

    try:
        with tempfile.TemporaryDirectory(prefix="state-root-freshness-") as raw:
            root = Path(raw)
            stale = make_root(root, "shared-stale", None,
                              "2026-01-01T00:00:00 +0800")
            trunk = make_root(root, "shared-stale-trunk", valid_config,
                              "2026-02-01T00:00:00 +0800")
            current = make_root(root, "task-worktree", None,
                                "2026-03-01T00:00:00 +0800")

            os.environ.pop("KG_REPO", None)
            os.environ["KG_TRUNK"] = str(trunk)
            sr = load_state_root()
            sr.main_worktree = lambda: stale
            sr.current_worktree = lambda: current

            got = sr.resolve_state_root()
            check("无配置共享检出不能压过有配置 trunk",
                  got == trunk.resolve(), f"(got={got}, want={trunk.resolve()})")

            os.environ["KG_REPO"] = str(stale)
            got_explicit = sr.resolve_state_root()
            check("KG_REPO 显式指定仍最高优先",
                  got_explicit == stale.resolve(),
                  f"(got={got_explicit}, want={stale.resolve()})")

            os.environ.pop("KG_REPO", None)
            fresh_shared = make_root(root, "shared-with-config", valid_config,
                                     "2026-01-01T00:00:00 +0800")
            fresh_trunk = make_root(root, "shared-with-config-trunk", valid_config,
                                    "2026-02-01T00:00:00 +0800")
            os.environ["KG_TRUNK"] = str(fresh_trunk)
            sr2 = load_state_root()
            sr2.main_worktree = lambda: fresh_shared
            sr2.current_worktree = lambda: current
            got_fresh = sr2.resolve_state_root()
            check("两边都有配置时选 HEAD 更新的 trunk",
                  got_fresh == fresh_trunk.resolve(),
                  f"(got={got_fresh}, want={fresh_trunk.resolve()})")

            broken_shared = make_root(root, "shared-broken-config", "{not json\n",
                                      "2026-01-01T00:00:00 +0800")
            fixed_trunk = make_root(root, "shared-broken-config-trunk", valid_config,
                                    "2026-01-02T00:00:00 +0800")
            os.environ["KG_TRUNK"] = str(fixed_trunk)
            sr3 = load_state_root()
            sr3.main_worktree = lambda: broken_shared
            sr3.current_worktree = lambda: current
            got_broken = sr3.resolve_state_root()
            check("损坏配置按不可用处理",
                  got_broken == fixed_trunk.resolve(),
                  f"(got={got_broken}, want={fixed_trunk.resolve()})")
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    if failures:
        print(f"\nstate root freshness: FAIL ({len(failures)})")
        return 1
    print("\nstate root freshness: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
