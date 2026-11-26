#!/usr/bin/env python3
"""Regression: auto-close must reject reused ids and mismatched evidence."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


OPS = Path(__file__).resolve().parent
TRUTHCTL = OPS / "truthctl.py"
DAEMON = OPS / "central_daemon.py"


def run(cmd, cwd, env=None, check=True):
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(
            f"command failed: {' '.join(map(str, cmd))}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc


def git(repo, *args):
    return run(["git", "-C", str(repo), *args], cwd=repo)


def seed_repo(repo: Path) -> None:
    (repo / "ops" / "truth").mkdir(parents=True)
    (repo / "ops" / "tasks").mkdir()
    (repo / "docs").mkdir()
    (repo / "ops" / "kgctl.py").write_text("", encoding="utf-8")
    (repo / "ops" / "truth" / "ledger.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "truth" / "progress.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "truth" / "decisions.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "truth" / "acceptance.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "truth" / "mainline.json").write_text("{}\n", encoding="utf-8")
    (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "tasks" / "lanes.json").write_text("[]\n", encoding="utf-8")
    (repo / "ops" / "central.config.json").write_text(
        '{"build": {"cmd": "", "cwd": "."}, "verify": []}\n', encoding="utf-8")
    (repo / "docs" / "PROJECT_STATUS.md").write_text("# status\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "identity@example.invalid")
    git(repo, "config", "user.name", "Identity Test")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "app.txt", "ops", "docs")
    git(repo, "commit", "-q", "-m", "base")


def write_rows(repo: Path, rows: list[dict]) -> None:
    path = repo / "ops" / "truth" / "ledger.jsonl"
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def ledger(repo: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (repo / "ops" / "truth" / "ledger.jsonl").read_text().splitlines()
        if line.strip()
    ]


def main() -> None:
    daemon_spec = importlib.util.spec_from_file_location(
        "central_daemon_identity_test", DAEMON
    )
    assert daemon_spec and daemon_spec.loader
    daemon = importlib.util.module_from_spec(daemon_spec)
    daemon_spec.loader.exec_module(daemon)
    old_repo = os.environ.get("KG_REPO")

    def use(repo: Path) -> None:
        os.environ["KG_REPO"] = str(repo)

    def restore() -> None:
        if old_repo is None:
            os.environ.pop("KG_REPO", None)
        else:
            os.environ["KG_REPO"] = old_repo

    try:
        with tempfile.TemporaryDirectory(prefix="kg-identity-collision-") as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            seed_repo(repo)
            (repo / "landed.txt").write_text("old task\n", encoding="utf-8")
            git(repo, "add", "landed.txt")
            git(repo, "commit", "-q", "-m", "old task landed")
            landed_sha = git(repo, "rev-parse", "HEAD").stdout.strip()
            write_rows(repo, [
                {"id": "L-9001", "ts": "2026-10-24 16:00:00", "type": "ledger",
                 "kind": "gate", "title": "fix local-history-relay default port",
                 "status": "done", "commit": landed_sha},
                {"id": "L-9001", "ts": "2026-10-24 17:00:00", "type": "ledger",
                 "kind": "arch", "title": "website naming draft", "status": "open"},
            ])
            use(repo)
            identity = json.loads(run(
                [sys.executable, str(TRUTHCTL), "identity", "L-9001"],
                cwd=repo, env=os.environ.copy(),
            ).stdout)
            assert identity["collision"] is True, identity

            blocked = {"L-9001": {"reason": "land_error", "commit": landed_sha,
                                  "ts": "2026-10-24 17:05:00"}}
            assert daemon._prune_exec_blocked(blocked) == []
            assert blocked["L-9001"]["reason"] == "identity_collision", blocked
            assert ledger(repo)[-1]["status"] == "open", ledger(repo)

            repo2 = Path(tmp) / "repo2"
            repo2.mkdir()
            seed_repo(repo2)
            (repo2 / "landed.txt").write_text("old task\n", encoding="utf-8")
            git(repo2, "add", "landed.txt")
            git(repo2, "commit", "-q", "-m", "old task landed")
            other_sha = git(repo2, "rev-parse", "HEAD").stdout.strip()
            write_rows(repo2, [
                {"id": "L-9002", "ts": "2026-10-24 17:00:00", "type": "ledger",
                 "kind": "arch", "title": "website naming draft", "status": "open",
                 "files": ["docs/PRODUCT_MAINLINE.md"]},
            ])
            blocked2 = {"L-9002": {"reason": "land_error", "commit": other_sha,
                                   "ts": "2026-10-24 17:05:00"}}
            use(repo2)
            assert daemon._prune_exec_blocked(blocked2) == []
            assert blocked2["L-9002"]["reason"] == "evidence_mismatch", blocked2
            assert ledger(repo2)[-1]["status"] == "open", ledger(repo2)

            repo3 = Path(tmp) / "repo3"
            repo3.mkdir()
            seed_repo(repo3)
            (repo3 / "docs" / "PRODUCT_MAINLINE.md").write_text("draft\n", encoding="utf-8")
            git(repo3, "add", "docs/PRODUCT_MAINLINE.md")
            git(repo3, "commit", "-q", "-m", "naming draft landed")
            good_sha = git(repo3, "rev-parse", "HEAD").stdout.strip()
            write_rows(repo3, [
                {"id": "L-9003", "ts": "2026-10-24 18:00:00", "type": "ledger",
                 "kind": "arch", "title": "website naming draft", "status": "open",
                 "files": ["docs/PRODUCT_MAINLINE.md"]},
            ])
            blocked3 = {"L-9003": {"reason": "land_error", "commit": good_sha,
                                   "ts": "2026-10-24 18:05:00"}}
            use(repo3)
            assert daemon._prune_exec_blocked(blocked3) == ["L-9003"], blocked3
            assert ledger(repo3)[-1]["status"] == "done", ledger(repo3)
    finally:
        restore()

    print("ledger identity collision + evidence mismatch + valid close: PASS")


if __name__ == "__main__":
    main()
