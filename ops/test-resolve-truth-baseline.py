#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：旧 `resolve.json` 快照不能撤回 HEAD 已落盘的裁决。

2026-11-25 实测：共享检出工作副本把 4 条 landed/abandoned 退回 needs_agent，
同时新增 2 条裁决。`resolve.json` 是普通 JSON，既没走 JSONL 的行级并集，也没走
按标题的追加型 Markdown 并集；`sync_trunk_truth` 暂存前直接 add，会把终态删掉。
`_restore_truth_files` 又按顶层 `updated` 选一侧，旧快照时间戳更新时同样会赢。

本门禁用真实 Git 夹具锁住两个入口：
  * 真相同步前与 HEAD 做任务级并集，已裁决终态不能倒退，工作副本新记录要保留；
  * 切主干前恢复快照时也按任务终态并集，不让旧快照吞掉已落盘裁决。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
KGCTL_PATH = OPS / "kgctl.py"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} 失败\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    return proc


def task(slug: str, status: str, updated: str) -> dict:
    return {
        "slug": slug,
        "branch": f"codex/{slug}",
        "status": status,
        "created": updated,
        "updated": updated,
        "conflict_files": ["ops/kgctl.py"],
        "unresolved_files": ["ops/kgctl.py"],
        "auto_resolved_jsonl": [],
    }


def resolve_doc(tasks: list, updated: str) -> str:
    return json.dumps({"tasks": tasks, "updated": updated},
                      ensure_ascii=False, indent=1) + "\n"


def seed_repo(path: Path, tasks: list, updated: str) -> Path:
    (path / "ops" / "tasks").mkdir(parents=True)
    (path / "ops" / "truth").mkdir(parents=True)
    (path / "docs").mkdir(parents=True)
    (path / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
    (path / "ops" / "tasks" / "resolve.json").write_text(
        resolve_doc(tasks, updated), encoding="utf-8")
    for name in ("ledger.jsonl", "progress.jsonl", "acceptance.jsonl",
                 "decisions.jsonl", "constraints.jsonl"):
        (path / "ops" / "truth" / name).write_text("", encoding="utf-8")
    (path / "ops" / "truth" / "mainline.json").write_text("{}\n", encoding="utf-8")
    (path / "docs" / "PROJECT_STATUS.md").write_text("# status\n", encoding="utf-8")
    (path / "ops" / "central.config.json").write_text(
        '{"main_branch": "main", "branch_prefix": "codex/", '
        '"build": {"cmd": "", "cwd": "."}, "verify": []}\n',
        encoding="utf-8")
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Resolve Baseline Test")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def load_kgctl(repo: Path, truth_ids: Path):
    keys = ("KG_REPO", "KG_TRUNK", "KG_MODEL_PROBE",
            "KG_BRANCH_PREFIX", "KG_TRUTH_ID_STATE_ROOT")
    saved = {key: os.environ.get(key) for key in keys}
    os.environ["KG_REPO"] = str(repo)
    os.environ["KG_TRUNK"] = str(repo)
    os.environ["KG_MODEL_PROBE"] = "off"
    os.environ["KG_BRANCH_PREFIX"] = "codex/"
    os.environ["KG_TRUTH_ID_STATE_ROOT"] = str(truth_ids)
    try:
        spec = importlib.util.spec_from_file_location(
            "kgctl_resolve_baseline_test", KGCTL_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def states(doc: dict) -> dict:
    return {t.get("slug"): t.get("status") for t in doc.get("tasks", [])}


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="kg-resolve-truth-baseline-") as tmp:
        root = Path(tmp)
        repo = seed_repo(
            root / "state",
            [
                task("settled-landed", "landed", "2026-11-25 22:00:00"),
                task("settled-abandoned-b", "abandoned", "2026-11-25 22:00:01"),
                task("settled-abandoned-c", "abandoned", "2026-11-25 22:00:02"),
                task("settled-abandoned-d", "abandoned", "2026-11-25 22:00:03"),
            ],
            "2026-11-25 22:00:03",
        )
        kg = load_kgctl(repo, root / "truth-ids")
        # /var 与 /private/var 是同一目录的两种路径，夹具只要求指向同一现场。
        assert kg.REPO.resolve() == repo.resolve(), (kg.REPO, repo)

        # 工作副本是旧快照：3 条终态倒退成 needs_agent，同时新增 2 条裁决。
        stale = [
            task("settled-landed", "landed", "2026-11-25 21:00:00"),
            task("settled-abandoned-b", "needs_agent", "2026-11-25 23:00:00"),
            task("settled-abandoned-c", "needs_agent", "2026-11-25 23:00:01"),
            task("settled-abandoned-d", "needs_agent", "2026-11-25 23:00:02"),
            task("new-abandoned-e", "abandoned", "2026-11-25 22:32:47"),
            task("new-abandoned-f", "abandoned", "2026-11-25 22:32:48"),
        ]
        stale_text = resolve_doc(stale, "2026-11-25 23:00:02")
        (repo / "ops" / "tasks" / "resolve.json").write_text(
            stale_text, encoding="utf-8")

        # 入口一：真相同步暂存前必须按任务终态并集，不能 add 旧快照。
        assert kg.sync_trunk_truth(repo, "sync from resolve baseline test") is True
        committed = states(json.loads(git(repo, "show",
                                          "HEAD:ops/tasks/resolve.json").stdout))
        working = states(json.loads(
            (repo / "ops" / "tasks" / "resolve.json").read_text(encoding="utf-8")))
        expected = {
            "settled-landed": "landed",
            "settled-abandoned-b": "abandoned",
            "settled-abandoned-c": "abandoned",
            "settled-abandoned-d": "abandoned",
            "new-abandoned-e": "abandoned",
            "new-abandoned-f": "abandoned",
        }
        assert committed == expected, committed
        assert working == expected, working

        # 入口二：land 切主干前的快照恢复也不能让旧时间戳快照赢。
        current = (repo / "ops" / "tasks" / "resolve.json").read_bytes()
        older_snapshot = resolve_doc(
            [
                task("settled-landed", "landed", "2026-11-25 21:00:00"),
                task("settled-abandoned-b", "needs_agent", "2026-11-25 23:30:00"),
                task("settled-abandoned-c", "needs_agent", "2026-11-25 23:30:01"),
                task("settled-abandoned-d", "needs_agent", "2026-11-25 23:30:02"),
            ],
            "2026-11-25 23:30:02",
        ).encode("utf-8")
        kg._restore_truth_files(repo, {"ops/tasks/resolve.json": older_snapshot})
        restored = states(json.loads(
            (repo / "ops" / "tasks" / "resolve.json").read_text(encoding="utf-8")))
        assert restored == expected, restored
        assert (repo / "ops" / "tasks" / "resolve.json").read_bytes() != current

    print("test-resolve-truth-baseline: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
