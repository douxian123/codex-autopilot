#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：切换主干后恢复真相快照，不得让旧文件覆盖 main 上新落盘的内容。

事故证据（2026-11-17）：`e5294880 chore(truth): sync state before landing`
把 `docs/PRODUCT_MAINLINE.md` 的生成时间从 2026-11-11 22:13:16 回退到
2026-10-13 21:42:52，同时删掉几十条已落盘的路线、决策与验收记录。
根因是 `_restore_truth_files()` 对非 append-only 真相路径直接 `write_bytes`，
把切换前抓到的陈旧工作副本当成权威覆盖切换后的 main 内容。

本门禁锁死四类真相的恢复语义：
  1. 生成视图按生成时间取新的一条；
  2. JSONL 做行级并集，旧快照不能删掉 main 上已落盘记录；
  3. 结构化 JSON 按 `updated` 取新的一侧，并保留另一侧独有键；
  4. 未知真相路径仍按旧快照恢复，不改变未分类行为。
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
ROOT = OPS.parent
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


def load_kgctl(root: Path):
    os.environ["KG_REPO"] = str(root)
    os.environ["KG_TRUNK"] = str(root)
    spec = importlib.util.spec_from_file_location("kgctl_restore_freshness", OPS / "kgctl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="restore-truth-freshness-") as raw:
        root = Path(raw)
        for rel in ("docs", "ops/truth", "ops/tasks", "ops/context"):
            (root / rel).mkdir(parents=True, exist_ok=True)
        (root / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
        kg = load_kgctl(root)

        old_view = (
            "# 产品主线（自动综合，勿手改）\n\n"
            "> 生成时间 2026-10-13 21:42:52\n\n"
            "OLD ROADMAP\n"
        )
        new_view = (
            "# 产品主线（自动综合，勿手改）\n\n"
            "> 生成时间 2026-11-11 22:13:16\n\n"
            "NEW ROADMAP\n- 浏览器插件首发\n"
        )
        old_progress = '{"id":"P-0001","status":"done"}\n'
        new_progress = (
            old_progress
            + '{"id":"P-0002","status":"done"}\n'
            + '{"id":"P-0003","status":"done"}\n'
        )
        old_mainline = '{"updated":"2026-10-13","current_focus":["old"],"old_only":1}\n'
        new_mainline = '{"updated":"2026-11-17","current_focus":["new"],"new_only":2}\n'
        old_binary = b"old-local-route-result"

        snapshot = {
            "docs/PRODUCT_MAINLINE.md": old_view.encode("utf-8"),
            "ops/truth/progress.jsonl": old_progress.encode("utf-8"),
            "ops/truth/mainline.json": old_mainline.encode("utf-8"),
            "ops/context/local.bin": old_binary,
        }
        (root / "docs/PRODUCT_MAINLINE.md").write_text(new_view, encoding="utf-8")
        (root / "ops/truth/progress.jsonl").write_text(new_progress, encoding="utf-8")
        (root / "ops/truth/mainline.json").write_text(new_mainline, encoding="utf-8")

        kg._restore_truth_files(root, snapshot)

        restored_view = (root / "docs/PRODUCT_MAINLINE.md").read_text(encoding="utf-8")
        check("旧生成视图不覆盖 main 上更新的视图",
              "NEW ROADMAP" in restored_view and "OLD ROADMAP" not in restored_view,
              f"(view={restored_view!r})")

        restored_progress = (root / "ops/truth/progress.jsonl").read_text(encoding="utf-8")
        check("旧 JSONL 快照不删掉 main 新增记录",
              all(f'"P-000{i}"' in restored_progress for i in (1, 2, 3)),
              f"(progress={restored_progress!r})")
        check("旧 JSONL 快照没有重复写入已有记录",
              restored_progress.count('"P-0001"') == 1,
              f"(progress={restored_progress!r})")

        import json
        restored_mainline = json.loads(
            (root / "ops/truth/mainline.json").read_text(encoding="utf-8"))
        check("结构化真相保留 main 的较新时间",
              restored_mainline.get("updated") == "2026-11-17",
              f"(mainline={restored_mainline!r})")
        check("结构化真相保留 main 的较新内容",
              restored_mainline.get("current_focus") == ["new"],
              f"(mainline={restored_mainline!r})")
        check("结构化真相保留快照独有键",
              restored_mainline.get("old_only") == 1,
              f"(mainline={restored_mainline!r})")
        check("未知二进制路径仍从快照恢复",
              (root / "ops/context/local.bin").read_bytes() == old_binary)

    if failures:
        print(f"\nrestore truth snapshot freshness: FAIL ({len(failures)})")
        return 1
    print("\nrestore truth snapshot freshness: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
