#!/usr/bin/env python3
"""Regression: build-only land evidence must not make land fail or claim verified."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KGCTL_PATH = ROOT / "ops" / "kgctl.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


with tempfile.TemporaryDirectory(prefix="kg-land-build-only-") as tmp:
    repo = Path(tmp) / "state"
    (repo / "ops" / "tasks").mkdir(parents=True)
    (repo / "ops" / "truth").mkdir(parents=True)
    (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
    for name in ("ledger.jsonl", "progress.jsonl", "acceptance.jsonl"):
        (repo / "ops" / "truth" / name).write_text("", encoding="utf-8")
    (repo / "ops" / "truth" / "mainline.json").write_text("{}\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Land Test")
    (repo / "changed.txt").write_text("changed\n", encoding="utf-8")
    _git(repo, "add", "changed.txt")
    _git(repo, "commit", "-q", "-m", "fixture")

    old_repo = os.environ.get("KG_REPO")
    old_trunk = os.environ.get("KG_TRUNK")
    os.environ["KG_REPO"] = str(repo)
    os.environ["KG_TRUNK"] = str(repo)
    try:
        spec = importlib.util.spec_from_file_location("kgctl_land_test", KGCTL_PATH)
        assert spec and spec.loader
        kgctl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(kgctl)
        kgctl._truthctl_path = lambda: ROOT / "ops" / "truthctl.py"
        kgctl._roadmap_touch = lambda *_args: ""

        manifest = {
            "changed_files": [{"path": "changed.txt", "added": "1", "deleted": "0"}],
            "total_stat": "1 file changed, 1 insertion(+)",
            "acceptance": [],
        }
        build_only = [{"name": "build", "cmd": "npm run build", "cwd": ".", "ok": True}]
        assert kgctl._build_only_evidence(build_only)
        kgctl._record_land_progress(repo, "codex/build-only", "abc123", build_only, manifest)
        acc = _rows(repo / "ops" / "truth" / "acceptance.jsonl")[-1]
        assert acc["status"] == "todo", acc
        assert "npm run build" in acc["evidence"]

        behavior = [{
            "name": "behavior",
            "cmd": "node test/user-visible-behavior.mjs",
            "cwd": ".",
            "ok": True,
        }]
        assert not kgctl._build_only_evidence(behavior)
        kgctl._record_land_progress(repo, "codex/behavior", "def456", behavior, manifest)
        acc = _rows(repo / "ops" / "truth" / "acceptance.jsonl")[-1]
        assert acc["status"] == "verified", acc
    finally:
        if old_repo is None:
            os.environ.pop("KG_REPO", None)
        else:
            os.environ["KG_REPO"] = old_repo
        if old_trunk is None:
            os.environ.pop("KG_TRUNK", None)
        else:
            os.environ["KG_TRUNK"] = old_trunk

print("test-land-build-only-todo: ok")
