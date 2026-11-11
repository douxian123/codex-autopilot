#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression: snapshot pruning may never remove unique Codex history."""
import importlib.util
import os
import tempfile
import time
from pathlib import Path

OPS = Path(__file__).resolve().parent
HELPER = OPS / "guards/snapshot_prune.py"
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


def load_helper():
    spec = importlib.util.spec_from_file_location("snapshot_prune_test", HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def main():
    mod = load_helper()
    with tempfile.TemporaryDirectory(prefix="snapshot-prune-") as tmp:
        root = Path(tmp)
        source = root / "sessions"
        snapshots = root / "snapshots"
        old = snapshots / "20260101-000000"
        newer = snapshots / "20260102-000000"
        source.mkdir()
        write(newer / "append.jsonl", b'{"n":1}\n{"n":2}\n')
        write(old / "append.jsonl", b'{"n":1}\n')
        now_ns = time.time_ns()
        os.utime(old, ns=(now_ns - 10_000_000_000, now_ns - 10_000_000_000))
        os.utime(newer, ns=(now_ns, now_ns))
        removable, retained = mod.prunable_snapshots(source, snapshots, keep=1)
        check("追加日志的旧前缀可以安全淘汰",
              removable == [old] and not retained, (removable, retained))
        now_ns = time.time_ns()
        os.utime(old, ns=(now_ns - 10_000_000_000, now_ns - 10_000_000_000))
        os.utime(newer, ns=(now_ns, now_ns))

        write(old / "deleted.jsonl", b'{"only":"in old snapshot"}\n')
        os.utime(old, ns=(now_ns - 10_000_000_000, now_ns - 10_000_000_000))
        os.utime(newer, ns=(now_ns, now_ns))
        removable, retained = mod.prunable_snapshots(source, snapshots, keep=1)
        check("新来源已删除的文件会保护整份快照",
              not removable and retained and retained[0][0] == old,
              (removable, retained))

        write(source / "deleted.jsonl", b'{"rewritten":"not the same history"}\n')
        os.utime(old, ns=(now_ns - 10_000_000_000, now_ns - 10_000_000_000))
        os.utime(newer, ns=(now_ns, now_ns))
        removable, retained = mod.prunable_snapshots(source, snapshots, keep=1)
        check("同路径但内容改写不被误判为已覆盖",
              not removable and retained and retained[0][0] == old,
              (removable, retained))

        write(source / "deleted.jsonl", b'{"only":"in old snapshot"}\n{"new":true}\n')
        os.utime(old, ns=(now_ns - 10_000_000_000, now_ns - 10_000_000_000))
        os.utime(newer, ns=(now_ns, now_ns))
        removable, retained = mod.prunable_snapshots(source, snapshots, keep=1)
        check("当前文件包含旧内容完整前缀时允许淘汰",
              removable == [old] and not retained, (removable, retained))
        now_ns = time.time_ns()
        os.utime(old, ns=(now_ns - 10_000_000_000, now_ns - 10_000_000_000))
        os.utime(newer, ns=(now_ns, now_ns))

        os_link = old / "rewritten.jsonl"
        if os_link.exists() or os_link.is_symlink():
            os_link.unlink()
        os_link.symlink_to("target-a")
        newer_link = newer / "rewritten.jsonl"
        newer_link.symlink_to("target-b")
        removable, retained = mod.prunable_snapshots(source, snapshots, keep=1)
        check("symlink 目标改变时保留旧快照",
              not removable and retained and retained[0][0] == old,
              (removable, retained))

    with tempfile.TemporaryDirectory(prefix="snapshot-prune-mtime-") as tmp:
        root = Path(tmp)
        source = root / "sessions"
        snapshots = root / "snapshots"
        older_name = snapshots / "99999999-late-name"
        newer_mtime = snapshots / "00000000-old-name"
        source.mkdir()
        write(older_name / "same.jsonl", b"old\n")
        write(newer_mtime / "same.jsonl", b"old\nnew\n")
        old_ns = time.time_ns() - 10_000_000_000
        new_ns = time.time_ns()
        os.utime(older_name, ns=(old_ns, old_ns))
        os.utime(newer_mtime, ns=(new_ns, new_ns))
        removable, retained = mod.prunable_snapshots(source, snapshots, keep=1)
        check("保留窗口按修改时间而不是目录名",
              removable == [older_name] and not retained, (removable, retained))

    with tempfile.TemporaryDirectory(prefix="snapshot-prune-archive-") as tmp:
        root = Path(tmp)
        archive = root / "archive"
        original = root / "snapshot/unique.jsonl"
        write(original, b"unique history\n")
        known = {}
        queued = []
        usage = [0]
        archived, reason = mod.archive_unique(
            original, archive, known, queued, usage, len(b"unique history\n"))
        archived_paths = [
            path for path in archive.rglob("*")
            if path.is_file() and not path.name.startswith(".incoming-")
        ]
        check("独有内容通过原子搬到归档而不是删除",
              archived and not reason and not original.exists()
              and len(archived_paths) == 1
              and archived_paths[0].read_bytes() == b"unique history\n"
              and usage[0] == len(b"unique history\n"),
              (archived, reason, archived_paths, usage))
        check("归档搬移不遗留 staging 文件",
              not list(archive.rglob(".incoming-*")), list(archive.rglob(".incoming-*")))

        original = root / "snapshot/cap.jsonl"
        write(original, b"must preserve\n")
        usage = [len(b"unique history\n")]
        archived, reason = mod.archive_unique(
            original, archive, known, queued, usage, usage[0] + 4)
        archived_paths = [
            path for path in archive.rglob("*")
            if path.is_file() and not path.name.startswith(".incoming-")
        ]
        check("归档容量不足时停止且不删除原快照文件",
              not archived and reason == "archive capacity reached"
              and original.read_bytes() == b"must preserve\n"
              and len(archived_paths) == 1,
              (archived, reason, original.exists(), archived_paths))
        check("不存在为满足容量上限删除旧归档的路径",
              not hasattr(mod, "enforce_archive_cap"))

    snapshot_sh = (OPS / "guards/snapshot.sh").read_text(encoding="utf-8")
    check("快照使用 APFS clone 而不是会跟随追加写入的硬链接",
          "cp -cR" in snapshot_sh and "cp -al" not in snapshot_sh)
    check("shell 不再按时间直接 rm -rf 超量快照",
          "tail -n +" not in snapshot_sh and "snapshot_prune.py" in snapshot_sh)
    check("快照低盘准入线固定为 20GiB",
          'MIN_FREE_GIB="${MIN_FREE_GIB:-20}"' in snapshot_sh)
    check("快照保留窗口固定为 3 份",
          "KEEP=3" in snapshot_sh)
    check("快照先写 partial 再原子改名",
          'PARTIAL="$SNAP/.${TS}.partial.$$"' in snapshot_sh
          and 'mv "$PARTIAL" "$FINAL"' in snapshot_sh)

    if failures:
        print(f"\nsnapshot clone/prune unit: FAIL ({len(failures)})")
        raise SystemExit(1)
    print("\nsnapshot clone/prune unit: PASS")


if __name__ == "__main__":
    main()
