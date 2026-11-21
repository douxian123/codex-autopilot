#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成本纪律门禁：token-friendly 不能变成牺牲交付质量的借口。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

OPS = Path(__file__).resolve().parent
REPO = OPS.parent


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"  ok   {label}")


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, OPS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    contract = (REPO / "docs" / "PRODUCT_CONTRACT.md").read_text(encoding="utf-8")
    for needle in ("成本纪律：token-friendly，不以质量换省钱",
                   "必留上下文 + 可判定验收 + 人工边界 = 不可裁剪",
                   "预算状态只有 `ok / blocked / unknown`"):
        check("PRD 写清成本边界：" + needle, needle in contract, needle)

    closure = load("cost_closure_package_under_test", "closure_package.py")
    package = {
        "service_result": "可用", "entrypoint": "面板", "activation": "可见",
        "machine_acceptance": [{"name": "gate", "cmd": "python3 ops/selftest.py",
                                "cwd": ".", "pass_condition": "exit 0"}],
        "real_usage_acceptance": [{"step": "打开面板", "pass_condition": "可见"}],
        "human_boundary": {"status": "not_required", "why": "已覆盖"},
        "rollback": "保留现场", "writeback": [{"target": "ops/truth/progress.jsonl"}],
        "cost_strategy": {"mode": "summary_first", "cache": "scope + 文件指纹",
                          "on_over_budget": "block_and_report",
                          "note": "不删必留上下文、不跳验收"},
    }
    check("完整成本策略可过施工包校验",
          closure.missing_fields(package) == [], closure.validate(package))
    unsafe = dict(package)
    unsafe["cost_strategy"] = {"mode": "summary_first", "cache": "scope",
                               "on_over_budget": "ignore", "note": "省钱优先"}
    check("省钱优先但不报告的伪策略被拒",
          "cost_strategy" in closure.missing_fields(unsafe), closure.validate(unsafe))

    daemon = load("cost_central_daemon_under_test", "central_daemon.py")
    prompt = daemon._exec_prompt({"id": "L-9001", "kind": "gate", "title": "成本纪律"})
    for needle in ("成本纪律", "scope + 文件指纹", "未命中才增量读取",
                   "不得删除目标、必留上下文、可判定验收、人工边界、回滚或真实入口证据",
                   "预算不足只停新派活并报告", "不中断在途收尾"):
        check("执行 prompt 保留质量边界：" + needle, needle in prompt, needle)

    budget_src = (OPS / "budget_gate.py").read_text(encoding="utf-8")
    for needle in ("只停「派活」", "不丢在途工作", "fail-open"):
        check("预算闸门保留在途工作：" + needle, needle in budget_src, needle)

    print("cost-discipline: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
