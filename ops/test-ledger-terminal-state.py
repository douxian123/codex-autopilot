#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression: an older snapshot appended after done must not revive an item."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


OPS = Path(__file__).resolve().parent


def append(path, record):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def main():
    with tempfile.TemporaryDirectory(prefix="autopilot-ledger-terminal-") as temp:
        repo = Path(temp) / "state"
        truth = repo / "ops" / "truth"
        tasks = repo / "ops" / "tasks"
        truth.mkdir(parents=True)
        tasks.mkdir(parents=True)
        (tasks / "inbox.jsonl").write_text("", encoding="utf-8")
        (tasks / "resolve.json").write_text('{"tasks": []}\n', encoding="utf-8")
        (truth / "progress.jsonl").write_text("", encoding="utf-8")
        (truth / "decisions.jsonl").write_text("", encoding="utf-8")
        (truth / "acceptance.jsonl").write_text("", encoding="utf-8")
        (truth / "mainline.json").write_text("{}\n", encoding="utf-8")
        (repo / "ops" / "central.config.json").write_text(
            '{"build": {"cmd": "", "cwd": "."}, "verify": []}\n',
            encoding="utf-8",
        )

        ledger = truth / "ledger.jsonl"
        append(ledger, {
            "id": "L-0089", "type": "ledger", "kind": "bug", "status": "open",
            "title": "old snapshot", "ts": "2026-10-16 14:20:43",
        })
        append(ledger, {
            "id": "L-0089", "type": "ledger", "kind": "bug", "status": "done",
            "title": "closed", "ts": "2026-10-24 05:24:12",
            "closed": "2026-10-24 05:24:12",
        })
        append(ledger, {
            "id": "L-0089", "type": "ledger", "kind": "bug", "status": "open",
            "title": "stale snapshot appended later", "ts": "2026-10-16 14:20:43",
        })
        append(ledger, {
            "id": "L-LIVE", "type": "ledger", "kind": "bug", "status": "open",
            "title": "genuinely open", "ts": "2026-10-25 01:00:00",
        })

        os.environ["KG_REPO"] = str(repo)
        os.environ["KG_TRUNK"] = str(repo)
        os.environ["KG_DAEMON_HOME"] = str(Path(temp) / "daemon-home")
        sys.path.insert(0, str(OPS))
        import central_daemon
        import central_mcp

        latest = central_daemon._ledger_latest()
        assert latest["L-0089"]["status"] == "done", latest
        ledger_open_ids = {row["id"] for row in central_daemon._ledger_open_items()}
        assert ledger_open_ids == {"L-LIVE"}, ledger_open_ids

        # Keep exec dispatch focused on the ledger; resolve worktrees are covered
        # by their own suites and this fixture deliberately has none.
        real_resolve_open = central_daemon._resolve_open_items
        real_resolve_ready = central_daemon._resolve_ready_items
        central_daemon._resolve_open_items = lambda limit=20: iter(())
        central_daemon._resolve_ready_items = lambda limit=20: iter(())
        try:
            exec_open_ids = {row["id"] for row in central_daemon._exec_open_items()}
        finally:
            central_daemon._resolve_open_items = real_resolve_open
            central_daemon._resolve_ready_items = real_resolve_ready
        assert exec_open_ids == {"L-LIVE"}, exec_open_ids
        assert central_mcp._open_ledger_count() == 1

        env = os.environ.copy()
        next_result = subprocess.run(
            [sys.executable, str(OPS / "truthctl.py"), "next"],
            cwd=str(OPS.parent), env=env, capture_output=True, text=True,
        )
        assert next_result.returncode == 0, next_result.stderr
        assert "L-0089" not in next_result.stdout, next_result.stdout
        assert "L-LIVE" in next_result.stdout, next_result.stdout

        import kgctl
        dash_rows = kgctl.latest_ledger_by_id(
            kgctl.read_jsonl(repo / "ops" / "truth" / "ledger.jsonl"))
        assert dash_rows["L-0089"]["status"] == "done", dash_rows["L-0089"]

        append(ledger, {
            "id": "L-0089", "type": "ledger", "kind": "bug", "status": "open",
            "title": "explicit reopen", "ts": "2026-10-26 01:00:00",
        })
        reopened = central_daemon._ledger_latest()["L-0089"]
        assert reopened["status"] == "open", reopened
        reopened_ids = {row["id"] for row in central_daemon._ledger_open_items()}
        assert reopened_ids == {"L-0089", "L-LIVE"}, reopened_ids
        assert central_mcp._open_ledger_count() == 2

        next_reopened = subprocess.run(
            [sys.executable, str(OPS / "truthctl.py"), "next"],
            cwd=str(OPS.parent), env=env, capture_output=True, text=True,
        )
        assert next_reopened.returncode == 0, next_reopened.stderr
        assert "L-0089" in next_reopened.stdout, next_reopened.stdout

    print("PASS ledger terminal state: stale open snapshot ignored; newer reopen honored")


if __name__ == "__main__":
    main()
