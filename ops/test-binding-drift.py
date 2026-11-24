#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：插件绑在旧脏 worktree 时，不许把当前干净状态根的需求写错账本。

2026-11-24 现场：`.mcp.json` 绑定旧脏状态根，当前会话 `cwd` 已是同一仓库里
另一个干净状态根。旧路由按 git common dir 把两者归并成同一个项目，于是需求
写进旧账本，面板仍显示绑定正常。这里用真实 git worktree 锁住检测、fail-closed
和恢复指令，而不是只测一个手填的 dict。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


def run(cmd, cwd=None, env=None, check_rc=True):
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if check_rc and proc.returncode != 0:
        raise AssertionError(
            "命令失败: %s\nstdout:\n%s\nstderr:\n%s" % (cmd, proc.stdout, proc.stderr))
    return proc


def git(repo, *args):
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "t@t"
    return run(["git", "-C", str(repo), *args], env=env)


def make_state_root(root):
    (root / "ops" / "tasks").mkdir(parents=True)
    (root / "ops" / "truth").mkdir(parents=True)
    (root / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
    (root / "ops" / "truth" / "ledger.jsonl").write_text("", encoding="utf-8")


def make_plugin(base, home, bound):
    plugin = base / "plugin"
    (plugin / "ops").mkdir(parents=True)
    shutil.copy(OPS / "plugin_binding.py", plugin / "ops" / "plugin_binding.py")
    for stub in ("kgctl.py", "central_mcp.py", "truthctl.py", "central_daemon.py"):
        (plugin / "ops" / stub).write_text("# stub\n", encoding="utf-8")
    (plugin / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"codex-autopilot": {
            "env": {"KG_REPO": str(bound.resolve())}}},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return plugin


def probe(plugin, home, current, extra_env=None):
    code = (
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import plugin_binding as b\n"
        "health = b.binding_health()\n"
        "print(json.dumps({\n"
        "  'health': health,\n"
        "  'cwd_drift': b.worktree_drift_for_cwd(sys.argv[2]),\n"
        "  'guard': b.binding_drift_guard('intake_request', sys.argv[2]),\n"
        "  'recovery': b.binding_recovery_action(health),\n"
        "}, ensure_ascii=False))\n"
    )
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}
    for key in ("KG_REPO", "KG_TRUNK"):
        env.pop(key, None)
    if extra_env:
        env.update(extra_env)
    proc = run([sys.executable, "-c", code, str(plugin / "ops"), str(current)],
               env=env, check_rc=False)
    if proc.returncode != 0:
        raise AssertionError("探针跑不起来：\n%s\n%s" % (proc.stdout, proc.stderr))
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main():
    with tempfile.TemporaryDirectory(prefix="autopilot-binding-drift-") as tmp:
        base = Path(tmp)
        home = base / "home"
        home.mkdir()
        repo = base / "repo"
        repo.mkdir()
        (repo / "README").write_text("main\n", encoding="utf-8")
        git(repo, "init", "-q", "-b", "main")
        git(repo, "add", "README")
        git(repo, "commit", "-q", "-m", "init")

        bound = base / "bound-old"
        current = base / "current-clean"
        git(repo, "worktree", "add", "-q", "-b", "bound-old", str(bound))
        git(repo, "worktree", "add", "-q", "-b", "current-clean", str(current))
        for root in (bound, current):
            make_state_root(root)
            git(root, "add", "ops")
            git(root, "commit", "-q", "-m", "state root")

        bound = bound.resolve()
        current = current.resolve()
        plugin = make_plugin(base, home, bound)

        print("1) 绑定根脏、会话 cwd 指向同仓库干净状态根：必须 fail-closed")
        (bound / "UNCOMMITTED.txt").write_text("old binding drift\n", encoding="utf-8")
        report = probe(plugin, home, current)
        health = report["health"]
        check("绑定体检判 broken", health.get("status") == "broken", health)
        drift = health.get("drift") or {}
        check("体检识别旧绑定根", drift.get("bound_repo") == str(bound), drift)
        check("体检给出干净状态根", drift.get("current_repo") == str(current), drift)
        check("cwd 探针识别同一漂移",
              (report["cwd_drift"] or {}).get("current_repo") == str(current),
              report["cwd_drift"])
        guard = report["guard"] or {}
        check("投稿被拦下", guard.get("code") == "AUTOPILOT_BINDING_DRIFT", guard)
        check("恢复指令要求重新绑定当前根", str(current) in guard.get("need_action", ""), guard)
        check("恢复指令要求重开 Codex", "重新打开 Codex" in guard.get("need_action", ""), guard)
        check("恢复动作类型为 binding_drift",
              (report["recovery"] or {}).get("kind") == "binding_drift",
              report["recovery"])

        print("2) 显式 KG_REPO 与插件绑定不同：即使旧绑定干净也要报漂移")
        (bound / "UNCOMMITTED.txt").unlink()
        explicit = probe(plugin, home, current, extra_env={"KG_REPO": str(current)})
        check("显式漂移仍判 broken", explicit["health"].get("status") == "broken",
              explicit["health"])
        explicit_drift = explicit["health"].get("drift") or {}
        check("显式漂移保留当前项目",
              explicit_drift.get("current_repo") == str(current), explicit_drift)

        print("3) 绑定根干净、同仓库多状态根：正常工作流不受影响")
        (bound / "UNCOMMITTED.txt").write_text("old binding drift\n", encoding="utf-8")
        installer_check = probe(plugin, home, current, extra_env={
            "CODEX_AUTOPILOT_SKIP_WORKTREE_DRIFT": "1"})
        check("安装自检 opt-out 不误报 broken",
              installer_check["health"].get("status") == "ok",
              installer_check["health"])
        check("安装自检 opt-out 不误报漂移",
              installer_check["health"].get("drift") is None,
              installer_check["health"])
        (bound / "UNCOMMITTED.txt").unlink()
        clean = probe(plugin, home, current)
        check("干净绑定不误报 broken", clean["health"].get("status") == "ok",
              clean["health"])
        check("干净绑定不误报漂移", clean["health"].get("drift") is None,
              clean["health"])
        check("投稿不会被拦", clean["guard"] is None, clean["guard"])

    print()
    if failures:
        print(f"FAILED {len(failures)} 项：")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("binding drift ok: 旧脏绑定被识别并 fail-closed，干净绑定不受影响。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
