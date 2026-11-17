#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：全局项目健康必须说真话，且不能把正常调度当成病。

为什么要有这条门禁：
  用户要的不是「再加一张卡片」，而是有人为整个项目的健康负责。判断必须来自
  已有台账，不能另造一套分数。这里锁住六个容易腐化的边界：

  1. 主线过期 → attention，并说清过期多少天；
  2. 约束台账为空 → attention（用户的预期还没沉淀）；
  3. 标了 done 却没证据 → attention（不能拿「已完成」冒充闭环）；
  4. 真相目录读不到 → unknown，绝不降级成「正常」；
  5. `overlap_hold` 排队让路 → 只报信号，不把整体抬成 attention；
  6. 待自验但自动实现关着 → attention（没人会去跑，是真实卡点）。

退出码 = 未通过项数。
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))

import project_health  # noqa: E402

failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}" + (f"（{detail}）" if detail else ""))


def snapshot(**over):
    base = {
        "mainline_present": True,
        "mainline_updated": "2026-11-16",
        "mainline_stage": "内部可用",
        "open_items": 2,
        "done_no_evidence": 0,
        "constraints": 3,
        "decisions": 1,
        "judgments": 1,
        "acceptance_total": 2,
        "human_ready": 0,
        "self_check": 0,
        "verified": 1,
    }
    base.update(over)
    return base


def layer(res, key):
    for row in res.get("layers") or []:
        if row.get("key") == key:
            return row
    return {}


def main():
    now = datetime(2026, 11, 18, tzinfo=timezone.utc)

    fresh = project_health.evaluate(
        snapshot(mainline_updated=now.strftime("%Y-%m-%d")),
        runtime={"auto_exec": True}, now=now)
    check("全部新鲜时整体 ok", fresh["level"] == "ok", fresh)
    check("ok 时三层齐全",
          [r["key"] for r in fresh["layers"]] == ["truth", "flow", "accept"],
          fresh["layers"])

    stale = project_health.evaluate(
        snapshot(mainline_updated=(now - timedelta(days=35)).strftime("%Y-%m-%d")),
        runtime={"auto_exec": True}, now=now)
    check("主线过期抬成 attention", stale["level"] == "attention", stale)
    check("主线过期说清多少天",
          "35 天" in " ".join(layer(stale, "truth")["reasons"]),
          layer(stale, "truth"))
    check("过期只报一条标题，不搬整段台账",
          "35 天" in stale["headline"], stale["headline"])

    empty = project_health.evaluate(snapshot(constraints=0),
                                    runtime={"auto_exec": True}, now=now)
    check("约束台账为空抬成 attention", empty["level"] == "attention", empty)
    check("约束为空说人话",
          "约束台账 0 条" in " ".join(layer(empty, "truth")["reasons"]),
          layer(empty, "truth"))

    no_ev = project_health.evaluate(snapshot(done_no_evidence=2),
                                    runtime={"auto_exec": True}, now=now)
    check("完成但没证据抬成 attention", no_ev["level"] == "attention", no_ev)
    check("完成但没证据给条数",
          "2 条" in " ".join(layer(no_ev, "truth")["reasons"]),
          layer(no_ev, "truth"))

    unknown = project_health.evaluate(None)
    check("真相读不到是 unknown", unknown["level"] == "unknown", unknown)
    check("unknown 不伪装正常",
          "取不到" in unknown["headline"], unknown["headline"])

    waiting = project_health.evaluate(
        snapshot(mainline_updated=now.strftime("%Y-%m-%d")),
        runtime={"auto_exec": True, "overlap_waiting": 2}, now=now)
    check("overlap 排队不抬整体级别", waiting["level"] == "ok", waiting)
    check("overlap 排队仍可见",
          "2 条在排队让路" in " ".join(layer(waiting, "flow")["reasons"]),
          layer(waiting, "flow"))

    stuck = project_health.evaluate(
        snapshot(mainline_updated=now.strftime("%Y-%m-%d")),
        runtime={"auto_exec": True, "exec_stuck": 1}, now=now)
    check("真卡住的实现抬成 attention", stuck["level"] == "attention", stuck)

    selfcheck = project_health.evaluate(
        snapshot(mainline_updated=now.strftime("%Y-%m-%d"), self_check=2),
        runtime={"auto_exec": False}, now=now)
    check("待自验但自动实现关着是 attention",
          selfcheck["level"] == "attention", selfcheck)
    check("待自验说清没人会跑",
          "没人会去跑" in " ".join(layer(selfcheck, "accept")["reasons"]),
          layer(selfcheck, "accept"))

    with tempfile.TemporaryDirectory(prefix="autopilot-health-") as tmp:
        repo = Path(tmp)
        truth = repo / "ops" / "truth"
        truth.mkdir(parents=True)
        (truth / "mainline.json").write_text(json.dumps({
            "product": "demo", "updated": now.strftime("%Y-%m-%d"),
            "stage": "内部可用",
        }), encoding="utf-8")
        (truth / "constraints.jsonl").write_text(json.dumps({
            "id": "C-0001", "status": "active", "statement": "非破坏性",
        }) + "\n", encoding="utf-8")
        (truth / "ledger.jsonl").write_text(json.dumps({
            "id": "L-0001", "status": "done",
            "title": "没写证据就标完成",
        }) + "\n", encoding="utf-8")
        real = project_health.compute(str(repo), runtime={"auto_exec": True}, now=now)
        check("真实目录读得到并给出 attention",
              real["level"] == "attention" and real["signals"]["open_items"] == 0,
              real)

        (truth / "constraints.jsonl").unlink()
        (truth / "ledger.jsonl").unlink()
        missing_ledgers = project_health.compute(
            str(repo), runtime={"auto_exec": True}, now=now)
        check("约束台账缺失不说成 0 条",
              missing_ledgers["signals"]["constraints"] is None,
              missing_ledgers["signals"])
        check("问题台账缺失不说成 0 条",
              missing_ledgers["signals"]["open_items"] is None,
              missing_ledgers["signals"])
        check("台账缺失抬成 unknown",
              missing_ledgers["level"] == "unknown"
              and layer(missing_ledgers, "truth")["state"] == "unknown",
              missing_ledgers)

        (truth / "mainline.json").unlink()
        missing = project_health.compute(str(repo), runtime={}, now=now)
        check("mainline 缺失是 unknown", missing["level"] == "unknown", missing)

    print("")
    if failures:
        print(f"project_health: {len(failures)} 项未通过")
    else:
        print("project_health: 全部通过")
    return len(failures)


if __name__ == "__main__":
    sys.exit(main())
