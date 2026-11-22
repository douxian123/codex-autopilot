#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：land 遇到主干非真相脏改动时，必须直接改用一次性干净 worktree。"""

import ast
from pathlib import Path

OPS = Path(__file__).resolve().parent
SOURCE = (OPS / "kgctl.py").read_text(encoding="utf-8")
tree = ast.parse(SOURCE)
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label} {detail}")


def cmd_land_source():
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_land":
            return node
    raise AssertionError("cmd_land not found")


def calls(node, name):
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == name]


def main():
    fn = cmd_land_source()
    src = ast.get_source_segment(SOURCE, fn) or ""
    aligns = calls(fn, "_align_main")
    clean = calls(fn, "_clean_land_worktree")
    trunk_calls = calls(fn, "_trunk_worktree")
    status_calls = calls(fn, "trunk_status")
    dirty_assigns = [n for n in ast.walk(fn)
                     if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "dirty_trunk"
                             for t in n.targets)]
    pre_dirty_reject = [n for n in ast.walk(fn)
                        if isinstance(n, ast.Constant)
                        and isinstance(n.value, str)
                        and "非真相层改动，改用一次性干净落盘工作树" in n.value]
    late_reject = [n for n in ast.walk(fn)
                   if isinstance(n, ast.Constant)
                   and isinstance(n.value, str)
                   and "非真相层改动，拒绝 land" in n.value]

    check("脏主干判断字段存在",
          len(dirty_assigns) >= 1 and len(trunk_calls) >= 1
          and len(status_calls) >= 2)
    check("首次 _align_main 前已读取主干状态",
          trunk_calls and aligns
          and trunk_calls[0].lineno < aligns[0].lineno
          and status_calls[0].lineno < aligns[0].lineno
          and dirty_assigns[0].lineno < aligns[0].lineno,
          f"(trunk={trunk_calls[0].lineno} status={status_calls[0].lineno} "
          f"dirty={dirty_assigns[0].lineno} align={aligns[0].lineno})")
    check("脏分支创建干净 worktree，不再拒绝",
          len(clean) == 1 and len(pre_dirty_reject) == 1
          and len(late_reject) == 1,
          f"(clean={[c.lineno for c in clean]} pre={len(pre_dirty_reject)} late={len(late_reject)})")
    check("同步后仍保留兜底检查",
          len(status_calls) >= 2 and status_calls[-1].lineno > clean[0].lineno,
          f"(status={[c.lineno for c in status_calls]} clean={[c.lineno for c in clean]})")

    if failures:
        print(f"\nland dirty trunk clean worktree unit: FAIL ({len(failures)})")
        return 1
    print("\nland dirty trunk clean worktree unit: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
