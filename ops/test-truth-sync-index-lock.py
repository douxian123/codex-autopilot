#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：陈旧 index.lock 不得让真相同步静默空转。

为什么有这条（2026-11-17 实测，守护重启后第一拍就撞上）：

  共享检出 `/Users/danta/.newmax/workspace/knowledge-graph/.git/index.lock`
  是一次被中断的 git 在 22:25:34 留下的 **0 字节**文件，`lsof` 查无持有者。
  此后每一拍 tick 的 `[7/8] commit + push truth state` 里

      git add -- ops/truth ...

  都以 `fatal: Unable to create '.../index.lock': File exists.` 失败。而旧实现
  **不检查 add 的返回码**：没有暂存就没有 staged，`sync_trunk_truth` 就返回
  False —— 调用方看起来像「这拍本来就没改动」。于是新写入的约束台账
  (`ops/truth/constraints.jsonl`) 连写几天都进不了 git，而守护与面板全显示正常。

这条门禁锁死四件事：
  1. 陈旧（无持有者 + 足够老）的 index.lock 会被隔离留证，真相同步照常完成；
  2. 被真实 git 进程持有的 index.lock **不许**被删/被搬，同步 loud-fail；
  3. 探不清持有者时按「挡住」处理，绝不假装同步过；
  4. `git add` 失败时返回 False 并打印，不再静默返回「无改动」。
"""

import importlib.util
import os
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


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True)


def load_kgctl(repo, trunk):
    os.environ["KG_REPO"] = str(repo)
    os.environ["KG_TRUNK"] = str(trunk)
    spec = importlib.util.spec_from_file_location("kgctl_index_lock", OPS / "kgctl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def seed(root: Path):
    """一个最小的状态根：只有真相层目录，且带一条待提交的真相改动。"""
    repo = root / "repo"
    for d in ("ops/truth", "ops/tasks", "ops/context", "docs"):
        (repo / d).mkdir(parents=True)
    (repo / "ops" / "truth" / "progress.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "truth" / "ledger.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "truth" / "acceptance.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "truth" / "decisions.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
    (repo / "docs" / "PROJECT_STATUS.md").write_text("# status\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def make_lock(repo: Path, age_seconds: float):
    """在这个检出的真实 index.lock 路径上造一把陈旧锁，并把 mtime 往前拨。"""
    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    old = time.time() - age_seconds
    os.utime(lock, (old, old))
    return lock


def main():
    with tempfile.TemporaryDirectory(prefix="truth-sync-index-lock-") as raw:
        root = Path(raw)
        repo = seed(root)
        kg = load_kgctl(repo, root / "unused-trunk")
        kg.REPO = repo
        kg.MAIN = "main"
        # 本测试依赖 lsof 的真实探活结果，不能让它落在「探不清」分支上。
        if kg._index_lock_holders(Path("/nonexistent-lock-file")) is None:
            print("SKIP: 本机没有可用的 lsof，index.lock 探活无法验证（不是通过）")
            return 0

        # ---- 1. 无锁：可控参照，同步应当成功 ----
        (repo / "ops" / "truth" / "constraints.jsonl").write_text(
            '{"id":"C-0001"}\n', encoding="utf-8")
        ok = kg.sync_trunk_truth(repo, "sync from tick")
        check("无 index.lock 时真相同步成功", ok is True, f"(rc={ok})")
        tracked = git(repo, "ls-files", "ops/truth/constraints.jsonl").stdout.strip()
        check("新台账被真实提交进版本库", tracked != "", f"(ls-files={tracked!r})")

        # ---- 2. 陈旧锁：隔离留证，同步照常完成 ----
        lock = make_lock(repo, age_seconds=3600)
        before = git(repo, "rev-parse", "HEAD").stdout.strip()
        (repo / "ops" / "truth" / "progress.jsonl").write_text(
            '{"id":"P-0002"}\n', encoding="utf-8")
        ok = kg.sync_trunk_truth(repo, "sync from tick with stale lock")
        after = git(repo, "rev-parse", "HEAD").stdout.strip()
        check("陈旧 index.lock 下真相同步仍然完成", ok is True, f"(rc={ok})")
        check("陈旧锁下确实产生了新的 chore(truth) 提交", before != after,
              f"({before[:8]} -> {after[:8]})")
        check("陈旧锁被移走（不再挡路）", not lock.exists(), f"(lock={lock})")
        evidence = list((repo / ".git").glob("index.lock.stale-*"))
        check("隔离留下证据文件（可复盘，不是凭空删除）", len(evidence) == 1,
              f"(evidence={[p.name for p in evidence]})")

        # ---- 3. 被真实进程持有的锁：绝不搬动，同步 loud-fail ----
        lock = make_lock(repo, age_seconds=3600)
        holder = subprocess.Popen([sys.executable, "-c",
                                   "import sys,time; f=open(sys.argv[1],'a'); time.sleep(30)",
                                   str(lock)])
        try:
            for _ in range(50):
                if kg._index_lock_holders(lock):
                    break
                time.sleep(0.1)
            held = bool(kg._index_lock_holders(lock))
            (repo / "ops" / "truth" / "progress.jsonl").write_text(
                '{"id":"P-0003"}\n', encoding="utf-8")
            before = git(repo, "rev-parse", "HEAD").stdout.strip()
            ok = kg.sync_trunk_truth(repo, "sync while a real holder is alive")
            after = git(repo, "rev-parse", "HEAD").stdout.strip()
            check("测试前提：确实探测到持有者", held, "(lsof 没看到子进程)")
            check("有人持有 index.lock 时同步被挡住", ok is False, f"(rc={ok})")
            check("有人持有时不产生提交", before == after,
                  f"({before[:8]} -> {after[:8]})")
            check("别人正在持有的锁没有被搬走", lock.exists(), f"(lock={lock})")
        finally:
            holder.terminate()
            try:
                holder.wait(timeout=10)
            except subprocess.TimeoutExpired:
                holder.kill()

        # ---- 4. 探不清持有者（lsof 不可用）：按「挡住」处理，绝不乱删 ----
        lock = make_lock(repo, age_seconds=3600)
        real_which = kg.shutil.which
        kg.shutil.which = lambda name, *a, **k: None if name == "lsof" else real_which(name, *a, **k)
        try:
            state = kg.index_lock_state(repo)
            check("lsof 不可用时判为「探不清 = 挡住」",
                  state.get("blocked") is True and state.get("holders") is None,
                  f"(state={state})")
            before = git(repo, "rev-parse", "HEAD").stdout.strip()
            ok = kg.sync_trunk_truth(repo, "sync with unclear holder")
            after = git(repo, "rev-parse", "HEAD").stdout.strip()
            check("探不清时同步返回 False（不假装同步过）", ok is False, f"(rc={ok})")
            check("探不清时不产生提交", before == after,
                  f"({before[:8]} -> {after[:8]})")
            check("探不清时锁仍在原地（绝不乱删）", lock.exists(), f"(lock={lock})")
        finally:
            kg.shutil.which = real_which

    if failures:
        print(f"\ntruth sync index-lock unit: FAIL ({len(failures)})")
        return 1
    print("\ntruth sync index-lock unit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
