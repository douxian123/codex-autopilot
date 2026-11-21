#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：被 .gitignore 排除的生成视图不得卡死真相同步，也不得把半暂存留给落盘。

为什么有这条（2026-11-17 本轮 land 实测）：

  守护/面板会重建一批生成视图（`docs/PROJECT_STATUS.md`、`docs/CONTROL_BOARD.md`、
  `docs/CONTROL_PLANE.html`、`ops/context/`）。它们在 .gitignore 里被显式排除，也
  从未进过版本库。但 `TRUTH_PATHS` 把它们和真账本列在一起，旧实现的 `git add` 循环
  对每个存在的路径无脑 `git add`：

      → 已生成的视图：`git add` 报
        `The following paths are ignored by one of your .gitignore files`
        → 循环把这次失败记成 add_failures，函数返回 False。

  更糟的是，前面已经 add 成功的 7 个真账本留在索引里不回滚，形成半暂存；紧接着
  `git merge` 立刻被这批本地改动挡住：

      error: Your local changes to the following files would be overwritten by merge
        ops/truth/acceptance.jsonl ops/truth/progress.jsonl …

  结果是源分支提交永远进不了 main —— 合并管家在最关键的一步静默卡死，面板却照常
  显示「自动合入=开」。本轮带着一个真实修复 land 时正是这样被挡住。

本门禁锁死四件事：
  1. 存在 ignored 生成视图时，真相同步照常提交真账本（不再被视图卡死）；
  2. ignored 生成视图绝不被提交进版本库；
  3. `.bak-*` 这类 smoke 测试遗留副本绝不被卷进 `chore(truth)`；
  4. add 失败时索引被清干净，不给下一次 land 留半暂存。
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


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True)


def load_kgctl(repo):
    os.environ["KG_REPO"] = str(repo)
    os.environ["KG_TRUNK"] = str(repo)
    spec = importlib.util.spec_from_file_location("kgctl_ignored_views", OPS / "kgctl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def seed(root: Path):
    repo = root / "repo"
    for d in ("ops/truth", "ops/tasks", "docs"):
        (repo / d).mkdir(parents=True)
    (repo / "ops" / "truth" / "progress.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "truth" / "ledger.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "truth" / "acceptance.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "truth" / "decisions.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
    (repo / "docs" / "PRODUCT_MAINLINE.md").write_text("# 主线\n", encoding="utf-8")
    # 关键复现条件：生成视图在 .gitignore 里被排除。
    (repo / ".gitignore").write_text(
        "docs/PROJECT_STATUS.md\n"
        "docs/PROJECT_CONSTITUTION.md\n"
        "docs/CONTROL_BOARD.md\n"
        "docs/CONTROL_PLANE.html\n"
        "ops/context/\n",
        encoding="utf-8",
    )
    (repo / "ops" / "central.config.json").write_text(
        '{"build": {"cmd": "", "cwd": "."}}\n', encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def main():
    with tempfile.TemporaryDirectory(prefix="truth-sync-ignored-views-") as raw:
        root = Path(raw)
        repo = seed(root)
        kg = load_kgctl(repo)
        kg.REPO = repo
        kg.MAIN = "main"

        # 生成视图真的存在（tick/dash 会写它们），且一条真账本有改动。
        (repo / "docs" / "PROJECT_STATUS.md").write_text("# status\n", encoding="utf-8")
        (repo / "docs" / "PROJECT_CONSTITUTION.md").write_text("# constitution\n", encoding="utf-8")
        (repo / "docs" / "CONTROL_BOARD.md").write_text("# board\n", encoding="utf-8")
        (repo / "docs" / "CONTROL_PLANE.html").write_text("<html></html>\n", encoding="utf-8")
        (repo / "ops" / "context").mkdir(parents=True, exist_ok=True)
        (repo / "ops" / "context" / "pack.json").write_text("{}\n", encoding="utf-8")
        # smoke 测试遗留的副本：不该被提交。
        (repo / "ops" / "tasks" / "inbox.jsonl.bak-route-smoke-20261116-235548").write_text(
            "{}\n", encoding="utf-8")
        (repo / "ops" / "truth" / "progress.jsonl").write_text(
            '{"id":"P-0001","status":"done"}\n', encoding="utf-8")

        ok = kg.sync_trunk_truth(repo, "sync from tick")
        check("存在 ignored 生成视图时真相同步仍然成功", ok is True, f"(rc={ok})")

        changed = git(repo, "show", "--name-only", "--format=", "HEAD").stdout
        check("真账本被提交", "ops/truth/progress.jsonl" in changed,
              f"(changed={changed.strip()!r})")
        for view in ("docs/PROJECT_STATUS.md", "docs/PROJECT_CONSTITUTION.md",
                     "docs/CONTROL_BOARD.md",
                     "docs/CONTROL_PLANE.html", "ops/context/pack.json"):
            check(f"ignored 生成视图不进版本库：{view}", view not in changed,
                  f"(changed={changed.strip()!r})")
        check("smoke 遗留副本不进版本库",
              "bak-route-smoke" not in changed, f"(changed={changed.strip()!r})")

        # 失败路径：add 被挡住时索引必须干净，不留半暂存给下一次 land。
        (repo / "ops" / "tasks" / "lanes.json").write_text("[]\n", encoding="utf-8")
        real_add = kg.proc_run

        def failing_add(args, **kwargs):
            if len(args) > 3 and args[0] == "git" and args[3] == "add":
                return subprocess.CompletedProcess(args, 1, "", "add blocked for test")
            return real_add(args, **kwargs)

        kg.proc_run = failing_add
        blocked = kg.sync_trunk_truth(repo, "sync with failing add")
        kg.proc_run = real_add
        check("add 失败时同步明确失败", blocked is False, f"(rc={blocked})")
        staged = git(repo, "diff", "--cached", "--name-only").stdout.strip()
        check("add 失败后索引被清干净（不留半暂存）", staged == "",
              f"(staged={staged!r})")
        # 失败只动索引，工作区内容一分不动。
        left = git(repo, "status", "--porcelain", "--", "ops/tasks/lanes.json").stdout
        check("失败后台账仍在工作区（没被清掉）", "lanes.json" in left,
              f"(status={left.strip()!r})")

    if failures:
        print(f"\ntruth sync ignored views unit: FAIL ({len(failures)})")
        return 1
    print("\ntruth sync ignored views unit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
