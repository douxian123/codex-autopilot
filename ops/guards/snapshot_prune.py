#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Conservatively prune Codex session snapshots without losing history.

A snapshot is removable only when every regular file in it is a byte-prefix of
the same path in the current sessions directory or in a newer snapshot. That
covers append-only JSONL logs, while preserving deleted, truncated, rewritten,
or otherwise unique historical content.
"""
import argparse
import os
import shutil
from pathlib import Path


def _is_prefix(candidate: Path, covering: Path) -> bool:
    if candidate.is_symlink():
        return covering.is_symlink() and os.readlink(candidate) == os.readlink(covering)
    if not candidate.is_file() or covering.is_symlink() or not covering.is_file():
        return False

    try:
        candidate_stat = candidate.stat()
        covering_stat = covering.stat()
        if (
            candidate_stat.st_dev == covering_stat.st_dev
            and candidate_stat.st_ino == covering_stat.st_ino
        ):
            return True
        wanted = candidate_stat.st_size
        if covering_stat.st_size < wanted:
            return False
        if wanted == 0:
            return True
        with candidate.open("rb") as left, covering.open("rb") as right:
            remaining = wanted
            while remaining:
                size = min(1024 * 1024, remaining)
                lhs = left.read(size)
                rhs = right.read(size)
                if lhs != rhs:
                    return False
                if not lhs:
                    return False
                remaining -= len(lhs)
    except OSError:
        return False
    return True


def snapshot_is_covered(candidate: Path, covering_roots: list[Path]) -> tuple[bool, str]:
    """Return whether every file in one snapshot exists in newer content."""
    try:
        entries = sorted(candidate.rglob("*"))
    except OSError as exc:
        return False, f"无法枚举: {exc}"

    for entry in entries:
        rel = entry.relative_to(candidate)
        if entry.is_dir() and not entry.is_symlink():
            continue
        found = False
        for root in covering_roots:
            newer = root / rel
            if newer.exists() or newer.is_symlink():
                if _is_prefix(entry, newer):
                    found = True
                    break
        if not found:
            return False, str(rel)
    return True, ""


def prunable_snapshots(source: Path, snapshots: Path, keep: int = 48) -> tuple[list[Path], list[tuple[Path, str]]]:
    dirs = sorted(
        (p for p in snapshots.iterdir() if p.is_dir() and not p.is_symlink()),
        key=lambda p: p.name,
        reverse=True,
    )
    removable: list[Path] = []
    retained: list[tuple[Path, str]] = []
    for index, candidate in enumerate(dirs):
        if index < keep:
            continue
        newer = [source, *dirs[:index]]
        safe, reason = snapshot_is_covered(candidate, newer)
        if safe:
            removable.append(candidate)
        else:
            retained.append((candidate, reason))
    return removable, retained


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--snapshots", type=Path, required=True)
    parser.add_argument("--keep", type=int, default=48)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    removable, retained = prunable_snapshots(
        args.source, args.snapshots, max(1, args.keep))
    removed = 0
    for path in removable:
        print(f"snapshot prune remove {path.name} (all files covered)")
        if not args.dry_run:
            try:
                resolved = path.resolve(strict=True)
                parent = args.snapshots.resolve(strict=True)
                if resolved.parent != parent:
                    print(f"snapshot prune keep {path.name} (path escaped snapshots root)")
                    continue
                shutil.rmtree(path)
                removed += 1
            except OSError as exc:
                print(f"snapshot prune keep {path.name} ({exc})")
    for path, reason in retained:
        print(f"snapshot prune keep {path.name} (unique content: {reason})")
    print(
        f"snapshot prune summary: removed={removed} "
        f"candidates={len(removable)} retained_unique={len(retained)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
