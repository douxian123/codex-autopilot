#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动挡的最小安全检查。

覆盖一个曾经真实发生的坏路径：convergence 分支把未解冲突保留在提交里，
它和 main 本身没有新的合并冲突，于是被误判成 CLEAN，守护进程可能直接把它推上 main。
这里用临时仓库锁住这道门禁：坏分支不能入队，也不能 land。
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
KGCTL = OPS / "kgctl.py"


def run(cmd, cwd=None, env=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AssertionError(
            f"命令失败: {' '.join(map(str, cmd))}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}")
    return p


def git(repo, *args):
    return run(["git", "-C", str(repo), *args])


def kgctl(repo, trunk, *args):
    env = os.environ.copy()
    env["KG_REPO"] = str(repo)
    env["KG_TRUNK"] = str(trunk)
    return run([sys.executable, str(KGCTL), *args], cwd=str(OPS.parent), env=env, check=False)


def main():
    with tempfile.TemporaryDirectory(prefix="codex-autopilot-selftest-") as tmp:
        root = Path(tmp)
        repo = root / "state"
        trunk = root / "state-trunk"
        repo.mkdir()
        (repo / "ops" / "tasks").mkdir(parents=True)
        (repo / "ops" / "truth").mkdir(parents=True)
        (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "truth" / "ledger.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "truth" / "progress.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "truth" / "decisions.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "truth" / "acceptance.jsonl").write_text("", encoding="utf-8")
        (repo / "ops" / "truth" / "mainline.json").write_text("{}\n", encoding="utf-8")

        run(["git", "init", "-q", "-b", "main", str(repo)])
        git(repo, "config", "user.email", "selftest@example.invalid")
        git(repo, "config", "user.name", "Autopilot Selftest")
        (repo / "app.txt").write_text("main\n", encoding="utf-8")
        git(repo, "add", "app.txt")
        git(repo, "commit", "-q", "-m", "init")

        git(repo, "checkout", "-q", "-b", "codex/good")
        (repo / "good.txt").write_text("clean\n", encoding="utf-8")
        git(repo, "add", "good.txt")
        git(repo, "commit", "-q", "-m", "clean branch")

        git(repo, "checkout", "-q", "main")
        git(repo, "checkout", "-q", "-b", "codex/bad")
        (repo / "bad.txt").write_text(
            "<<<<<<< HEAD\nleft\n=======\nright\n>>>>>>> branch\n", encoding="utf-8")
        git(repo, "add", "bad.txt")
        git(repo, "commit", "-q", "-m", "unresolved marker branch")

        q = kgctl(repo, trunk, "queue")
        if "codex/good" not in q.stdout or "codex/bad" in q.stdout:
            raise AssertionError(f"queue 没有正确排除坏分支:\n{q.stdout}\n{q.stderr}")
        if "残留冲突标记" not in q.stdout:
            raise AssertionError(f"queue 没有报告残留冲突标记:\n{q.stdout}")

        land = kgctl(repo, trunk, "land", "codex/bad", "--dry")
        if land.returncode != 0 or "拒绝自动 land" not in land.stdout:
            raise AssertionError(f"land 没有拒绝坏分支:\n{land.stdout}\n{land.stderr}")

        good = kgctl(repo, trunk, "land", "codex/good", "--dry")
        if good.returncode != 0 or "无冲突" not in good.stdout:
            raise AssertionError(f"干净分支 dry-run 失败:\n{good.stdout}\n{good.stderr}")

    print("selftest ok: 干净分支可入队；带冲突标记的分支被 queue 排除且 land 拒绝。")


if __name__ == "__main__":
    main()
