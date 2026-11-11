#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Conservatively prune Codex session snapshots without losing history.

A snapshot is removable only when every regular file in it is a byte-prefix of
the same path in the current sessions directory or in a newer snapshot. That
covers append-only JSONL logs, while preserving deleted, truncated, rewritten,
or otherwise unique historical content.
"""
import argparse
import hashlib
import os
import re
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


def snapshot_uncovered_files(
    candidate: Path, covering_roots: list[Path]
) -> list[Path]:
    """Return files whose historical bytes are not covered by newer content."""
    uncovered: list[Path] = []
    try:
        entries = sorted(candidate.rglob("*"))
    except OSError:
        return [candidate]

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
            uncovered.append(entry)
    return uncovered


def snapshot_is_covered(candidate: Path, covering_roots: list[Path]) -> tuple[bool, str]:
    """Return whether every file in one snapshot exists in newer content."""
    uncovered = snapshot_uncovered_files(candidate, covering_roots)
    if uncovered:
        return False, str(uncovered[0].relative_to(candidate))
    return True, ""


class CoveredNameIndex:
    """Index newer files by name for renamed-file coverage checks."""

    def __init__(self) -> None:
        self._files: dict[str, list[tuple[int, Path]]] = {}

    def add_tree(self, root: Path) -> None:
        if not root.is_dir():
            return
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                try:
                    self.add(path)
                except OSError:
                    continue

    def add(self, path: Path) -> None:
        size = path.stat().st_size
        self._files.setdefault(path.name, []).append((size, path))

    def covering(self, candidate: Path) -> Path | None:
        wanted = candidate.stat().st_size
        for size, path in self._files.get(candidate.name, []):
            if size >= wanted and _is_prefix(candidate, path):
                return path
        return None

    def covering_any(self, candidate: Path) -> Path | None:
        wanted = candidate.stat().st_size
        for rows in self._files.values():
            for size, path in rows:
                if size >= wanted and _is_prefix(candidate, path):
                    return path
        return None


def safe_rel(rel: Path) -> Path:
    parts = [re.sub(r"[^A-Za-z0-9._-]+", "_", part) for part in rel.parts]
    parts = [part or "_" for part in parts]
    return Path(*parts[-8:])


def content_key(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest(), path.stat().st_size


def archive_unique(
    path: Path,
    archive: Path,
    known_content: dict[tuple[str, int], Path],
    global_queue: list[Path],
    archive_bytes: list[int],
    archive_cap: int,
) -> tuple[bool, str]:
    """Move unique bytes into the protection archive without exceeding its cap."""
    key = content_key(path)
    if key in known_content:
        path.unlink()
        return True, ""

    needed = path.stat().st_size
    if archive_cap >= 0 and archive_bytes[0] + needed > archive_cap:
        return False, "archive capacity reached"

    archive.mkdir(parents=True, exist_ok=True)
    staged = archive / f".incoming-{key[0][:20]}-{os.getpid()}"
    if staged.exists():
        staged.unlink()
    os.replace(path, staged)

    rel = safe_rel(Path(*path.parts[-8:]))
    destination = archive / rel.parent / f"{rel.name}.{key[0][:16]}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        try:
            if content_key(destination) == key:
                staged.unlink()
                known_content[key] = destination
                return True, ""
        except OSError:
            pass
    os.replace(staged, destination)
    known_content[key] = destination
    global_queue.append(destination)
    archive_bytes[0] += needed
    return True, ""


def prune_snapshot_files(
    candidate: Path,
    covering_roots: list[Path],
    name_index: CoveredNameIndex,
    archive: Path,
    known_content: dict[tuple[str, int], Path],
    global_queue: list[Path],
    archive_bytes: list[int],
    archive_cap: int,
) -> tuple[bool, str]:
    """Remove covered files; archive every unique historical byte sequence."""
    failed: list[str] = []
    for path in sorted(candidate.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(candidate)
        covered = False
        for root in covering_roots:
            newer = root / rel
            if newer.exists() or newer.is_symlink():
                if _is_prefix(path, newer):
                    covered = True
                    break
        if not covered and (name_index.covering(path) or name_index.covering_any(path)):
            covered = True
        if covered:
            try:
                path.unlink()
            except OSError:
                failed.append(str(rel))
            continue
        try:
            archived, reason = archive_unique(
                path, archive, known_content, global_queue,
                archive_bytes, archive_cap)
            if not archived:
                return False, f"{rel}: {reason}"
        except OSError:
            failed.append(str(rel))
    return not failed, failed[0] if failed else ""


def archive_usage(archive: Path) -> int:
    if not archive.is_dir():
        return 0
    return sum(
        path.stat().st_size
        for path in archive.rglob("*")
        if path.is_file()
        and path.name != ".source-stamp"
        and not path.name.startswith(".incoming-")
    )


def prunable_snapshots(source: Path, snapshots: Path, keep: int = 48) -> tuple[list[Path], list[tuple[Path, str]]]:
    dirs = sorted(
        (p for p in snapshots.iterdir() if p.is_dir() and not p.is_symlink()),
        key=lambda p: (p.stat().st_mtime_ns, p.name),
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
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--archive-cap-mib", type=int, default=2048)
    parser.add_argument("--keep-archive", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    archive = args.archive or (args.snapshots.parent / "archive")
    if not args.dry_run:
        archive.mkdir(parents=True, exist_ok=True)
    known_content: dict[tuple[str, int], Path] = {}
    global_queue: list[Path] = []
    name_index = CoveredNameIndex()
    archive_cap = args.archive_cap_mib * 1024 * 1024
    archive_bytes = [archive_usage(archive) if not args.keep_archive else 0]
    dirs = sorted(
        (p for p in args.snapshots.iterdir() if p.is_dir() and not p.is_symlink()),
        key=lambda p: (p.stat().st_mtime_ns, p.name),
        reverse=True,
    )
    if not args.keep_archive and archive.is_dir():
        for path in sorted(archive.rglob("*")):
            if path.is_file() and not path.is_symlink():
                try:
                    key = content_key(path)
                except OSError:
                    continue
                known_content.setdefault(key, path)
                global_queue.append(path)
        name_index.add_tree(archive)
    for d in dirs:
        name_index.add_tree(d)

    removable, retained = prunable_snapshots(
        args.source, args.snapshots, max(1, args.keep))
    removed = 0
    for path in removable:
        print(f"snapshot prune remove {path.name} (file-level history check)")
        if not args.dry_run:
            try:
                resolved = path.resolve(strict=True)
                parent = args.snapshots.resolve(strict=True)
                if resolved.parent != parent:
                    print(f"snapshot prune keep {path.name} (path escaped snapshots root)")
                    continue
                index = next(i for i, d in enumerate(dirs) if d == path)
                ok, reason = prune_snapshot_files(
                    path, [args.source, *dirs[:index]], name_index,
                    archive, known_content, global_queue,
                    archive_bytes, archive_cap)
                if ok:
                    shutil.rmtree(path)
                    removed += 1
                else:
                    print(f"snapshot prune keep {path.name} (unprotected: {reason})")
            except OSError as exc:
                print(f"snapshot prune keep {path.name} ({exc})")
    for path, reason in retained:
        print(f"snapshot prune inspect {path.name} (metadata differs: {reason})")
        if not args.dry_run:
            try:
                resolved = path.resolve(strict=True)
                parent = args.snapshots.resolve(strict=True)
                if resolved.parent != parent:
                    continue
                index = next(i for i, d in enumerate(dirs) if d == path)
                ok, why = prune_snapshot_files(
                    path, [args.source, *dirs[:index]], name_index,
                    archive, known_content, global_queue,
                    archive_bytes, archive_cap)
                if ok:
                    shutil.rmtree(path)
                    removed += 1
                else:
                    print(f"snapshot prune keep {path.name} (unprotected: {why})")
            except OSError as exc:
                print(f"snapshot prune keep {path.name} ({exc})")
    print(
        f"snapshot prune summary: removed={removed} "
        f"candidates={len(removable) + len(retained)} "
        f"archive={archive} archive_files={len(global_queue)} "
        f"archive_bytes={archive_bytes[0]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
