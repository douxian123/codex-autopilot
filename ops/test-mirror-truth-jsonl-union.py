#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：land 镜像状态根账本时，JSONL 不得整文件覆盖主干已落盘记录。

事故证据（2026-11-18）：状态根的 P-0080 关闭记录整文件覆盖了 main 的 P-0079
land 记录，本轮 A-0006 验收记录又覆盖了上一轮 A-0006。两次都只能从旧 commit
考古，再手工用新 ID 补回。本门禁锁死：
  1. trunk 已有 JSONL 行必须保留；
  2. 状态根新增 JSONL 行必须补进 trunk；
  3. 同 id 不同 ts 的两个版本都保留，且较新 ts 排在最后；
  4. 非 JSONL 真相文件仍按整文件镜像，不改变既有行为。
  5. sync_trunk_truth 暂存前也必须与 HEAD 的 JSONL 并集，否则 land 前
     的旧状态根仍会把已 done 的台账整文件覆盖回 open。
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


def load_kgctl(state_root: Path):
    os.environ["KG_REPO"] = str(state_root)
    spec = importlib.util.spec_from_file_location("kgctl_mirror_union", OPS / "kgctl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="mirror-truth-union-") as raw:
        base = Path(raw)
        state, trunk = base / "state", base / "trunk"
        for root in (state, trunk):
            for rel in ("ops/truth", "ops/tasks", "ops/context", "docs"):
                (root / rel).mkdir(parents=True, exist_ok=True)

        old_land = '{"id":"P-0079","ts":"2026-11-18 09:06:24","title":"old land","landed":true}\n'
        new_close = '{"id":"P-0080","ts":"2026-11-18 09:30:42","title":"close L-0006","status":"done"}\n'
        old_acc = '{"id":"A-0006","ts":"2026-11-18 09:06:24","title":"old acceptance"}\n'
        new_acc = '{"id":"A-0006","ts":"2026-11-18 09:50:39","title":"new acceptance"}\n'
        state_progress = new_close
        trunk_progress = old_land
        state_acc = new_acc
        trunk_acc = old_acc

        (state / "ops/truth/progress.jsonl").write_text(state_progress, encoding="utf-8")
        (state / "ops/truth/acceptance.jsonl").write_text(state_acc, encoding="utf-8")
        (trunk / "ops/truth/progress.jsonl").write_text(trunk_progress, encoding="utf-8")
        (trunk / "ops/truth/acceptance.jsonl").write_text(trunk_acc, encoding="utf-8")

        state_task = '{"id":"T-0001","ts":"2026-11-18 09:00:00","text":"state task"}\n'
        trunk_task = '{"id":"T-0000","ts":"2026-11-18 08:00:00","text":"trunk task"}\n'
        (state / "ops/tasks/inbox.jsonl").write_text(state_task, encoding="utf-8")
        (trunk / "ops/tasks/inbox.jsonl").write_text(trunk_task, encoding="utf-8")

        (state / "ops/truth/product_state.json").write_text(
            '{"updated":"2026-11-18","area":"state"}\n', encoding="utf-8")
        (trunk / "ops/truth/product_state.json").write_text(
            '{"updated":"2026-11-01","area":"trunk"}\n', encoding="utf-8")

        kg = load_kgctl(state)
        kg._mirror_truth_into_trunk(trunk)

        progress = (trunk / "ops/truth/progress.jsonl").read_text(encoding="utf-8")
        check("主干已落盘 P-0079 不被状态根 P-0080 覆盖",
              old_land in progress and new_close in progress, repr(progress))
        check("progress 镜像后没有重复写入同一行",
              progress.count(old_land.strip()) == 1
              and progress.count(new_close.strip()) == 1,
              repr(progress))

        acc = (trunk / "ops/truth/acceptance.jsonl").read_text(encoding="utf-8")
        check("同 id 不同 ts 的 A-0006 两个版本都保留",
              old_acc in acc and new_acc in acc, repr(acc))
        check("较新 ts 的 A-0006 排在最后",
              acc.index(new_acc.strip()) > acc.index(old_acc.strip()), repr(acc))

        inbox = (trunk / "ops/tasks/inbox.jsonl").read_text(encoding="utf-8")
        check("ops/tasks JSONL 也做行级并集",
              trunk_task in inbox and state_task in inbox, repr(inbox))

        product = (trunk / "ops/truth/product_state.json").read_text(encoding="utf-8")
        check("非 JSONL 真相文件仍按整文件镜像",
              '"area":"state"' in product and '"area":"trunk"' not in product,
              repr(product))

    with tempfile.TemporaryDirectory(prefix="truth-sync-union-") as raw:
        base = Path(raw)
        state, trunk = base / "state", base / "state-trunk"
        state.mkdir()
        for rel in ("ops/truth", "ops/tasks", "ops/context", "docs"):
            (state / rel).mkdir(parents=True)
        (state / "ops" / "central.config.json").write_text(
            '{"build":{"cmd":"","cwd":"."}}\n', encoding="utf-8")

        old_open = ('{"id":"L-0002","ts":"2026-11-17 05:41:22",'
                    '"status":"open"}\n')
        done = ('{"id":"L-0002","ts":"2026-11-18 11:16:14",'
                '"status":"done"}\n')
        (state / "ops/truth/ledger.jsonl").write_text(old_open + done, encoding="utf-8")
        (state / "ops/truth/progress.jsonl").write_text("", encoding="utf-8")

        git(state, "init", "-q", "-b", "main")
        git(state, "config", "user.email", "union@example.invalid")
        git(state, "config", "user.name", "Union Gate")
        git(state, "add", ".")
        git(state, "commit", "-q", "-m", "init")
        git(state, "worktree", "add", "-q", "--detach", str(trunk), "main")

        # 旧状态根带来的陈旧副本：只有 open，没有 HEAD 上已落盘的 done。
        (trunk / "ops/truth/ledger.jsonl").write_text(old_open, encoding="utf-8")
        kg = load_kgctl(state)
        ok = kg.sync_trunk_truth(trunk, "sync stale ledger")
        # 并集后如果已经与 HEAD 一致，sync 返回 False（没有新提交）是正确结果；
        # 这里只要求它不许把 HEAD 的 done 从工作区抹掉。
        check("sync_trunk_truth 未因陈旧台账失败", ok in (True, False), repr(ok))

        work_ledger = (trunk / "ops/truth/ledger.jsonl").read_text(encoding="utf-8")
        check("sync 后工作区 done 记录不被 open 覆盖",
              old_open.strip() in work_ledger and done.strip() in work_ledger,
              repr(work_ledger))
        check("工作区较新 ts 的 done 排在最后",
              work_ledger.index(done.strip()) > work_ledger.index(old_open.strip()),
              repr(work_ledger))

        committed = git(trunk, "show", "HEAD:ops/truth/ledger.jsonl")
        check("sync 前 HEAD 的 done 记录不被 open 覆盖",
              old_open.strip() in committed and done.strip() in committed,
              repr(committed))
        check("sync 后较新 ts 的 done 排在最后",
              committed.index(done.strip()) > committed.index(old_open.strip()),
              repr(committed))

    if failures:
        print(f"\nmirror truth jsonl union: FAIL ({len(failures)})")
        return 1
    print("\nmirror truth jsonl union: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
