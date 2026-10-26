#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared conservative worktree lifecycle policy.

Source and live processes always win over disk savings. Only reproducible build
output may be removed from a dirty worktree; a clean worktree with unique
commits needs a remote backup before local removal.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


PROTECTED_NAMES = {"knowledge-graph", "kg-trunk", "kg-land-main"}
GENERATED_DIRS = (
    "frontend/node_modules",
    "backend/node_modules",
    "frontend/dist",
    "backend/dist",
)
# Tracked-but-regenerable output. These paths are build/test artifacts that the
# repo nonetheless committed at least once (e.g. backend/evidence, written by
# probe-*.mjs / build-progress-radar.mjs). Two consequences, both observed
# 2026-10-27:
#   1. every worktree checkout materialises its own ~131MB copy, so 50+ live
#      worktrees duplicate the same bytes (~6.25GB) with zero information gain;
#   2. because the content is tracked, `git status` reports it, so a worktree
#      that merely deleted those copies looks "dirty" and can never be reaped.
# The fix below does NOT delete unique history: before treating such a worktree
# as dirty it restores the tracked files from HEAD (content always preserved in
# git), which lets an otherwise-clean worktree be reaped normally.
REGENERABLE_TRACKED_DIRS = (
    "backend/evidence",
    "evidence",
)


def restore_regenerable_tracked(worktree: Path) -> list[str]:
    """Restore tracked files under regenerable output dirs from HEAD.

    Only ever runs `git checkout -- <dir>`, i.e. it puts files back to their
    committed state. It never stages, commits, stashes or discards anything a
    human wrote. Returns the dirs it touched.
    """
    touched = []
    for rel in REGENERABLE_TRACKED_DIRS:
        path = worktree / rel
        # Only act when the path actually differs from HEAD.
        diff = run_git(worktree, "status", "--porcelain", "--", rel)
        if diff.returncode != 0 or not (diff.stdout or "").strip():
            continue
        res = run_git(worktree, "checkout", "--", rel)
        if res.returncode == 0:
            touched.append(rel)
    return touched


def run_git(path: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
    )


def process_commands() -> str:
    try:
        result = subprocess.run(
            ["ps", "ax", "-o", "command="],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or "").lower() if result.returncode == 0 else ""


def process_reference(worktree: Path, commands: str | None = None) -> str:
    """Return a matching command when a live process references `worktree`."""
    if commands is None:
        commands = process_commands()
    if not commands:
        return ""
    candidates = {str(worktree), worktree.as_posix(), str(worktree.resolve())}
    candidates.add(os.path.realpath(worktree))
    needles = {path.rstrip("/").lower() for path in candidates if path}
    for raw in commands.splitlines():
        line = raw.strip().lower()
        for needle in needles:
            start = 0
            while True:
                pos = line.find(needle, start)
                if pos < 0:
                    break
                end = pos + len(needle)
                if end == len(line) or line[end] in "/ \t'\"=":
                    return line[:160]
                start = pos + 1
    return ""


def status(worktree: Path) -> str:
    result = run_git(worktree, "status", "--porcelain")
    return (result.stdout or "").strip() if result.returncode == 0 else "status-unavailable"


def remove_generated_dirs(worktree: Path) -> list[str]:
    """Delete only rebuildable dirs; never follow a symlink."""
    removed = []
    for rel in GENERATED_DIRS:
        path = worktree / rel
        if not path.exists() and not path.is_symlink():
            continue
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)
            removed.append(rel)
        except OSError:
            continue
    return removed


def branch_ahead(repo: Path, branch: str, main: str = "main") -> int:
    result = run_git(repo, "rev-list", "--count", f"{main}..{branch}")
    try:
        return int((result.stdout or "0").strip())
    except ValueError:
        return -1


def backup_branch(repo: Path, branch: str, stamp: str) -> tuple[bool, str]:
    """Push a branch, falling back to a unique backup ref on non-fast-forward."""
    remote_result = run_git(repo, "config", "--get", f"branch.{branch}.remote")
    remote = (remote_result.stdout or "").strip() if remote_result.returncode == 0 else ""
    if not remote or remote == ".":
        remote = "origin"
    pushed = run_git(repo, "push", remote, f"refs/heads/{branch}:refs/heads/{branch}",
                     timeout=180)
    if pushed.returncode == 0:
        return True, f"{remote}/{branch}"
    backup = f"{branch}-backup-{stamp}"
    pushed = run_git(repo, "push", remote, f"refs/heads/{branch}:refs/heads/{backup}",
                     timeout=180)
    detail = ((pushed.stdout or "") + (pushed.stderr or "")).strip()[-300:]
    return (pushed.returncode == 0), (
        f"{remote}/{backup}" if pushed.returncode == 0 else detail
    )


def worktree_removable(
    repo: Path,
    worktree: Path,
    branch: str | None = None,
    main: str = "main",
    *,
    commands: str | None = None,
    backup_unique: bool = False,
    stamp: str = "",
    restore_regenerable: bool = False,
) -> dict:
    """Assess a worktree without deleting source."""
    if not worktree.exists():
        return {"ok": False, "reason": "missing", "ahead": 0, "backup": ""}
    if worktree.resolve() == repo.resolve() or worktree.name in PROTECTED_NAMES:
        return {"ok": False, "reason": "protected", "ahead": 0, "backup": ""}
    live = process_reference(worktree, commands)
    if live:
        return {"ok": False, "reason": f"process: {live}", "ahead": 0, "backup": ""}

    ahead = branch_ahead(repo, branch, main) if branch else 0
    if status(worktree):
        remove_generated_dirs(worktree)
        # Opt-in: a worktree whose only "dirt" is a modified/deleted copy of a
        # tracked-but-regenerable artifact (backend/evidence etc.) is not a
        # human's work in progress. Restoring it from HEAD makes the assessment
        # honest instead of permanently "dirty". Off by default so the plain
        # reaper never rewrites anyone's tree.
        if restore_regenerable:
            restore_regenerable_tracked(worktree)
        if status(worktree):
            return {"ok": False, "reason": "dirty", "ahead": ahead, "backup": ""}

    backup = ""
    if branch and ahead > 0:
        if not backup_unique:
            return {"ok": False, "reason": "unique-commits", "ahead": ahead, "backup": ""}
        ok, backup = backup_branch(repo, branch, stamp)
        if not ok:
            return {"ok": False, "reason": "backup-failed", "ahead": ahead, "backup": backup}
    return {"ok": True, "reason": "clean", "ahead": ahead, "backup": backup}


def remove_worktree(repo: Path, worktree: Path) -> tuple[bool, str]:
    remove_generated_dirs(worktree)
    result = run_git(repo, "worktree", "remove", str(worktree))
    detail = ((result.stdout or "") + (result.stderr or "")).strip()
    return result.returncode == 0, detail[-300:]
