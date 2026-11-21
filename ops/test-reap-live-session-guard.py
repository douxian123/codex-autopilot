#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression: the reaper must not delete a worktree something is working in.

Why this file exists (measured 2026-11-10):
  The hourly reaper decided a worktree was reclaimable from git state alone --
  branch merged into main, `git status` clean -- plus a directory-mtime idle
  gate. On 2026-11-10 the disk sat below 12GB, which switches the idle gate to
  15 minutes, and the reaper deleted 18 worktrees that a desktop session was
  still working in. They survived only because the branches were kept.

  Two blind spots made "in use" read exactly like "abandoned":
    * `worktree_removable(..., commands="")` switched the command-line scan off;
    * a desktop session's cwd is the worktree, but that never shows up as
      `codex exec -C <tree>`, and read-only analysis never bumps directory mtime.

  The dangerous direction is deletion, so the assertions are weighted towards
  "must NOT reclaim". The opposite risk -- pinning every tree forever -- is
  covered by the last case: a genuinely idle, clean worktree must still be
  reclaimable with the guard on.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))
import live_guard  # noqa: E402  (canonical instance, shared with workspace_guard)
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, OPS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def spawn_sleep(cwd: Path, seconds: int = 60) -> subprocess.Popen:
    return subprocess.Popen(["/bin/sleep", str(seconds)], cwd=str(cwd))


def write_transcript(path: Path, cwd: str) -> None:
    """One rollout line whose recorded cwd is `cwd`, as Codex writes it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"type": "session_meta", "payload": {"cwd": cwd,
                                               "runtime_workspace_roots": [cwd]}}
    path.write_text(json.dumps(rec) + "\n")


def git(repo: Path, *args) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def make_repo(base: Path) -> Path:
    repo = base / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("hello\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-qm", "init")
    git(repo, "branch", "-M", "main")
    return repo


def main():
    lg = live_guard
    wg = load("wg_under_test", "workspace_guard.py")
    assert wg._live_guard is lg, "workspace_guard must share the live_guard instance"
    base = Path(tempfile.mkdtemp(prefix="reap-live-guard-"))
    procs = []

    # Session evidence must come from a sandbox root, never the real machine.
    session_root = base / "sessions"
    session_root.mkdir()
    os.environ["KG_SESSION_ROOTS"] = str(session_root)
    os.environ["KG_LIVE_SESSION_MAX_AGE_S"] = "10800"

    try:
        # 1. A live process whose cwd is the tree must veto reclamation.
        live_tree = base / "kg-live-tree"
        live_tree.mkdir()
        procs.append(spawn_sleep(live_tree))
        time.sleep(0.3)
        lg.reset_caches()
        check("在用的树命中了活进程 cwd",
              lg.live_processes_in(live_tree), lg.live_processes_in(live_tree))
        check("live_guard_blocker 报出 cwd 证据",
              lg.live_blocker(live_tree).startswith("cwd: pid"),
              lg.live_blocker(live_tree))

        # 2. Prefix sibling must NOT be matched: /x/kg-a must not protect
        #    /x/kg-a-extra, and vice versa.
        sibling = base / "kg-live-tree-extra"
        sibling.mkdir()
        procs.append(spawn_sleep(sibling))
        time.sleep(0.3)
        lg.reset_caches()
        found = lg.live_processes_in(live_tree)
        check("同前缀兄弟目录的进程不被误算进这棵树",
              str(procs[1].pid) not in found, found)
        check("这棵树自己的进程仍能被认出",
              str(procs[0].pid) in found, found)
        check("兄弟目录自己的进程能被认出",
              str(procs[1].pid) in lg.live_processes_in(sibling),
              lg.live_processes_in(sibling))

        # 3. A quiet session whose recorded cwd is the tree: no process holds
        #    the tree, yet it is still in use. Only the "all" mode sees it.
        quiet = base / "kg-quiet-session"
        quiet.mkdir()
        write_transcript(session_root / "2026" / "11" / "10" / "rollout-x.jsonl",
                         str(quiet))
        lg.reset_caches()
        # The reaper calls the mode-aware gate; "cwd" must ignore a transcript.
        check("安静的会话在 cwd 模式下看不到",
              wg.live_guard_blocker(quiet, "cwd") == "",
              wg.live_guard_blocker(quiet, "cwd"))
        lg.reset_caches()
        check("安静的会话在 all 模式下被认作在用",
              wg.live_guard_blocker(quiet, "all").startswith("live-session:"),
              wg.live_guard_blocker(quiet, "all"))

        # 4. Fail-open: when both probes are unavailable the verdict is
        #    "no evidence" -- not a veto, and not a delete licence either.
        probe_broken = base / "kg-probe-broken"
        probe_broken.mkdir()
        orig_pids, orig_sess = lg.live_processes_in, lg.sessions_working_in
        lg.live_processes_in = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom"))
        lg.sessions_working_in = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom"))
        lg.reset_caches()
        check("两个探针都不可用时返回空证据",
              wg.live_guard_blocker(probe_broken, "all") == "",
              wg.live_guard_blocker(probe_broken, "all"))
        lg.live_processes_in, lg.sessions_working_in = orig_pids, orig_sess

        # 5. End to end through the guard: a clean, idle worktree still gets
        #    reclaimed even with the strongest guard mode on. This is the
        #    assertion that stops the veto from pinning every tree forever.
        repo = make_repo(base)
        idle = base / "kg-idle-clean"
        git(repo, "worktree", "add", "-q", "-b", "codex/idle", str(idle), "main")
        os.environ["KG_SESSION_ROOTS"] = str(session_root)
        lg.reset_caches()
        assessment = wg.worktree_removable(repo, idle, "codex/idle", "main",
                                           commands="", live_guard_mode="all")
        check("闲置且干净的真死树仍可回收",
              assessment.get("ok") is True, assessment)
        check("闲置死树没有被判成 live",
              "live" not in str(assessment.get("reason")), assessment)

        # 6. Same guard, but now a live process sits in it -> refused.
        procs.append(spawn_sleep(idle))
        time.sleep(0.3)
        lg.reset_caches()
        blocked = wg.worktree_removable(repo, idle, "codex/idle", "main",
                                        commands="", live_guard_mode="all")
        check("同一棵树有活进程后回收被拒",
              blocked.get("ok") is False and "live" in str(blocked.get("reason")),
              blocked)
    finally:
        for proc in procs:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

    if failures:
        print("\n失败项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\nreap live-session guard unit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
