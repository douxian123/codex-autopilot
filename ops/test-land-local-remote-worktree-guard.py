#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""land 在本地路径远端把 main 挂在 worktree 上时必须提前 fail-closed。

2026-11-28 受控 151 候选实测：push 五次后才报
``refusing to update checked out branch``，但那时 merge、门禁、进展和验收台账
都已经写了，看起来像已落盘、实际上永远推不上去。这里用真实 git 仓库锁住
「写之前发现、远端与本地 ref 不动、台账不写」，并验证裸远端与显式允许更新
当前分支的配置不会被误伤。
"""

import importlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))


def run(cmd, cwd=None, env=None, check=True):
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(
            f"命令失败: {' '.join(map(str, cmd))}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    return proc


def git(repo, *args):
    return run(["git", "-C", str(repo), *args])


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def main():
    with tempfile.TemporaryDirectory(prefix="codex-autopilot-land-worktree-guard-") as tmp:
        root = Path(tmp)
        origin = root / "origin-nonbare"
        state = root / "state"
        trunk = root / "state-trunk"
        bare = root / "origin-bare.git"

        run(["git", "init", "-q", "-b", "main", str(origin)])
        git(origin, "config", "user.email", "selftest@example.invalid")
        git(origin, "config", "user.name", "Autopilot Selftest")
        (origin / "ops" / "tasks").mkdir(parents=True)
        (origin / "ops" / "truth").mkdir(parents=True)
        (origin / "docs").mkdir(parents=True)
        (origin / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
        (origin / "ops" / "truth" / "ledger.jsonl").write_text("", encoding="utf-8")
        (origin / "ops" / "truth" / "progress.jsonl").write_text("", encoding="utf-8")
        (origin / "ops" / "truth" / "acceptance.jsonl").write_text("", encoding="utf-8")
        (origin / "docs" / "PROJECT_STATUS.md").write_text("# status\n\nbase\n",
                                                            encoding="utf-8")
        (origin / "app.txt").write_text("base\n", encoding="utf-8")
        write_json(origin / "ops" / "central.config.json",
                   {"build": {"cmd": "", "cwd": "."}, "verify": []})
        git(origin, "add", ".")
        git(origin, "commit", "-q", "-m", "init")

        run(["git", "clone", "-q", str(origin), str(state)])
        git(state, "config", "user.email", "selftest@example.invalid")
        git(state, "config", "user.name", "Autopilot Selftest")
        git(state, "checkout", "-q", "-b", "codex/good")
        (state / "good.txt").write_text("clean\n", encoding="utf-8")
        git(state, "add", "good.txt")
        git(state, "commit", "-q", "-m", "clean branch")

        origin_before = git(origin, "rev-parse", "main").stdout.strip()
        state_main_before = git(state, "rev-parse", "main").stdout.strip()
        remote_before = git(state, "rev-parse", "origin/main").stdout.strip()
        progress_before = (state / "ops" / "truth" / "progress.jsonl").read_text(
            encoding="utf-8")
        acceptance_before = (state / "ops" / "truth" / "acceptance.jsonl").read_text(
            encoding="utf-8")

        env = os.environ.copy()
        env["KG_REPO"] = str(state)
        env["KG_TRUNK"] = str(trunk)
        env["KG_TRUTH_ID_STATE_ROOT"] = str(root / "truth-ids")
        env["KG_MODEL_PROBE"] = "off"
        landed = run([sys.executable, str(OPS / "kgctl.py"), "land", "codex/good"],
                     cwd=str(OPS.parent), env=env, check=False)
        joined = landed.stdout + landed.stderr
        if landed.returncode == 0:
            raise AssertionError(f"被占用的本地远端没有挡住 land:\n{joined}")
        for needle in ("远端 origin 是本机仓库", "未执行 merge、门禁或台账写入"):
            if needle not in joined:
                raise AssertionError(f"land 没有说明结构性阻塞 {needle!r}:\n{joined}")

        if git(origin, "rev-parse", "main").stdout.strip() != origin_before:
            raise AssertionError("preflight 动了远端 main")
        if git(state, "rev-parse", "main").stdout.strip() != state_main_before:
            raise AssertionError("preflight 动了本地 main")
        if git(state, "rev-parse", "origin/main").stdout.strip() != remote_before:
            raise AssertionError("preflight 动了 origin/main 引用")
        if (state / "ops" / "truth" / "progress.jsonl").read_text(
                encoding="utf-8") != progress_before:
            raise AssertionError("preflight 之后进展台账被写了")
        if (state / "ops" / "truth" / "acceptance.jsonl").read_text(
                encoding="utf-8") != acceptance_before:
            raise AssertionError("preflight 之后验收台账被写了")
        if (state / ".git" / "kgctl-land.lock").exists():
            raise AssertionError("preflight 之后留下了 land 锁")
        if trunk.exists():
            raise AssertionError("preflight 之后创建了一次性落盘工作树")

        # 直接读预检函数：本地非裸仓库占用 main 时应指名道姓，裸远端与显式
        # 允许更新当前分支的配置不能被误伤。
        os.environ["KG_REPO"] = str(state)
        os.environ["KG_TRUNK"] = str(trunk)
        try:
            import kgctl
            importlib.reload(kgctl)
            owner = kgctl._local_remote_main_worktree_owner(state)
            if Path(owner).resolve() != origin.resolve():
                raise AssertionError(f"没有识别占用 main 的 worktree: {owner!r}")

            git(origin, "config", "receive.denyCurrentBranch", "ignore")
            if kgctl._local_remote_main_worktree_owner(state):
                raise AssertionError("显式允许更新当前分支时被误判为阻塞")
            git(origin, "config", "--unset", "receive.denyCurrentBranch")

            run(["git", "init", "-q", "--bare", str(bare)])
            git(state, "remote", "set-url", "origin", str(bare))
            if kgctl._local_remote_main_worktree_owner(state):
                raise AssertionError("裸远端被误判为本地 worktree 占用")
        finally:
            for key, value in (
                ("KG_REPO", str(state)), ("KG_TRUNK", str(trunk)),
                ("KG_TRUTH_ID_STATE_ROOT", str(root / "truth-ids")),
                ("KG_MODEL_PROBE", "off"),
            ):
                os.environ.pop(key, None)

    print("selftest ok: land 在本地路径远端占用 main 时写前 fail-closed，"
          "远端/本地 main、台账与落盘锁都不动；裸远端与显式允许配置不误伤。")


if __name__ == "__main__":
    main()
