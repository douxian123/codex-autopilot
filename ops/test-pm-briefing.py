#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：PM 简报只读聚合预期、让路、追问候选，绝不把候选变成动作。

为什么要有这条门禁：
  用户把 PM 管家描述成「确认预期、打磨需求、规划最优施工路线、自动追击进度」，
  但自动挡此前把这些信号散在 constraints / native_routes / exec_blocked 各自的
  台账里。人得自己拼起来，等于 PM 没在工作。简报如果只是「再加一张卡片」也很
  容易腐化成两个坑：取不到数据时印 0，或者生成追问候选后顺手自动发消息。

本门禁锁住五个边界：
  1. 台账缺失 -> unknown，不说「没有候选」；
  2. 待确认预期、生效约束、主线阶段原样进简报；
  3. overlap_hold 明示「正常排队，不计入卡住」；
  4. 长时间无进展的已绑定工作项只生成追问候选；
  5. compute 不写台账、不发消息，候选只给人看。

退出码 = 未通过项数。
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))

import pm_briefing  # noqa: E402

failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}" + (f"（{detail}）" if detail else ""))


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")


def main():
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    old = (now - timedelta(minutes=90)).strftime("%Y-%m-%d %H:%M:%S")
    old_env = os.environ.get("KG_REPO")

    with tempfile.TemporaryDirectory(prefix="autopilot-pm-brief-") as tmp:
        repo = Path(tmp)
        truth = repo / "ops" / "truth"
        truth.mkdir(parents=True)
        os.environ["KG_REPO"] = str(repo)

        blank = pm_briefing.compute(repo, now=now)
        check("台账缺失时整体 unknown", blank["status"] == "unknown", blank)
        check("台账缺失时预期 unknown",
              blank["expectations"]["status"] == "unknown"
              and blank["expectations"]["pending_count"] is None,
              blank["expectations"])
        check("台账缺失时追问 unknown",
              blank["follow_up_candidates"]["status"] == "unknown",
              blank["follow_up_candidates"])
        check("让路信号缺失不假装没有排队",
              blank["overlap_holds"]["status"] == "unknown"
              and "取不到" in blank["overlap_holds"]["note"],
              blank["overlap_holds"])

        write_jsonl(truth / "constraints.jsonl", [{
            "id": "C-0001", "status": "active",
            "statement": "验收前必须先自验", "scope": ["ops/central_mcp.py"],
        }])
        write_jsonl(truth / "constraint_candidates.jsonl", [{
            "id": "CC-0001", "status": "proposed", "kind": "taboo",
            "text": "绝不影响用户正在用的服务",
        }])
        (truth / "mainline.json").write_text(json.dumps({
            "stage": "架构闭环", "current_focus": ["PM 简报", "验收截图"],
        }, ensure_ascii=False), encoding="utf-8")
        write_jsonl(truth / "ledger.jsonl", [{
            "id": "L-0021", "status": "doing", "title": "等待用户复核的施工项", "ts": old,
        }])
        write_jsonl(truth / "native_routes.jsonl", [{
            "ledger_id": "L-0021", "status": "dispatched",
            "thread_id": "thread-abc", "ts": old,
        }])
        before = {
            p.name: p.read_bytes()
            for p in truth.glob("*.jsonl")
        }
        runtime = {"blocked": {"exec_waiting": [{
            "id": "L-0021", "title": "撞文件的后开工项",
            "files": ["ops/truthctl.py"],
            "holders": [{"id": "L-0020", "files": ["ops/truthctl.py"]}],
        }]}}

        brief = pm_briefing.compute(repo, runtime=runtime, now=now)
        exp = brief["expectations"]
        check("预期聚合 ok", exp["status"] == "ok", exp)
        check("待确认预期进简报",
              exp["pending_count"] == 1 and "绝不影响" in exp["pending"][0]["text"], exp)
        check("生效约束进简报", exp["active_count"] == 1
              and "先自验" in exp["active"][0]["text"], exp)
        check("主线阶段进简报", exp["mainline_stage"] == "架构闭环", exp)
        check("PM 感知到待确认预期", brief["status"] == "attention", brief)

        hold = brief["overlap_holds"]
        check("让路信号进简报", hold["status"] == "ok" and len(hold["items"]) == 1, hold)
        check("让路写清等谁与撞哪些文件",
              hold["items"][0]["holders"][0]["id"] == "L-0020"
              and "ops/truthctl.py" in hold["items"][0]["files"], hold)
        check("让路明确不是卡住", "不计入卡住" in hold["note"], hold)

        follow = brief["follow_up_candidates"]
        check("无进展工作项只生成候选", follow["status"] == "ok"
              and [r["id"] for r in follow["items"]] == ["L-0021"], follow)
        check("候选带真实线程绑定", follow["items"][0]["thread_id"] == "thread-abc", follow)
        check("候选明确是建议不是动作", "不会自动追问" in follow["note"], follow)

        after = {p.name: p.read_bytes() for p in truth.glob("*.jsonl")}
        check("PM 简报不写任何台账", before == after, (before, after))

        fresh_ts = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        write_jsonl(truth / "ledger.jsonl", [{
            "id": "L-0021", "status": "doing", "title": "等待用户复核的施工项",
            "ts": fresh_ts,
        }])
        write_jsonl(truth / "native_routes.jsonl", [{
            "ledger_id": "L-0021", "status": "dispatched",
            "thread_id": "thread-abc", "ts": fresh_ts,
        }])
        fresh = pm_briefing.compute(repo, runtime=runtime, now=now)
        check("刚更新的工作项不误报追问",
              fresh["follow_up_candidates"]["items"] == [],
              fresh["follow_up_candidates"])

    if old_env is None:
        os.environ.pop("KG_REPO", None)
    else:
        os.environ["KG_REPO"] = old_env

    print()
    if failures:
        print(f"pm_briefing 门禁：{len(failures)} 项未通过")
        for f in failures:
            print("  - " + f)
        return len(failures)
    print("pm_briefing 门禁：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
