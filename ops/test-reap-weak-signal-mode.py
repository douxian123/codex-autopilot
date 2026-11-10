#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：每一条会删东西的回收路径都必须问「有没有人正在用」，而且是最强的问法。

历史缺陷（2026-11-10 复查）：
  `live_guard_blocker(worktree, mode)` 有两个强度：
    * "cwd" -- 只问内核：有没有活进程把这棵树当工作目录；
    * "all" -- 再加一条：最近写过的 Codex 会话记录里，cwd 是不是这棵树。

  只读会话（正在看代码、想问题、等你回话）不会留下任何持有 cwd 的长命进程，
  也不会刷新目录 mtime。于是它在 "cwd" 模式下读起来和废弃现场一模一样。

  主回收通道（central_daemon._reap_dead_worktrees -> worktree_removable）用了 "all"，
  但另外三条同类通道漏了，各自只认 "cwd"：
    1. disk_reap._remove_one_kg_tmp 里对 /private/tmp 下已登记 kg-* worktree 的回收；
    2. disk_reap.reap_regenerable_duplicates（剥离重复副本）；
    3. workspace_guard.orphan_residue（孤儿残留目录）。

  本文件把这三条钉住：断言它们确实以 "all" 提问，且命中判活时一定不删。

反向断言同样重要：判活不能把每一棵树都永久钉住。最后一节用一棵真正闲置、
干净、无人使用的树，确认它在最强模式下仍可回收。
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))
import live_guard  # noqa: E402
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


def write_transcript(root: Path, name: str, cwd: str) -> None:
    """一条 rollout 记录，cwd 指向某棵树 —— 真会话在磁盘上的样子。"""
    path = root / "2026" / "11" / "10" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"type": "session_meta",
           "payload": {"cwd": cwd, "runtime_workspace_roots": [cwd]}}
    path.write_text(json.dumps(rec) + "\n")


class RecordingGuard:
    """记录「被问到的判活强度」，并按脚本回答。"""

    PROTECTED_NAMES = {"knowledge-graph", "kg-trunk", "kg-land-main"}
    PROTECTED_PREFIXES = ("kg-live-",)

    def __init__(self, blocker=""):
        self.blocker = blocker
        self.modes = []
        self.stripped = []
        self.removed = []

    def process_reference(self, *_a, **_k):
        return ""

    def live_guard_blocker(self, _worktree, mode="cwd"):
        self.modes.append(mode)
        return self.blocker

    def regenerable_dirs_state(self, _worktree):
        return {"evidence": "pristine"}

    def dirty_outside_regenerable(self, _worktree):
        return []

    def strip_regenerable_tracked(self, worktree):
        self.stripped.append(worktree)
        return {"evidence": 123}

    def worktree_removable(self, *_a, **kw):
        self.modes.append(kw.get("live_guard_mode"))
        return {"ok": False, "reason": "recorded"}

    def remove_worktree(self, *_a, **_k):
        self.removed.append(_a)
        return True, "recorded"


def main():
    lg = live_guard
    wg = load("wg_weak_signal", "workspace_guard.py")
    dr = load("dr_weak_signal", "disk_reap.py")
    base = Path(tempfile.mkdtemp(prefix="reap-weak-signal-"))
    session_root = base / "sessions"
    session_root.mkdir()
    os.environ["KG_SESSION_ROOTS"] = str(session_root)
    os.environ["KG_LIVE_SESSION_MAX_AGE_S"] = "10800"

    # ---- 1. 剥离重复副本：必须用 "all" 提问，命中就跳过 ----
    strip_tree = base / "kg-strip-candidate"
    strip_tree.mkdir()
    (strip_tree / ".git").write_text("gitdir: /nowhere/.git\n")
    write_transcript(session_root, "rollout-strip.jsonl", str(strip_tree))
    lg.reset_caches()
    guard = RecordingGuard(blocker="live-session: rollout-strip.jsonl")
    os.environ["KG_WORKSPACE_ROOT"] = str(base)
    os.environ["KG_STRIP_DUPLICATES"] = "1"
    n, freed = dr.reap_regenerable_duplicates(dry=True, guard=guard, idle_h=0.0)
    check("剥离重复副本用 all 模式提问",
          guard.modes == ["all"], guard.modes)
    check("只读会话在用时不剥离",
          n == 0 and freed == 0 and guard.stripped == [],
          (n, freed, guard.stripped))

    # ---- 2. /private/tmp 下已登记 kg-* worktree 的回收：必须用 "all" ----
    tmp_tree = base / "kg-tmp-registered"
    tmp_tree.mkdir()
    (tmp_tree / ".git").write_text("gitdir: /nowhere/.git\n")
    orig_reg = dr._registered_worktree
    orig_common = dr._git_common_dir
    dr._registered_worktree = lambda _p: True
    dr._git_common_dir = lambda _p: (base / "repo" / ".git")
    try:
        guard2 = RecordingGuard()
        ok = dr._remove_one_kg_tmp(tmp_tree, "", guard=guard2)
    finally:
        dr._registered_worktree = orig_reg
        dr._git_common_dir = orig_common
    check("kg tmp 已登记 worktree 用 all 模式提问",
          guard2.modes == ["all"], guard2.modes)
    check("kg tmp 判活为真时不回收", ok is False, ok)

    # ---- 3. 孤儿残留：只读会话在用时不删 ----
    orphan = base / "kg-orphan-residue"
    (orphan / "node_modules").mkdir(parents=True)
    (orphan / "vite.log").write_text("x\n")
    write_transcript(session_root, "rollout-orphan.jsonl", str(orphan))
    lg.reset_caches()
    # 目录 mtime 要够旧：min_idle_h=0 时任何过去时间都过闸。
    past = time.time() - 3600
    os.utime(orphan, (past, past))
    assessment = wg.orphan_residue(orphan, min_idle_h=0.0)
    check("孤儿残留认出只读会话在用",
          assessment.get("ok") is False, assessment)
    check("孤儿残留拒绝理由是判活",
          "live" in str(assessment.get("reason")), assessment)

    # ---- 4. 反向断言：真死树在最强模式下仍可回收 ----
    dead = base / "kg-really-dead"
    dead.mkdir()
    (dead / "node_modules").mkdir()
    (dead / "vite.log").write_text("x\n")
    os.utime(dead, (past, past))
    lg.reset_caches()
    dead_assessment = wg.orphan_residue(dead, min_idle_h=0.0)
    check("闲置且无人在用的孤儿仍可回收",
          dead_assessment.get("ok") is True, dead_assessment)

    if failures:
        print("\n失败项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\nreap weak-signal mode unit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
