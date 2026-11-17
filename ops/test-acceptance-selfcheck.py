#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：验收管家必须先自己验一遍，才有资格来敲用户的门。

为什么要有这条门禁：
  用户明确要求「他自己必须先验收完了一遍，然后剩下的需要用户验收的地方，
  再截图提醒用户验收」。此前只要登记一条 `needs_human=true`，它立刻出现在
  面板的「需要你验收」里——不管机器有没有跑过任何东西。于是用户可以登记
  一个空验收项就把活推给他，等于让他当第一道测试。

本门禁锁三件事：
  1. 人工验收项**没有** evidence → 进「待自验」，不进「需要你验收」；
  2. 人工验收项**有** evidence → 才算自验完，进「需要你验收」；
  3. 机器自证项不受影响：needs_human=false 的 verified 仍在「已机器验收」。

第 1 条是反向断言：把闸门去掉、只要 needs_human 就上屏，本门禁必须变红。

退出码 = 未通过项数。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))

failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}" + (f"（{detail}）" if detail else ""))


def main():
    with tempfile.TemporaryDirectory(prefix="autopilot-accept-") as tmp:
        repo = Path(tmp)
        truth = repo / "ops" / "truth"
        truth.mkdir(parents=True)
        rows = [
            # 有自验证据：够格来敲用户的 门。
            {"id": "A-0001", "type": "acceptance", "title": "已自验的人工项",
             "status": "todo", "needs_human": True,
             "steps": "1) 打开面板 2) 看第一屏",
             "evidence": "panel-render: python3 ops/panel_render_check.py -> exit 0",
             "screenshots": ["/tmp/first.png", "/tmp/second.png"]},
            # 没有自验证据：必须留在「待自验」。
            {"id": "A-0002", "type": "acceptance", "title": "还没自验的人工项",
             "status": "todo", "needs_human": True, "steps": "1) 打开面板"},
            # 只有空白也算没自验。
            {"id": "A-0003", "type": "acceptance", "title": "证据是空白的",
             "status": "wip", "needs_human": True, "evidence": "   "},
            # 机器自证：不进人工那两堆。
            {"id": "A-0004", "type": "acceptance", "title": "机器自证项",
             "status": "verified", "needs_human": False,
             "evidence": "selftest: python3 ops/selftest.py -> exit 0"},
        ]
        (truth / "acceptance.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8")
        (truth / "ledger.jsonl").write_text("", encoding="utf-8")

        old = os.environ.get("KG_REPO")
        os.environ["KG_REPO"] = str(repo)
        try:
            import central_mcp
            board = central_mcp._acceptance_board()
        finally:
            if old is None:
                os.environ.pop("KG_REPO", None)
            else:
                os.environ["KG_REPO"] = old

        human = {r["id"] for r in board["human"]}
        self_check = {r["id"] for r in board["self_check"]}

        check("有自验证据的人工项才进「需要你验收」", human == {"A-0001"}, human)
        check("没有自验证据的人工项留在「待自验」",
              self_check == {"A-0002", "A-0003"}, self_check)
        check("空白证据不算自验", "A-0003" in self_check and "A-0003" not in human)
        check("待自验项不混进等你验收", not (human & self_check), (human, self_check))
        check("待自验计数如实", board["self_check_count"] == 2, board["self_check_count"])
        check("机器自证项仍归机器那堆",
              [r["id"] for r in board["machine"]] == ["A-0004"], board["machine"])
        check("自验证据原样带给面板",
              "panel_render_check" in (board["human"][0].get("evidence") or ""),
              board["human"][0])
        check("人工验收截图原样带给面板",
              board["human"][0].get("screenshots") == ["/tmp/first.png", "/tmp/second.png"],
              board["human"][0].get("screenshots"))

    print()
    if failures:
        print(f"acceptance_selfcheck 门禁：{len(failures)} 项未通过")
        for f in failures:
            print("  - " + f)
        return len(failures)
    print("acceptance_selfcheck 门禁：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
