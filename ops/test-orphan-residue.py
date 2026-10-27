#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression: `Directory not empty` orphans must not survive forever.

Why this file exists (measured 2026-10-27):
  `git worktree remove` fails with `Directory not empty` when the tree still
  holds something git refuses to delete. The directory stays on disk but is no
  longer registered, so `_reap_dead_worktrees` -- which walks *registered*
  worktrees -- can never see it again. 44 such orphans had accumulated in the
  workspace root; `kg-growth-feedback-long-task-main` had been reported as
  `Directory not empty` at 07:33 and was still sitting there hours later.

  The fix adds `workspace_guard.orphan_residue` / `remove_orphan_residue` plus
  `central_daemon._reap_orphan_residue`. The dangerous direction is the other
  one: deleting a directory that still holds someone's work. So the assertions
  below are weighted towards "must NOT delete".
"""
import importlib.util
import os
import sys
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


def load_guard():
    spec = importlib.util.spec_from_file_location("wg_under_test", OPS / "workspace_guard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def age(path, hours):
    """Backdate a directory so the idle gate lets it through."""
    ts = time.time() - hours * 3600
    os.utime(path, (ts, ts))


def make(path, files):
    path.mkdir(parents=True)
    for rel, body in files.items():
        target = path / rel
        if body is None:            # None == directory
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)


def main():
    g = load_guard()
    base = Path(os.environ["ORPHAN_TEST_ROOT"])

    # 1. The real residue shape: a reclaimed worktree whose only leftover is the
    #    Vite cache inside node_modules. Must be reclaimable.
    residue = base / "kg-growth-feedback-long-task-main"
    make(residue, {
        "node_modules/.vite/deps_temp_a4b45bf3/package.json": "{}",
    })
    age(residue, 48)
    check("只剩 Vite 缓存的孤儿判为可回收", g.orphan_residue(residue).get("ok") is True,
          g.orphan_residue(residue))

    # 2. One authored file anywhere -> refuse. This is the assertion that keeps
    #    the reaper from becoming a data-loss tool.
    for label, files in {
        "根下有个源码文件": {"src/main.ts": "export const x = 1;"},
        "缓存旁边有人写的说明": {"node_modules/.vite/x/package.json": "{}", "NOTES.md": "重要"},
        "深层有个未跟踪的台账": {"data/ledger.jsonl": "{}"},
    }.items():
        d = base / f"kg-refuse-{abs(hash(label)) % 10000}"
        make(d, files)
        age(d, 48)
        check(f"含人工内容不回收（{label}）", g.orphan_residue(d).get("ok") is False,
              g.orphan_residue(d))

    # 3. Still registered as a worktree -> has an owner, not ours.
    registered = base / "kg-still-registered"
    make(registered, {"node_modules/x": "{}"})
    (registered / ".git").write_text("gitdir: /nowhere/.git/worktrees/x\n")
    age(registered, 48)
    check("仍登记的 worktree 不进孤儿通道", g.orphan_residue(registered).get("reason") == "registered",
          g.orphan_residue(registered))

    # 4. Not a machine-generated name -> hand-made dirs are never candidates,
    #    even when they are empty.
    handmade = base / "my-important-scratch"
    make(handmade, {"node_modules/x": "{}"})
    age(handmade, 48)
    check("手工目录名不进孤儿通道", g.orphan_residue(handmade).get("reason") == "name",
          g.orphan_residue(handmade))

    # 4b. The deployed live copy is not a worktree and has no owner in git, so
    #     nothing else protects it. It must be excluded by name, not by luck.
    live_copy = base / "kg-live-3000"
    make(live_copy, {"node_modules/.vite/x/package.json": "{}"})
    age(live_copy, 24 * 30)
    check("常驻线上副本永不被回收（名称保护）",
          g.orphan_residue(live_copy).get("reason") == "protected",
          g.orphan_residue(live_copy))

    # 5. Just failed -> give the owner time to retry.
    fresh = base / "kg-just-failed"
    make(fresh, {"node_modules/x": "{}"})
    check("刚失败的现场留观察期", g.orphan_residue(fresh).get("reason") == "recent",
          g.orphan_residue(fresh))

    # 6. Deleting happens only through the guarded entry point.
    check("未通过判据时删除被拒", g.remove_orphan_residue(fresh)[0] is False)
    check("被拒后目录仍完整", fresh.is_dir())
    ok, detail = g.remove_orphan_residue(residue)
    check("通过判据后真的删掉", ok is True and not residue.exists(), detail)

    # 7. A symlink at top level must not trick us into following it out of the
    #    tree: deleting the orphan must not touch the target.
    outside = base / "outside-precious"
    make(outside, {"keep.txt": "must survive"})
    linky = base / "kg-with-symlink"
    make(linky, {})
    os.symlink(outside, linky / "node_modules_link")
    age(linky, 48)
    if g.orphan_residue(linky).get("ok"):
        g.remove_orphan_residue(linky)
    check("删孤儿不顺着符号链接删到外面", (outside / "keep.txt").exists())

    if failures:
        print("\n失败项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\norphan residue unit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
