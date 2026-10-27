#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared conservative worktree lifecycle policy.

Source and live processes always win over disk savings. Only reproducible build
output may be removed from a dirty worktree; a clean worktree with unique
commits needs a remote backup before local removal.
"""

from __future__ import annotations

import os
import re
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

# Machine-written scratch that every task worktree accumulates, and that no human
# ever authored. These are *untracked* by definition (matched by `??`), so
# restoring them from HEAD is impossible -- there is nothing in HEAD to restore.
# They must be deleted instead, and deleting them loses nothing.
#
# Why this matters (measured 2026-10-27 on this machine): 244 worktrees, 224 of
# them "dirty", and the reaper reclaimed exactly 0 per hour for days. The blocker
# was not human work at all -- it was:
#   .kg-task-env                     82 worktrees  (new-task.sh scratch, 121 bytes)
#   frontend/.verify-tmp/            21 worktrees  (browser-gate scratch dirs)
#   data/dev.db-shm + -wal           20 worktrees  (SQLite shared-memory sidecars)
# Any one of these makes `git status` non-empty, so `worktree_removable` returned
# dirty forever. The capacity existed; the classifier kept refusing to see it.
#
# The trap this list has to avoid: `ops/truth/*.jsonl` and `ops/tasks/inbox.jsonl`
# are ALSO machine-written and ALSO show up dirty, but they are *tracked* and they
# carry the autopilot's own decision ledger. Deleting or resetting those would
# destroy real state, so they are deliberately absent here. Tracked-but-dirty is
# never machine noise: it is either someone's work or a shared ledger.
UNTRACKED_SCRATCH_PATTERNS = (
    re.compile(r"^\.kg-task-env$"),
    re.compile(r"^(?:frontend/|backend/)?\.verify-tmp/"),
    re.compile(r"^(?:backend/)?data/.*\.db-(?:shm|wal|journal)$"),
    re.compile(r".*\.pyc$"),
    re.compile(r"^(?:.*/)?__pycache__/"),
    re.compile(r"^(?:.*/)?\.DS_Store$"),
    re.compile(r"^(?:.*/)?[^/]*\.log$"),
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


def is_untracked_scratch(path: str) -> bool:
    """True when a relative path is machine scratch no human authored."""
    return any(p.match(path) for p in UNTRACKED_SCRATCH_PATTERNS)


def dirty_paths(worktree: Path) -> list[tuple[str, str]]:
    """Return [(status_code, relative_path)] for everything git reports."""
    result = run_git(worktree, "status", "--porcelain", "-uall")
    if result.returncode != 0:
        return []
    out = []
    for line in (result.stdout or "").splitlines():
        if not line.strip():
            continue
        code, rest = line[:2], line[3:]
        path = rest.strip().strip('"')
        # Renames show as `old -> new`; the target is what exists on disk.
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip().strip('"')
        out.append((code, path))
    return out


def only_untracked_scratch(worktree: Path) -> bool:
    """True when every dirty entry is untracked machine scratch.

    Deliberately strict: a single tracked modification, or a single untracked
    file that is not on the scratch allowlist, returns False. That keeps the
    promise that nothing a human wrote is ever discarded to free disk space.
    """
    entries = dirty_paths(worktree)
    if not entries:
        return True
    for code, path in entries:
        if code != "??" or not is_untracked_scratch(path):
            return False
    return True


def drop_untracked_scratch(worktree: Path) -> list[str]:
    """Delete untracked scratch files. Never touches tracked or unknown files.

    Only called after `only_untracked_scratch` said every dirty entry is on the
    allowlist, so this can only remove things git never tracked.
    """
    removed = []
    for code, path in dirty_paths(worktree):
        if code != "??" or not is_untracked_scratch(path):
            continue
        target = worktree / path
        try:
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            else:
                continue
            removed.append(path)
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
    drop_scratch: bool = False,
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
        # Opt-in: when the *only* thing making this tree dirty is untracked
        # machine scratch (.kg-task-env, SQLite -shm/-wal, .verify-tmp), erase
        # that scratch so a genuinely-abandoned worktree can be reclaimed.
        # Default OFF, and even when ON it does nothing unless every single
        # dirty entry is on the untracked allowlist -- one tracked edit keeps
        # the tree alive, exactly as before. See UNTRACKED_SCRATCH_PATTERNS for
        # why the tracked ops/truth ledgers are excluded on purpose.
        scratch_note = ""
        if drop_scratch and only_untracked_scratch(worktree):
            removed = drop_untracked_scratch(worktree)
            if removed:
                detail = ", ".join(removed[:5])
                more = "" if len(removed) <= 5 else f" (+{len(removed) - 5})"
                scratch_note = f"{detail}{more}"
        if status(worktree):
            return {"ok": False, "reason": "dirty", "ahead": ahead, "backup": ""}
    else:
        scratch_note = ""

    backup = ""
    if branch and ahead > 0:
        if not backup_unique:
            return {"ok": False, "reason": "unique-commits", "ahead": ahead, "backup": ""}
        ok, backup = backup_branch(repo, branch, stamp)
        if not ok:
            return {"ok": False, "reason": "backup-failed", "ahead": ahead, "backup": backup}
    reason = "clean" if not scratch_note else "scratch-only"
    return {"ok": True, "reason": reason, "ahead": ahead, "backup": backup,
            "scratch": scratch_note}


def remove_worktree(repo: Path, worktree: Path) -> tuple[bool, str]:
    remove_generated_dirs(worktree)
    result = run_git(repo, "worktree", "remove", str(worktree))
    detail = ((result.stdout or "") + (result.stderr or "")).strip()
    return result.returncode == 0, detail[-300:]
