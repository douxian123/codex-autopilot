#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收字典的正向和反向门禁。"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acceptance_dictionary as d


def check(label, condition):
    if not condition:
        raise AssertionError(label)
    print("  PASS", label)


def main():
    check("字典包含五类标准", set(d.CATEGORIES) == {
        "engineering", "frontend", "comprehension", "alignment", "evidence"})
    check("每条标准有稳定字段", all({"id", "title", "scope", "severity", "required",
                                       "pass_condition", "fail_condition", "evidence_required"} <= set(x)
                                      for x in d.ITEMS))
    spec = d.BY_ID["FE-001"]
    good = d.evaluate_item({**spec}, {"status": "pass",
        "evidence": list(spec["evidence_required"]), "version": "abc123"})
    check("完整当前版本证据才 pass", good["state"] == "pass")
    check("缺证据不能 pass", d.evaluate_item(spec, {"status": "pass", "version": "abc123"})["state"] == "unknown")
    check("没有版本不能 pass", d.evaluate_item(spec, {"status": "pass",
        "evidence": list(spec["evidence_required"])})["state"] == "unknown")
    check("fixture-only不能 pass", d.evaluate_item(spec, {"status": "pass",
        "evidence": ["fixture-only", *spec["evidence_required"]], "version": "abc123"})["state"] == "unknown")
    check("失败不能被证据覆盖", d.evaluate_item(spec, {"status": "fail",
        "evidence": list(spec["evidence_required"]), "version": "abc123"})["state"] == "fail")
    check("不适用必须有理由", d.evaluate_item(spec, {"status": "not_applicable"})["state"] == "unknown")
    check("有理由才允许不适用", d.evaluate_item(spec, {"status": "not_applicable",
        "reason": "本工程无前端", "version": "abc123"})["state"] == "not_applicable")
    check("未知范围不默认放宽", len(d.applicable("x", "unknown")) == len(d.ITEMS))
    print("acceptance-dictionary: PASS")


if __name__ == "__main__":
    main()
