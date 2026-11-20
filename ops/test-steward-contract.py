#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""职责契约门禁：正向完整 + 反向拒绝职责漂移与假绿灯。"""

from __future__ import annotations

import copy
import importlib.util
import json
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("steward_contract_under_test",
                                              OPS / "steward_contract.py")
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"  ok   {label}")


def must_fail(label, mutate):
    bad = copy.deepcopy(mod.load())
    mutate(bad)
    errors = mod.validate(bad)
    check(label, bool(errors), "反向用例没有被拦住")


def main():
    data = mod.load()
    check("契约可读", bool(data), "")
    check("正向契约完整", mod.validate(data) == [], str(mod.validate(data)))
    ids = [r["id"] for r in data["roles"]]
    check("七个管家不增不减", len(ids) == 7 and len(set(ids)) == 7, str(ids))
    check("全局健康是层不是管家",
          data["global_health"]["kind"] == "layer"
          and data["global_health"]["id"] not in ids, "")
    check("三条语义视图不改职责数",
          len(data["semantic_views"]) == 3 and len(data["roles"]) == 7, "")

    must_fail("删掉一个管家会红", lambda d: d["roles"].pop())
    must_fail("新增第八个管家会红",
              lambda d: d["roles"].append(copy.deepcopy(d["roles"][0])))
    must_fail("缺人工边界会红",
              lambda d: d["roles"][0].pop("human_boundary"))
    must_fail("把全局健康写成管家会红",
              lambda d: d["global_health"].update(kind="steward", id="status"))
    must_fail("语义视图新增独立职责会红",
              lambda d: d["semantic_views"][0]["composed_of"].append("new_bot"))
    must_fail("把 unknown 写成 0 会红",
              lambda d: d.update(unknown_policy="取不到就写 0"))
    must_fail("人的拍板边界被扩大会红",
              lambda d: d["human_decision_types"].append("auto_publish"))
    must_fail("首屏三问被扩成一屏问题会红",
              lambda d: d["first_screen_questions"].append("今天天气"))
    must_fail("次入口删掉上下文档案会红",
              lambda d: d["secondary_entries"].remove("上下文档案"))
    must_fail("职责只写状态不写缺口会红",
              lambda d: d["roles"][0].update(gap=""))

    with tempfile.TemporaryDirectory(prefix="steward-contract-") as tmp:
        broken = Path(tmp) / "broken.json"
        broken.write_text("{", encoding="utf-8")
        try:
            mod.load(broken)
        except (ValueError, json.JSONDecodeError):
            print("  ok   契约损坏时 fail closed")
        else:
            raise AssertionError("损坏契约没有 fail closed")

    print("steward-contract: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
