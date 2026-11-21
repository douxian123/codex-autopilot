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
import types
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
        (truth / "pre_acceptance.jsonl").write_text(
            "".join(json.dumps({
                "check_id": check_id,
                "status": "pass",
                "evidence_types": evidence_types,
                "summary": "预验收证据已确认",
            }, ensure_ascii=False) + "\n" for check_id, evidence_types in (
                ("engineering", ["machine", "backend", "integration", "rollback"]),
                ("frontend", ["browser", "screenshot", "interaction", "responsive"]),
                ("comprehension", ["comprehension", "user_walkthrough"]),
                ("alignment", ["prd", "expectations", "acceptance", "risk"]),
            )), encoding="utf-8")

        old = os.environ.get("KG_REPO")
        os.environ["KG_REPO"] = str(repo)
        # 这个临时仓库要一直活到 MCP 边界测试结束：补图工具在 call_tool 里
        # 重新读验收台账，早还原就会把「已自验」误判成「未自验」。
        import central_mcp
        board = central_mcp._acceptance_board()

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

        # MCP 调用口四道边界：没有显式确认不截；验收项不存在不截；
        # 未自验项不截；只有已自验且待人工复核、且人点了确认，才追加附件。
        # 这里注入假截图器与假 truthctl，绝不在门禁里调用系统截图或写现场台账。
        calls = []
        events = []
        fake_shot = types.ModuleType("acceptance_screenshot")
        fake_shot.capture = lambda aid: {"path": "/tmp/fake-acceptance-%s.png" % aid}
        old_shot = sys.modules.get("acceptance_screenshot")
        old_truthctl = central_mcp.truthctl
        old_append = central_mcp.control_state.append_event
        sys.modules["acceptance_screenshot"] = fake_shot
        central_mcp.truthctl = lambda *a, **kw: (
            calls.append((a, kw)) or ("appended", 0))
        central_mcp.control_state.append_event = lambda *a, **kw: events.append((a, kw))
        try:
            out, code = central_mcp.call_tool(
                "capture_acceptance_screenshot", {"id": "A-0001"})
            check("补图必须显式 confirm=true",
                  code == 2 and out.get("code") == "SCREENSHOT_CONFIRM_REQUIRED", out)
            out, code = central_mcp.call_tool(
                "capture_acceptance_screenshot", {"confirm": True})
            check("补图必须有验收 id",
                  code == 2 and out.get("code") == "SCREENSHOT_ID_REQUIRED", out)
            out, code = central_mcp.call_tool(
                "capture_acceptance_screenshot", {"id": "A-9999", "confirm": True})
            check("不存在的验收项不补图",
                  code == 2 and out.get("code") == "SCREENSHOT_ACCEPTANCE_NOT_FOUND", out)
            out, code = central_mcp.call_tool(
                "capture_acceptance_screenshot", {"id": "A-0002", "confirm": True})
            check("未自验的人工项不补图",
                  code == 2 and out.get("code") == "SCREENSHOT_NOT_HUMAN_READY", out)
            check("拒绝路径不调用截图器", not calls and not events, (calls, events))

            out, code = central_mcp.call_tool(
                "capture_acceptance_screenshot", {"id": "A-0001", "confirm": True})
            check("人点确认后才追加截图",
                  code == 0 and out.get("id") == "A-0001"
                  and out.get("path") == "/tmp/fake-acceptance-A-0001.png", out)
            check("补图保持原验收状态，不替人收口",
                  out.get("status") == "todo", out)
            check("补图只走 truthctl 追加，不改状态",
                  len(calls) == 1 and calls[0][0] == (
                      "accept", "--id", "A-0001", "--status", "todo")
                  and calls[0][1].get("extra_env", {}).get("KG_TRUTH_SHOTS")
                  == "/tmp/fake-acceptance-A-0001.png", calls)
            check("补图写入事件流水",
                  len(events) == 1 and events[0][0][0] == "acceptance_screenshot", events)
        finally:
            central_mcp.truthctl = old_truthctl
            central_mcp.control_state.append_event = old_append
            if old_shot is None:
                sys.modules.pop("acceptance_screenshot", None)
            else:
                sys.modules["acceptance_screenshot"] = old_shot

        if old is None:
            os.environ.pop("KG_REPO", None)
        else:
            os.environ["KG_REPO"] = old

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
