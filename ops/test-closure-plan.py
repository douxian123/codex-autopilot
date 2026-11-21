#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""施工计划门禁：正向完整 + 反向拒绝无门禁、越级施工和假进度。"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

OPS = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("closure_plan_under_test",
                                              OPS / "closure_plan.py")
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"  ok   {label}")


def must_fail(label, mutate):
    bad = copy.deepcopy(mod.load())
    mutate(bad)
    check(label, bool(mod.validate(bad)), "反向用例没有被拦住")


def main():
    data = mod.load()
    check("施工计划可读", bool(data))
    check("正向施工计划完整", mod.validate(data) == [], str(mod.validate(data)))
    check("九阶段顺序固定", [s["id"] for s in data["stages"]] == list(mod.STAGE_IDS))
    check("每阶段都有真门禁",
          all("python3 " in s["gate"] for s in data["stages"]))

    must_fail("删掉一个施工阶段会红", lambda d: d["stages"].pop())
    must_fail("依赖前指会红",
              lambda d: d["stages"][1].update(depends_on=["e2e_closure"]))
    must_fail("阶段缺门禁会红", lambda d: d["stages"][2].update(gate=""))
    must_fail("阶段缺证据会红", lambda d: d["stages"][3].update(evidence=""))
    must_fail("阶段状态非法会红", lambda d: d["stages"][4].update(status="done-ish"))
    must_fail("进度估算缺口径会红",
              lambda d: d["progress_estimates"].pop("口径"))
    must_fail("缺完成分层会红", lambda d: d.pop("completion_layers"))
    must_fail("虚报 5/5 可以对外会红",
              lambda d: d["progress_estimates"].update(
                  {"对外发布就绪度": "产品评估 5/5，可以对外"}))
    must_fail("发布地基越级标 done 会红",
              lambda d: d["stages"][1].update(status="done"))

    print("closure-plan: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
