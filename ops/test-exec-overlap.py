#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：并发施工项的「文件占地」判据必须挡重复活、放行不相关的活。

为什么要有这条门禁：
  worktree 隔离解决了「两个 worker 同时写同一个目录」，没解决「两个 worker 做
  同一件事」。两条需求都声明改 `ops/central_daemon.py` 时，各自改一版、各自
  落盘，必有一条白干，或者变成一个要人来拆的语义冲突。这是用户明确交给项目经理
  的判断：「要判断用户的重复项……让它们的修改不会相互冲突和覆盖。」

这里锁住五个容易腐化的边界：
  1. 真重叠 → 拦截，且说清撞了哪几条、哪几个文件
  2. 不重叠 → 放行
  3. 没有文件线索 → **不做判断**（既不假拦，也不假放）
  4. 自己 vs 自己 → 不算重叠（否则一条需求永远等自己）
  5. reason 固定为 overlap_hold，守护靠它每拍自愈

退出码 = 未通过项数。
"""

import sys
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))

import exec_overlap  # noqa: E402

failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}" + (f"（{detail}）" if detail else ""))


def main():
    item = {"id": "L-0002", "title": "修面板卡住",
            "files": ["ops/central_daemon.py", "plugin/ui/panel.html"]}

    hold = exec_overlap.overlap_hold(
        item, [("L-0001", ["ops/central_daemon.py"])])
    check("重叠时拦截", hold is not None, hold)
    check("reason 固定为 overlap_hold",
          (hold or {}).get("reason") == exec_overlap.REASON_OVERLAP_HOLD, hold)
    check("说清等谁", [h["id"] for h in (hold or {}).get("holders") or []]
          == ["L-0001"], hold)
    check("只说公共文件，不印整份声明",
          (hold or {}).get("files") == ["ops/central_daemon.py"], hold)
    check("带上标题便于面板说人话", (hold or {}).get("title") == "修面板卡住", hold)

    check("不重叠时放行",
          exec_overlap.overlap_hold(
              item, [("L-0001", ["docs/README.md"])]) is None)
    check("持有者没有文件线索时不误拦",
          exec_overlap.overlap_hold(item, [("L-0001", [])]) is None)
    check("新条目没有文件线索时不猜",
          exec_overlap.overlap_hold(
              {"id": "L-0003", "title": "只有一句话"},
              [("L-0001", ["ops/central_daemon.py"])]) is None)
    check("自己不算自己的持有者",
          exec_overlap.overlap_hold(
              item, [("L-0002", ["ops/central_daemon.py"])]) is None)

    # 路径归一化：「./a」和「/a」是同一条路径，否则会漏拦。
    check("./ 与前导 / 归一后仍能比较",
          exec_overlap.overlap_hold(
              {"id": "x", "files": ["./ops/a.py"]},
              [("y", ["/ops/a.py"])]) is not None)
    check("空字符串不算文件线索",
          exec_overlap.declared_files({"files": ["", "  ", "/"]}) == [])
    check("files 写成字符串也认",
          exec_overlap.declared_files({"files": "ops/a.py"}) == ["ops/a.py"])
    check("非字典输入不炸",
          exec_overlap.declared_files(None) == []
          and exec_overlap.overlap_hold(None, [("y", ["a"])]) is None)

    print()
    if failures:
        print(f"exec_overlap 门禁：{len(failures)} 项未通过")
        for f in failures:
            print("  - " + f)
        return len(failures)
    print("exec_overlap 门禁：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
