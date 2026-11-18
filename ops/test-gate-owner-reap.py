#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：kg-gate 崩溃残留必须带归属标记才允许回收（L-0001）。

为什么需要这条门禁：
  `kgctl gate` 过去用「pid + 分支」拼临时路径，并在建树前先
  `git worktree remove --force` 同路径对象。pid 复用时这等于替别人清现场；
  进程被 SIGKILL 后留下的目录又只按 `kg-` 前缀被回收器认领，同样分不清归属。

现在两条硬判据：
  1. 建树路径带随机后缀，不再覆盖同路径旧对象；
  2. 只有带有效 sidecar 标记、且过创建宽限期的 kg-gate-* 才进入回收，
     判活仍走现有 worktree guard 的 "all" 模式。

本文件用隔离的假 /private/tmp + 假 git worktree 锁住这两条，不碰真机对象。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))
import gate_owner  # noqa: E402

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


def touch_idle(path: Path) -> None:
    past = time.time() - 2 * 3600
    os.utime(path, (past, past))


def age_marker(worktree: Path, hours: float = 2) -> None:
    path = gate_owner.marker_path(worktree)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["created"] = time.time() - hours * 3600
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8")


def make_tree(root: Path, name: str, common_git: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    tree = root / name
    tree.mkdir()
    admin = common_git / "worktrees" / name
    admin.mkdir(parents=True, exist_ok=True)
    (tree / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    touch_idle(tree)
    return tree


class Guard:
    """最小 worktree guard：记录判活强度，按脚本回答，回收时真删隔离树。"""

    def __init__(self, ok: bool, reason: str = ""):
        self.ok = ok
        self.reason = reason
        self.modes = []

    def worktree_removable(self, *_a, **kw):
        self.modes.append(kw.get("live_guard_mode"))
        return {"ok": self.ok, "reason": self.reason or ("clean" if self.ok else "blocked")}

    def remove_worktree(self, _repo, worktree, **_kw):
        if not self.ok:
            return False, self.reason or "blocked"
        shutil.rmtree(worktree, ignore_errors=True)
        return True, "removed"


def main() -> int:
    dr = load("dr_gate_owner", "disk_reap.py")
    base = Path(tempfile.mkdtemp(prefix="gate-owner-reap-"))
    common_git = base / "repo" / ".git"
    orig = {
        "age": dr.KG_TMP_AGE_H,
        "pressure_age": dr.KG_TMP_AGE_PRESSURE_H,
        "grace": dr.KG_GATE_OWNER_CREATE_GRACE_H,
        "reap": os.environ.get("KG_TMP_REAP"),
        "procs": dr._process_lines,
        "registered": dr._registered_worktree,
        "common": dr._git_common_dir,
        "free": dr.free_gb,
    }
    try:
        os.environ["KG_TMP_REAP"] = "1"
        dr.KG_TMP_AGE_H = 0.0
        dr.KG_TMP_AGE_PRESSURE_H = 0.0
        dr.KG_GATE_OWNER_CREATE_GRACE_H = 0.0
        dr._process_lines = lambda: ""
        dr._registered_worktree = lambda _p: True
        dr._git_common_dir = lambda _p: common_git
        dr.free_gb = lambda: 100.0

        # 1. 无标记：历史/他人产物一律不碰。
        root = base / "tmp-no-marker"
        tree = make_tree(root, "kg-gate-no-marker", common_git)
        guard = Guard(ok=True)
        check("无标记的 kg-gate 不回收",
              dr._reap_kg_tmp(root=root, guard=guard) == 0
              and tree.exists() and not guard.modes,
              (tree.exists(), guard.modes))

        # 2. 标记被改成别的 owner：与无标记同等对待。
        root = base / "tmp-foreign-marker"
        tree = make_tree(root, "kg-gate-foreign-marker", common_git)
        marker = gate_owner.write_marker(tree, "codex/foreign")
        data = json.loads(marker.read_text(encoding="utf-8"))
        data["owner"] = "someone-else"
        marker.write_text(json.dumps(data) + "\n", encoding="utf-8")
        age_marker(tree)
        check("外部 owner 标记不回收",
              dr._reap_kg_tmp(root=root, guard=Guard(ok=True)) == 0 and tree.exists(),
              tree.exists())

        # 3. 新标记仍在创建宽限期内：目录已经闲置也不能回收。
        dr.KG_GATE_OWNER_CREATE_GRACE_H = 6.0
        root = base / "tmp-fresh-marker"
        tree = make_tree(root, "kg-gate-fresh-marker", common_git)
        gate_owner.write_marker(tree, "codex/fresh")
        touch_idle(tree)
        check("创建宽限期内的 gate 不回收",
              dr._reap_kg_tmp(root=root, guard=Guard(ok=True)) == 0 and tree.exists(),
              tree.exists())
        dr.KG_GATE_OWNER_CREATE_GRACE_H = 0.0

        # 4. 活会话命中：仍必须问最强的 all，命中就保留目录和标记。
        root = base / "tmp-live-marker"
        tree = make_tree(root, "kg-gate-live-marker", common_git)
        gate_owner.write_marker(tree, "codex/live")
        age_marker(tree)
        guard = Guard(ok=False, reason="live-session: active")
        check("活会话占用的 gate 不回收",
              dr._reap_kg_tmp(root=root, guard=guard) == 0
              and tree.exists() and gate_owner.marker_path(tree).exists(),
              (tree.exists(), guard.modes))
        check("gate 回收仍用 all 强度判活",
              guard.modes == ["all"], guard.modes)

        # 5. 过期标记 + 无人占用：真回收，并清掉 sidecar。
        root = base / "tmp-dead-marker"
        tree = make_tree(root, "kg-gate-dead-marker", common_git)
        gate_owner.write_marker(tree, "codex/dead")
        age_marker(tree)
        marker = gate_owner.marker_path(tree)
        guard = Guard(ok=True)
        check("过期归属 gate 可回收",
              dr._reap_kg_tmp(root=root, guard=guard) == 1 and not tree.exists(),
              (tree.exists(), guard.modes))
        check("回收后 sidecar 一并清掉", not marker.exists(), marker)

        # 6. dry-run 只统计，不删目录、不删标记。
        root = base / "tmp-dry-run"
        tree = make_tree(root, "kg-gate-dry-run", common_git)
        gate_owner.write_marker(tree, "codex/dry")
        age_marker(tree)
        marker = gate_owner.marker_path(tree)
        check("dry-run 统计候选",
              dr._reap_kg_tmp(dry=True, root=root, guard=Guard(ok=True)) == 1,
              "dry-run 未统计")
        check("dry-run 不删目录和标记",
              tree.exists() and marker.exists(),
              (tree.exists(), marker.exists()))
    finally:
        for key, value in orig.items():
            if key == "reap":
                if value is None:
                    os.environ.pop("KG_TMP_REAP", None)
                else:
                    os.environ["KG_TMP_REAP"] = value
            else:
                setattr(dr, {
                    "age": "KG_TMP_AGE_H",
                    "pressure_age": "KG_TMP_AGE_PRESSURE_H",
                    "grace": "KG_GATE_OWNER_CREATE_GRACE_H",
                    "procs": "_process_lines",
                    "registered": "_registered_worktree",
                    "common": "_git_common_dir",
                    "free": "free_gb",
                }[key], value)
        shutil.rmtree(base, ignore_errors=True)

    if failures:
        print("\n失败项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\ngate owner reap unit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
