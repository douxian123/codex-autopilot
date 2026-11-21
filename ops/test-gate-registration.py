#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""元门禁：`ops/test-*.py` 里的每一条都必须被总闸 `selftest.py` 点名执行。

为什么需要这条（2026-11-17 实测）：
  `selftest.py` 是**显式点名**跑门禁的，没有 glob 自动发现。于是很容易出现
  「门禁文件写了、也随发版进了版本目录，但总闸从来不跑它」——文件在、绿灯在、
  回归保护不在。审计当时 40 条 `ops/test-*.py` 里有 5 条从未被点名，其中就
  包括当天刚加的 `test-reap-detached-head-guard.py`（detached HEAD 回收判据）。

  这类缺口靠人记性防不住，只能靠门禁防：新增一条 `ops/test-*.py` 而忘了登记，
  这条元门禁就红，而不是安静地让那条门禁变成摆设。

反向自我保护：本文件自身也必须登记在 selftest.py 里（下面会一并检查），否则
元门禁自己就成了漏网的那一条。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

OPS = Path(__file__).resolve().parent
SELFTEST = OPS / "selftest.py"


def registered_gate_names(selftest_source: str) -> set[str]:
    """把 selftest.py 里出现的 `test-*.py` 字符串常量全部收集出来。

    用 AST 而不是子串匹配：子串匹配会把注释里提一句也算「已登记」，而那种
    「只在注释里提过」的门禁实际上从没被执行 —— 正是我们要抓的假登记。
    """
    tree = ast.parse(selftest_source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if text.startswith("test-") and text.endswith(".py"):
                names.add(text)
    return names


def main() -> int:
    selftest_source = SELFTEST.read_text(encoding="utf-8")
    registered = registered_gate_names(selftest_source)

    gate_files = sorted(p.name for p in OPS.glob("test-*.py"))
    missing = [name for name in gate_files if name not in registered]

    if missing:
        print("以下门禁文件存在，但 selftest.py 从未点名执行它们：", file=sys.stderr)
        for name in missing:
            print(f"  - {name}", file=sys.stderr)
        print(
            "\n把它们加进 selftest.py 的执行清单（或删除文件）。"
            "不登记 = 发了不跑 = 回归保护是假的。",
            file=sys.stderr,
        )
        return 1

    # 反向：不能登记一个根本不存在的文件（改名后忘更新清单会制造同样的假绿灯）。
    phantom = sorted(name for name in registered if not (OPS / name).is_file())
    if phantom:
        print("selftest.py 点名了并不存在的门禁文件：", file=sys.stderr)
        for name in phantom:
            print(f"  - {name}", file=sys.stderr)
        return 1

    print(f"gate registration unit: PASS（{len(gate_files)} 条门禁全部已登记）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
