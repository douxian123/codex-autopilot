#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression: snapshot pruning may never remove unique Codex history."""
import importlib.util
import tempfile
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
        removable, retained = mod.prunable_snapshots(source, snapshots, keep=1)
        check("追加日志的旧前缀可以安全淘汰",
              removable == [old] and not retained, (removable, retained))

        write(old / "deleted.jsonl", b'{"only":"in old snapshot"}\n')
        removable, retained = mod.prunable_snapshots(source, snapshots, keep=1)
        check("新来源已删除的文件会保护整份快照",
              not removable and retained and retained[0][0] == old,
              (removable, retained))

        write(source / "deleted.jsonl", b'{"rewritten":"not the same history"}\n')
        removable, retained = mod.prunable_snapshots(source, snapshots, keep=1)
        check("同路径但内容改写不被误判为已覆盖",
              not removable and retained and retained[0][0] == old,
              (removable, retained))

        write(source / "deleted.jsonl", b'{"only":"in old snapshot"}\n{"new":true}\n')
        removable, retained = mod.prunable_snapshots(source, snapshots, keep=1)
        check("当前文件包含旧内容完整前缀时允许淘汰",
              removable == [old] and not retained, (removable, retained))

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

    snapshot_sh = (OPS / "guards/snapshot.sh").read_text(encoding="utf-8")
    check("快照使用 APFS clone 而不是会跟随追加写入的硬链接",
          "cp -cR" in snapshot_sh and "cp -al" not in snapshot_sh)
    check("shell 不再按时间直接 rm -rf 超量快照",
          "tail -n +" not in snapshot_sh and "snapshot_prune.py" in snapshot_sh)

    if failures:
        print(f"\nsnapshot clone/prune unit: FAIL ({len(failures)})")
        raise SystemExit(1)
    print("\nsnapshot clone/prune unit: PASS")


if __name__ == "__main__":
    main()
