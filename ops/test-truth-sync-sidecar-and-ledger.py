#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：真相同步不得被数据库伴随文件卡死，也不得被陈旧台账删章节。

为什么有这条（2026-11-16 守护重启后第一拍实测）：

  守护跑起来后，tick 的 `[7/8] commit + push truth state` 每一拍都被拒：

      主干工作树有非真相层改动，拒绝同步真相层：…/knowledge-graph
       D data/task-L0031.db-wal
       D data/task-resolve-hover-card.db-wal
       M docs/OCT29_PROGRESS_RADAR.generated.md

  三个文件没有一个是人造改动：
    * 两条 `data/*.db-wal` 是 SQLite 运行时伴随文件，历史上被误提交进版本库，
      数据库一 checkpoint 就在工作区留下一条 ` D`——它既不是真相层也永远不会自己
      恢复，于是真相层从此再也推不上 main；
    * `docs/OCT29_PROGRESS_RADAR.generated.md` 由 `backend/scripts/build-progress-radar.mjs`
      生成（文件头写明「勿手改」），是生成视图，本来就该算真相层。

  同时 supersede 也每一拍失败：trunk 工作树切不回 main，因为两份追加型台账
  （`docs/AGENT_SYNC.md` / `docs/COCKPIT_BRIEF.md`）相对 HEAD 有改动会被 checkout
  覆盖。那份工作副本比 main 少 10318 / 10616 行，是**旧的截断快照**；如果这里改成
  「直接 checkout -- 丢掉」，就等于把 main 的完整章节删掉。

本门禁锁死三件事：
  1. 运行时伴随文件存在时，真相同步照常进行，且伴随文件**不被提交**；
  2. 工作副本是 HEAD 真子集时，同步后 HEAD 里的章节一个都不能少，工作副本新增的
     条目也要保留；
  3. 真代码改动仍然 fail-closed：不许被 `chore(truth)` 吞掉。
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


def load_kgctl(repo, trunk):
    os.environ["KG_REPO"] = str(repo)
    os.environ["KG_TRUNK"] = str(trunk)
    spec = importlib.util.spec_from_file_location("kgctl_truth_sync", OPS / "kgctl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def seed(root: Path):
    repo = root / "repo"
    for d in ("ops/truth", "ops/tasks", "ops/context", "docs", "data"):
        (repo / d).mkdir(parents=True)
    (repo / "ops" / "truth" / "progress.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "truth" / "ledger.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "truth" / "acceptance.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "truth" / "decisions.jsonl").write_text("", encoding="utf-8")
    # 刻意不造 ops/truth/benchmark-contract-report.json：那份 36 题报告有固定真实哈希，
    # 造假副本会（正确地）被 _ensure_truth_baselines fail-closed 拦下，与本条门禁无关。
    # 文件缺失时该判据保持旧行为（放行），正是通用项目该走的路径。
    (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
    (repo / "docs" / "PROJECT_STATUS.md").write_text("# status\n", encoding="utf-8")
    # 已被误提交进版本库的伴随文件 + 生成视图，就是现场那三条。
    (repo / "data" / "task-L0031.db-wal").write_text("stale-wal\n", encoding="utf-8")
    (repo / "docs" / "OCT29_PROGRESS_RADAR.generated.md").write_text(
        "生成时间：old\n", encoding="utf-8")
    committed = "## 已落盘 A\n\nA 正文\n\n## 已落盘 B\n\nB 正文\n"
    (repo / "docs" / "COCKPIT_BRIEF.md").write_text(committed, encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def sections(text: str) -> set:
    return {ln for ln in text.splitlines() if ln.startswith("## ")}


def main():
    with tempfile.TemporaryDirectory(prefix="truth-sync-sidecar-") as raw:
        root = Path(raw)
        repo = seed(root)
        # kgctl 在**导入时**就解析状态根并对不上就退出，所以必须先把真实临时仓建好。
        kg = load_kgctl(repo, root / "unused-trunk")
        kg.REPO = repo
        kg.MAIN = "main"

        # 伴随文件残留（被 checkout 掉）——现场就是这条 ` D`。
        (repo / "data" / "task-L0031.db-wal").unlink()
        # tick 会重写生成视图，这是真相层改动，应当被提交。
        (repo / "docs" / "OCT29_PROGRESS_RADAR.generated.md").write_text(
            "生成时间：new\n", encoding="utf-8")
        # 追加型台账的工作副本是「旧的截断版」，还多了本地新条目。
        (repo / "docs" / "COCKPIT_BRIEF.md").write_text(
            "## 已落盘 B\n\nB 正文\n\n## 本地新条目 C\n\nC 正文\n", encoding="utf-8")

        # ---- 1. 伴随文件不再挡住真相同步 ----
        out = kg.sync_trunk_truth(repo, "sync from tick")
        check("伴随文件存在时真相同步仍然成功", out is True, f"(rc={out})")

        # ---- 2. 伴随文件绝不被这次同步动到 ----
        # 注意：现场里它**本来就是**被跟踪的（历史上误提交进版本库），所以 `ls-files`
        # 必然非空，不能拿它当判据。真正的不变量是「这次 chore(truth) 提交里没有它」。
        changed = git(repo, "show", "--name-only", "--format=", "HEAD").stdout
        check("这次同步提交里不含伴随文件",
              "data/task-L0031.db-wal" not in changed, f"(changed={changed.strip()!r})")
        left = git(repo, "status", "--porcelain", "--", "data/task-L0031.db-wal").stdout
        check("伴随文件的删除仍留在工作区（没有被伪装成已处理）", left.strip().startswith("D"),
              f"(status={left.strip()!r})")

        # ---- 3. 陈旧台账不得删掉已落盘章节 ----
        head = git(repo, "show", "HEAD:docs/COCKPIT_BRIEF.md").stdout
        got = sections(head)
        check("已落盘章节 A 仍在", "## 已落盘 A" in got, f"(sections={sorted(got)})")
        check("已落盘章节 B 仍在", "## 已落盘 B" in got)
        check("本地新增条目 C 被保留", "## 本地新条目 C" in got, f"(sections={sorted(got)})")

        # ---- 4. 生成视图算真相层，被提交 ----
        radar = git(repo, "show", "HEAD:docs/OCT29_PROGRESS_RADAR.generated.md").stdout
        check("生成视图被纳入真相层并被提交", "new" in radar, f"(radar={radar!r})")

        # ---- 5. 真代码改动仍然 fail-closed ----
        (repo / "backend").mkdir(exist_ok=True)
        (repo / "backend" / "app.ts").write_text("export const x = 1;\n", encoding="utf-8")
        git(repo, "add", "backend/app.ts")
        (repo / "ops" / "tasks" / "inbox.jsonl").write_text(
            '{"id":"L-0001"}\n', encoding="utf-8")
        before = git(repo, "rev-parse", "HEAD").stdout.strip()
        blocked = kg.sync_trunk_truth(repo, "sync from tick")
        after = git(repo, "rev-parse", "HEAD").stdout.strip()
        check("夹带代码改动时仍然拒绝同步", blocked is False, f"(rc={blocked})")
        check("拒绝时不产生 chore(truth) 提交", before == after,
              f"({before[:8]} -> {after[:8]})")
        status = git(repo, "status", "--porcelain").stdout
        check("代码改动没被吞掉", "backend/app.ts" in status, f"(status={status.strip()[:120]!r})")
    if failures:
        print(f"\ntruth sync sidecar + ledger unit: FAIL ({len(failures)})")
        return 1
    print("\ntruth sync sidecar + ledger unit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
