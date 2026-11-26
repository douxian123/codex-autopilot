#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：主干工作树带陈旧台账、且与 main 没有共同祖先时，仍能安全切回 main。

为什么有这条（2026-11-16 守护重启后每拍实测）：

    [supersede] rc=1
    × 主干工作树切不到 main： error: Your local changes to the following files
      would be overwritten by checkout:
            docs/AGENT_SYNC.md
            docs/COCKPIT_BRIEF.md

真实现场有两条互相叠加的因素：

  1. `knowledge-graph-trunk` 与 `main` **没有共同祖先**（仓库是浅克隆 /
     嫁接历史：`git diff main...HEAD` 直接 fatal）。于是
     `trunk_head_is_truth_only()` 判不出「纯真相层」，`reset_trunk_head()` 走
     「切回 main」那条路。
  2. 那两份追加型台账的工作副本比 main 少 10318 / 10616 行，是**旧的截断快照**。
     `git checkout --detach main` 因此被拒，supersede / land 每一拍都失败。

危险点在于「怎么修」：直接把工作副本 checkout 丢掉，就等于删掉 main 的完整章节。
本门禁锁死三件事：
  1. 切得过去：不抛 SystemExit，HEAD 落到 main；
  2. 不丢内容：main 的章节一个不少，工作副本里独有的新条目也保留；
  3. 不越界：夹带**代码**改动时仍然 fail-closed（宁可失败，也不静默丢弃）。
"""

import contextlib
import importlib.util
import io
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


def git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          check=check, capture_output=True, text=True)


def load_kgctl(repo, trunk):
    os.environ["KG_REPO"] = str(repo)
    os.environ["KG_TRUNK"] = str(trunk)
    spec = importlib.util.spec_from_file_location("kgctl_reset_trunk", OPS / "kgctl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LEDGER = "docs/COCKPIT_BRIEF.md"
COMMITTED = "## 已落盘 A\n\nA 正文\n\n## 已落盘 B\n\nB 正文\n\n## 已落盘 C\n\nC 正文\n"
# trunk HEAD（无共同祖先的那个提交）里这份台账是**旧的截断版**：只有 B。
# 现场实测的正是这个形状——trunk 版比 main 少 10318 / 10616 行。
TRUNCATED = "## 已落盘 B\n\nB 正文\n"


def sections(text: str) -> set:
    return {ln for ln in text.splitlines() if ln.startswith("## ")}


def build(root: Path):
    repo = root / "repo"
    for d in ("ops/truth", "ops/tasks", "docs", "data", "backend"):
        (repo / d).mkdir(parents=True)
    (repo / "ops" / "truth" / "progress.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
    (repo / LEDGER).write_text(COMMITTED, encoding="utf-8")
    (repo / "docs" / "AGENT_SYNC.md").write_text("## 同步条目\n", encoding="utf-8")
    (repo / "data" / "task-L0031.db-wal").write_text("wal\n", encoding="utf-8")
    (repo / "backend" / "app.ts").write_text("export const base = 0;\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")

    trunk = root / "repo-trunk"
    git(repo, "worktree", "add", "-q", "--detach", str(trunk), "main")
    # 造一个与 main **无共同祖先**、且该台账是旧截断版的提交，让 trunk HEAD 停在它上面。
    # 两点都必要：没有共同祖先 → 三向 diff 算不出来；树不同 → checkout 真的会被拒。
    # 注意代码文件也要造出差异：若 ghost 与 main 的 `backend/app.ts` 逐字节相同，
    # git 会把本地改动原样带过去、根本不报错，那条安全断言就会失去意义。
    (trunk / LEDGER).write_text(TRUNCATED, encoding="utf-8")
    (trunk / "backend" / "app.ts").write_text("export const ghost = 0;\n", encoding="utf-8")
    git(trunk, "add", "-A")
    tree = git(trunk, "write-tree").stdout.strip()
    gh = git(trunk, "commit-tree", tree, "-m", "unrelated root").stdout.strip()
    git(trunk, "checkout", "-q", "--detach", gh)
    probe = git(trunk, "diff", "--name-only", "main...HEAD", check=False)
    return repo, trunk, gh, probe.returncode


def main():
    with tempfile.TemporaryDirectory(prefix="reset-trunk-stale-") as raw:
        root = Path(raw)
        repo, trunk, ghost, probe_rc = build(root)
        check("现场前提：trunk 与 main 没有共同祖先（三向 diff 算不出来）",
              probe_rc != 0, f"(rc={probe_rc})")
        kg = load_kgctl(repo, trunk)
        kg.REPO = repo
        kg.MAIN = "main"

        # 陈旧台账：比 main 少一节，另外多一条本地新条目。
        (trunk / LEDGER).write_text(
            "## 已落盘 B\n\nB 正文\n\n## 本地新条目 D\n\nD 正文\n", encoding="utf-8")
        # 运行时伴随文件残留，复刻现场那两条 wal 的形态。
        (trunk / "data" / "task-L0031.db-wal").unlink()
        snap = kg._snapshot_truth_files(trunk)
        check("快照抓到了陈旧台账（否则切完就真丢了）", LEDGER in snap,
              f"(keys={sorted(snap)[:4]})")

        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                kg.reset_trunk_head(trunk)
            crashed = None
        except SystemExit as exc:  # 旧实现在这里 exit(1)
            crashed = exc
        check("能切回 main（旧实现此处 exit 1）", crashed is None,
              f"(exit={getattr(crashed, 'code', None)}; out={buf.getvalue().strip()[:160]!r})")
        if crashed is None:
            head = git(trunk, "rev-parse", "HEAD").stdout.strip()
            want = git(repo, "rev-parse", "main").stdout.strip()
            check("HEAD 落在 main 上", head == want, f"({head[:8]} vs {want[:8]})")

        text = (trunk / LEDGER).read_text(encoding="utf-8")
        got = sections(text)
        check("main 的章节 A 没被删", "## 已落盘 A" in got, f"(sections={sorted(got)})")
        check("main 的章节 B 没被删", "## 已落盘 B" in got)
        check("main 的章节 C 没被删", "## 已落盘 C" in got)
        check("工作副本独有的新条目 D 被保留", "## 本地新条目 D" in got,
              f"(sections={sorted(got)})")

        # ---- 安全边界：夹带代码改动时必须 fail-closed，绝不静默丢弃 ----
        # 先把上面并集写回的台账放回 HEAD 版，否则切 ghost 自己就会被拒。
        git(trunk, "checkout", "--", LEDGER)
        git(trunk, "checkout", "-q", "--detach", ghost)
        (trunk / "backend" / "app.ts").write_text("export const x = 1;\n", encoding="utf-8")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                kg.reset_trunk_head(trunk)
            refused = False
        except SystemExit:
            refused = True
        check("夹带代码改动时拒绝切换（不静默丢代码）", refused,
              "(旧代码把它当普通切换处理)")
        check("拒绝之后代码改动仍在工作区",
              "backend/app.ts" in git(trunk, "status", "--porcelain").stdout)
    if failures:
        print(f"\nreset trunk head stale ledger unit: FAIL ({len(failures)})")
        return 1
    print("\nreset trunk head stale ledger unit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
