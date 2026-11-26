#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：刚建好的 worktree 在「创建宽限期」内不得被回收。

为什么有这条（用户实测 2026-11-11）：
  磁盘长期低于 REAP_PRESSURE_FREE_GB=12GB，回收器就一直处于压力模式，闲置门槛
  只有 REAP_IDLE_PRESSURE_H=0.25（15 分钟）。一个新 worktree 刚建好、人还没写字，
  它天然满足「分支 ahead=0 + 工作区干净」，会话稍一离开就会落进回收视角 ——
  用户感知到的就是「做着做着工作目录被清空」。

  判活（live_guard）能挡住「有会话/进程正在用」的树，但它挡不住「刚建好、还没被
  任何东西碰过、也没被任何 transcript 记录到」的空档。创建宽限期补的就是这一段：
  只看目录创建时间，不放松任何安全判据 —— 它只会让回收器少删，不会多删。

  反向断言同样重要：一旦超过宽限期，同样的树仍必须可回收，否则这条加固就变成了
  「把每棵新树永久钉住」，磁盘照样爆。
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

OPS = Path(__file__).resolve().parent
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


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True)


def make_repo(base: Path) -> Path:
    """一个 main 分支 + 它的工作树，够回收器认出「这是本仓的 worktree」。"""
    repo = base / "kg-main"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("x\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "remote", "add", "origin", str(repo))
    return repo


def add_worktree(repo: Path, path: Path, branch: str) -> None:
    """建一棵与 main 同内容（ahead=0）、干净的工作树。"""
    git(repo, "worktree", "add", "-b", branch, str(path), "main")


def main():
    wg = load("wg_grace", "workspace_guard.py")
    cd = load("cd_grace", "central_daemon.py")

    base = Path(tempfile.mkdtemp(prefix="reap-grace-"))
    # 隔离运行时，别碰真实 ~/.codex-autopilot
    runtime = base / "runtime"
    runtime.mkdir()
    cd.RUNTIME = runtime
    cd.REAP_STATE = runtime / "reap_state.json"
    cd.LOG = runtime / "daemon.log"
    os.environ["KG_DAEMON_HOME"] = str(runtime)
    os.environ["KG_WORKSPACE_ROOT"] = str(base)
    # 打开回收 + 强制压力模式（磁盘阈值拉到天上，必进压力分支）
    os.environ["KG_REAP_WORKTREES"] = "1"
    os.environ["KG_REAP_PRESSURE_FREE_GB"] = "100000"
    os.environ["KG_REAP_CREATION_GRACE_H"] = "1"
    cd.REAP_PRESSURE_FREE_GB = 100000.0
    cd.REAP_CREATION_GRACE_H = 1.0
    cd.REAP_MIN_IDLE_H = 0.0
    cd.REAP_IDLE_PRESSURE_H = 0.0
    cd.REAP_MAX_PER_ROUND = 40
    cd.REAP_SCAN_SLICE = 400

    try:
        repo = make_repo(base)

        # ---- 1. 刚建好的树（宽限期内）：不许回收 ----
        fresh = base / "kg-fresh-worktree"
        add_worktree(repo, fresh, "codex/fresh")
        cd._save_reap_state({})  # 清节流
        n = cd._reap_dead_worktrees()
        check("刚建好的 worktree 未被回收", fresh.exists(), f"reaped={n}")
        check("宽限期判定为真", cd._within_creation_grace(fresh) is True)

        # ---- 2. 把创建时间推到宽限之外：同样的树必须可回收 ----
        # st_birthtime 不可写，直接替换判定里用的创建时间函数。
        orig_created = cd._wt_created_at
        cd._wt_created_at = lambda p: time.time() - 3 * 3600  # 3 小时前创建
        # 磁盘 fresh 目录的 mtime 也要过 idle 闸（已验证 REAP_*_IDLE_H=0）
        cd._save_reap_state({})
        n2 = cd._reap_dead_worktrees()
        check("超过宽限期后同一棵树被回收", not fresh.exists(), f"reaped={n2}")
        cd._wt_created_at = orig_created

        # ---- 3. 关闭宽限期（=0）：刚建好的树立刻可回收，证明这条开关有效 ----
        fresh2 = base / "kg-fresh-off"
        add_worktree(repo, fresh2, "codex/fresh-off")
        cd.REAP_CREATION_GRACE_H = 0.0
        cd._save_reap_state({})
        n3 = cd._reap_dead_worktrees()
        check("宽限期=0 时不做保护", not fresh2.exists(), f"reaped={n3}")
        cd.REAP_CREATION_GRACE_H = 1.0

        # ---- 4. 读不到创建时间时偏保守：视为「刚创建」，不回收 ----
        fresh3 = base / "kg-fresh-unknown"
        add_worktree(repo, fresh3, "codex/fresh-unknown")
        cd._wt_created_at = lambda p: time.time()  # 模拟读不到 -> 当前时间
        cd._save_reap_state({})
        n4 = cd._reap_dead_worktrees()
        check("创建时间不可读时保留现场", fresh3.exists(), f"reaped={n4}")

        # ---- 5. 手工 kgctl reap --go 也要过同一组闸（2026-11-13 实测缺口）----
        # 实测：/private/tmp/kg-t01-binding 03:37 建好，03:39 被 `kgctl reap --go`
        # 连目录一起删掉，而那个会话正拿它当工作目录。守护进程通道早就有判活 +
        # 创建宽限，只有这条手工通道漏了。
        import importlib.util as _ilu
        kg = load("kg_reap_guard", "kgctl.py")

        fresh_manual = base / "kg-manual-fresh"
        add_worktree(repo, fresh_manual, "codex/manual-fresh")
        os.environ["KG_REAP_CREATION_GRACE_H"] = "1"
        check("手工 reap：宽限期内的树被保留",
              kg._reap_wt_keep_reason(fresh_manual) == "creation-grace",
              kg._reap_wt_keep_reason(fresh_manual))

        # 反向断言：宽限期关掉后，同一棵树不再被这条判据保留，
        # 否则加固就变成「把每棵新树永久钉住」。
        os.environ["KG_REAP_CREATION_GRACE_H"] = "0"
        reason_off = kg._reap_wt_keep_reason(fresh_manual)
        check("手工 reap：宽限期=0 时不再因新建保留",
              reason_off != "creation-grace", reason_off)
        os.environ["KG_REAP_CREATION_GRACE_H"] = "1"

        # 真判活：写一条把树当 cwd 的会话记录，必须被认成「有人在用」。
        # 与 test-reap-weak-signal-mode 用的是同一条 live_guard 证据链。
        import json as _json
        session_root = base / "manual-sessions"
        session_root.mkdir(exist_ok=True)
        roll = session_root / "2026" / "11" / "13"
        roll.mkdir(parents=True, exist_ok=True)
        (roll / "rollout-manual.jsonl").write_text(_json.dumps({
            "type": "session_meta",
            "payload": {"cwd": str(fresh_manual),
                        "runtime_workspace_roots": [str(fresh_manual)]},
        }) + "\n")
        os.environ["KG_SESSION_ROOTS"] = str(session_root)
        os.environ["KG_LIVE_SESSION_MAX_AGE_S"] = "10800"
        import live_guard as _lg
        _lg.reset_caches()
        # 先把创建宽限关掉，否则先命中的永远是 creation-grace，测不到会话判据。
        os.environ["KG_REAP_CREATION_GRACE_H"] = "0"
        check("手工 reap：有活跃会话把树当 cwd 时保留",
              kg._reap_wt_keep_reason(fresh_manual) == "live-session",
              kg._reap_wt_keep_reason(fresh_manual))
        os.environ["KG_REAP_CREATION_GRACE_H"] = "1"

    finally:
        shutil.rmtree(base, ignore_errors=True)

    if failures:
        print("\n失败项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\nreap creation grace unit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
