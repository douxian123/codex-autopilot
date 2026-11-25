#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：旧 HEAD 造成的文件级假脏不得卡死真相同步，也不得混进 chore(truth)。

2026-11-25 现场：共享检出的 HEAD 还停在已合入的 codex 分支，工作副本与索引却已经
是 origin/main 的 blob。`git status` 报 `M  ops/disk_reap.py`，tick 的 [7/8] 因此
每拍拒绝同步真相层；但这个文件没有任何在途内容，直接放宽门禁又可能把它吞进
`chore(truth)`。本夹具用真实 git 仓库锁死五条：

  1. 工作副本与索引都等于 origin/main 的假脏被跳过，真相层照常提交；
  2. 假脏文件绝不进入 chore(truth) 提交，工作副本一个字节不动；
  3. 工作副本等于 origin/main、索引仍是旧 HEAD 的假脏也被跳过；
  4. 真代码改动、索引里第三种暂存内容、文件删除仍然 fail-closed。
"""

import importlib.util
import os
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


def git(repo, *args, check_rc=True):
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=check_rc, capture_output=True, text=True)


def head(repo):
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def load_kgctl(repo):
    os.environ["KG_REPO"] = str(repo)
    os.environ["KG_TRUNK"] = str(repo / "unused-trunk")
    os.environ["KG_MODEL_PROBE"] = "off"
    spec = importlib.util.spec_from_file_location(
        "kgctl_stale_head_fake_dirty", OPS / "kgctl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.REPO = repo
    mod.MAIN = "main"
    return mod


def seed(root):
    repo = root / "repo"
    for d in ("ops/truth", "ops/tasks", "ops/context", "docs"):
        (repo / d).mkdir(parents=True)
    for rel in ("progress.jsonl", "ledger.jsonl", "acceptance.jsonl", "decisions.jsonl"):
        (repo / "ops" / "truth" / rel).write_text("", encoding="utf-8")
    (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
    (repo / "docs" / "PROJECT_STATUS.md").write_text("# status\n", encoding="utf-8")
    (repo / "app.txt").write_text("main-v1\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")

    # 已合入的功能分支仍是共享检出的 HEAD。
    git(repo, "checkout", "-q", "-b", "codex/old")
    (repo / "app.txt").write_text("old-head\n", encoding="utf-8")
    git(repo, "add", "app.txt")
    git(repo, "commit", "-q", "-m", "old branch")

    # main 前进到 origin/main，app.txt 换成了第三份内容。
    git(repo, "checkout", "-q", "main")
    (repo / "app.txt").write_text("origin-main\n", encoding="utf-8")
    git(repo, "add", "app.txt")
    git(repo, "commit", "-q", "-m", "origin advance")
    git(repo, "update-ref", "refs/remotes/origin/main", head(repo))
    git(repo, "checkout", "-q", "codex/old")
    return repo


def write_truth(repo, line):
    p = repo / "ops" / "truth" / "progress.jsonl"
    p.write_text(p.read_text(encoding="utf-8") + line + "\n", encoding="utf-8")


def assert_blocked(kg, repo, truth_line, label):
    write_truth(repo, truth_line)
    before = head(repo)
    ok = kg.sync_trunk_truth(repo, label)
    after = head(repo)
    check(f"{label}：同步被拒绝", ok is False, f"(rc={ok})")
    check(f"{label}：没有产生 chore(truth) 提交", before == after,
          f"({before[:8]} -> {after[:8]})")


def main():
    with tempfile.TemporaryDirectory(prefix="truth-sync-stale-head-") as raw:
        root = Path(raw)
        repo = seed(root)
        kg = load_kgctl(repo)

        origin_blob = git(repo, "rev-parse", "origin/main:app.txt").stdout.strip()
        old_blob = git(repo, "rev-parse", "HEAD:app.txt").stdout.strip()
        check("夹具前提：旧 HEAD 与 origin/main 的 app.txt 不同",
              origin_blob != old_blob)

        # ---- 1. 工作副本与索引都等于 origin/main：现场那条形如 `M  app.txt` 的假脏 ----
        (repo / "app.txt").write_text("origin-main\n", encoding="utf-8")
        git(repo, "add", "app.txt")
        status = git(repo, "status", "--porcelain", "--", "app.txt").stdout.strip()
        check("夹具前提：git status 报出已暂存的假脏", status == "M  app.txt",
              f"(status={status!r})")
        write_truth(repo, '{"id":"P-0001"}')
        ok = kg.sync_trunk_truth(repo, "sync fake dirty staged")
        check("工作副本与索引都等于 origin 的假脏不挡住真相同步", ok is True,
              f"(rc={ok})")
        changed = git(repo, "show", "--name-only", "--format=", "HEAD").stdout
        check("假脏文件没有混进 chore(truth) 提交", "app.txt" not in changed,
              f"(changed={changed.strip()!r})")
        check("真相层确实被提交", "ops/truth/progress.jsonl" in changed,
              f"(changed={changed.strip()!r})")
        check("假脏文件的工作副本保持 origin/main 原样",
              (repo / "app.txt").read_text(encoding="utf-8") == "origin-main\n")
        # 不能用 strip()：它会把 porcelain 第一列的前导空格吃掉，` M` 会变成 `M`。
        after_reset = git(repo, "status", "--porcelain", "--", "app.txt").stdout.rstrip("\n")
        check("reset 只动索引，文件相对旧 HEAD 仍显示为工作区改动",
              after_reset == " M app.txt", f"(status={after_reset!r})")

        # ---- 2. 工作副本等于 origin/main、索引仍是旧 HEAD：未暂存的假脏 ----
        write_truth(repo, '{"id":"P-0002"}')
        ok = kg.sync_trunk_truth(repo, "sync fake dirty unstaged")
        check("索引仍在旧 HEAD 的假脏也不挡住真相同步", ok is True, f"(rc={ok})")
        changed = git(repo, "show", "--name-only", "--format=", "HEAD").stdout
        check("未暂存假脏同样没有混进提交", "app.txt" not in changed,
              f"(changed={changed.strip()!r})")

        # ---- 3. 真代码改动仍然 fail-closed ----
        (repo / "app.txt").write_text("real local edit\n", encoding="utf-8")
        git(repo, "add", "app.txt")
        assert_blocked(kg, repo, '{"id":"P-0003"}', "real code edit")
        check("真代码改动仍留在工作区",
              (repo / "app.txt").read_text(encoding="utf-8") == "real local edit\n")

        # ---- 4. 工作副本等于 origin，索引是第三种 blob：不能当成假脏 ----
        (repo / "app.txt").write_text("origin-main\n", encoding="utf-8")
        git(repo, "add", "app.txt")
        third_oid = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                                   input="third staged content\n", text=True,
                                   capture_output=True, check=True).stdout.strip()
        git(repo, "update-index", "--cacheinfo", f"100644,{third_oid},app.txt")
        assert_blocked(kg, repo, '{"id":"P-0004"}', "third staged blob")

        # ---- 5. 文件被删除：diff --quiet 的陷阱，必须继续 fail-closed ----
        git(repo, "reset", "-q", "--", "app.txt")
        (repo / "app.txt").unlink()
        assert_blocked(kg, repo, '{"id":"P-0005"}', "deleted file")

    if failures:
        print(f"\ntruth sync stale head fake dirty: FAIL ({len(failures)})")
        return 1
    print("\ntruth sync stale head fake dirty: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
