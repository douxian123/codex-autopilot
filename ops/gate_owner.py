#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kgctl gate 临时工作树的归属标记（stdlib only）。

为什么要有这个文件（L-0001，2026-11-18）：
  `kgctl gate` 在 tempfile.gettempdir() 下建 `kg-gate-*` worktree，正常结束时
  自己删；进程被 SIGKILL 或机器掉电时会留下目录。回收器不能只凭 `kg-` 前缀认领
  /private/tmp 里的东西，否则就可能删到别人的同名对象。标记写在 worktree 外的
  隐藏 sidecar：建树成功后才写，只有这里定义的结构能被回收器认可。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

OWNER = "codex-autopilot"
KIND = "kg-gate"
MARKER_PREFIX = ".codex-autopilot-kg-gate-owner-"
MARKER_SUFFIX = ".json"


def marker_path(worktree: Path) -> Path:
    """Return the hidden sidecar beside a gate worktree, never inside it."""
    path = Path(worktree)
    return path.parent / f"{MARKER_PREFIX}{path.name}{MARKER_SUFFIX}"


def write_marker(worktree: Path, branch: str) -> Path:
    """Atomically record who created this temporary gate worktree."""
    path = marker_path(worktree)
    payload = {
        "owner": OWNER,
        "kind": KIND,
        "worktree": Path(worktree).name,
        "branch": str(branch),
        "pid": os.getpid(),
        "created": time.time(),
    }
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_marker(worktree: Path) -> dict | None:
    """Return a valid marker, or None for missing/tampered/foreign markers."""
    path = marker_path(worktree)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("owner") != OWNER or data.get("kind") != KIND:
        return None
    if data.get("worktree") != Path(worktree).name:
        return None
    if not isinstance(data.get("branch"), str) or not data["branch"]:
        return None
    if not isinstance(data.get("created"), (int, float)):
        return None
    return data


def remove_marker(worktree: Path) -> None:
    """Remove the sidecar if present; missing sidecar is not an error."""
    marker_path(worktree).unlink(missing_ok=True)
