#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：失效 worktree 注册不能把 `kgctl gate` 骗进不存在的 cwd。

2026-11-22 实测：分支注册路径 `/private/tmp/autopilot-main-clean-final.HZn0HX`
早已消失，但 `git worktree list --porcelain` 仍列出它。`worktree_map()` 旧实现
照单全收，`kgctl gate` 于是选了这个死路径，selftest / panel-logic / panel-render
全部报 `cwd 不存在`。本门禁锁住过滤判据：只映射目录真在、且带 `.git` 的注册。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KGCTL_PATH = ROOT / "ops" / "kgctl.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


with tempfile.TemporaryDirectory(prefix="kg-stale-worktree-map-") as tmp:
    repo = Path(tmp) / "state"
    (repo / "ops" / "tasks").mkdir(parents=True)
    (repo / "ops" / "truth").mkdir(parents=True)
    (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
    for name in ("ledger.jsonl", "progress.jsonl", "acceptance.jsonl"):
        (repo / "ops" / "truth" / name).write_text("", encoding="utf-8")
    (repo / "ops" / "truth" / "mainline.json").write_text("{}\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Worktree Map Test")

    live = Path(tmp) / "live"
    live.mkdir()
    (live / ".git").write_text(f"gitdir: {repo / '.git' / 'worktrees' / 'live'}\n",
                               encoding="utf-8")
    stale = Path(tmp) / "stale-does-not-exist"

    porcelain = "\n".join([
        f"worktree {repo}",
        "HEAD " + "a" * 40,
        "branch refs/heads/main",
        "",
        f"worktree {live}",
        "HEAD " + "b" * 40,
        "branch refs/heads/codex/live",
        "",
        f"worktree {stale}",
        "HEAD " + "c" * 40,
        "branch refs/heads/codex/stale",
        "prunable gitdir file points to non-existent location",
        "",
    ])

    old_repo = os.environ.get("KG_REPO")
    old_trunk = os.environ.get("KG_TRUNK")
    os.environ["KG_REPO"] = str(repo)
    os.environ["KG_TRUNK"] = str(repo)
    try:
        spec = importlib.util.spec_from_file_location("kgctl_worktree_map_test", KGCTL_PATH)
        assert spec and spec.loader
        kgctl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(kgctl)
        kgctl.git = lambda *_args: porcelain
        mapping = kgctl.worktree_map()
        assert mapping.get("main") == str(repo), mapping
        assert mapping.get("codex/live") == str(live), mapping
        assert "codex/stale" not in mapping, mapping
    finally:
        if old_repo is None:
            os.environ.pop("KG_REPO", None)
        else:
            os.environ["KG_REPO"] = old_repo
        if old_trunk is None:
            os.environ.pop("KG_TRUNK", None)
        else:
            os.environ["KG_TRUNK"] = old_trunk

print("test-stale-worktree-map: ok")
