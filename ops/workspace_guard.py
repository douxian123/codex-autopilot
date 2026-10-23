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
