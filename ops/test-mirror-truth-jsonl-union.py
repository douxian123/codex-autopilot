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
"""

import importlib.util
import os
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

    if failures:
        print(f"\nmirror truth jsonl union: FAIL ({len(failures)})")
        return 1
    print("\nmirror truth jsonl union: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
