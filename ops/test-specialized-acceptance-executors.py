#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""动态验收专项执行器回归。

锁定三件事：专项风险被选中、专项证据齐全才可 ready、无执行器的风险仍阻塞。
"""

from pathlib import Path
import tempfile

import acceptance_dictionary
import dynamic_acceptance_scope
import pre_acceptance


def check(label, condition):
    if not condition:
        raise AssertionError(label)
    print("  PASS", label)


def record_all(root, categories, version="specialized-commit"):
    for spec in acceptance_dictionary.ITEMS:
        if spec["category"] not in categories:
            continue
        pre_acceptance.record(
            root, spec["category"], "pass", list(spec["evidence_required"]),
            item_id=spec["id"], version=version,
            summary=spec["title"] + " 已由专项证据确认",
        )


def main():
    check("专项类别已注册", {"security", "deployment", "ai_output"} <= set(pre_acceptance._CHECK_IDS))
    check("专项字典有稳定编号", all(
        any(spec["category"] == category for spec in acceptance_dictionary.ITEMS)
        for category in ("security", "deployment", "ai_output")))

    scope = dynamic_acceptance_scope.generate_scope(
        text="发布带租户权限的模型输出面板", kind="release",
        files=["ops/deploy.sh", "backend/auth.ts", "frontend/Panel.tsx"],
        product_shape=["web_app", "production_service"],
        delivery_goal="production_release")
    selected = set(scope["selected_checks"])
    check("三类专项进入当前最小充分范围",
          {"security", "deployment", "ai_output"} <= selected)
    check("专项不再伪装为未映射风险",
          not any(any(name in risk for name in ("security", "deployment", "ai_output"))
                  for risk in scope["unmapped_risks"]))

    with tempfile.TemporaryDirectory(prefix="autopilot-specialized-acceptance-") as tmp:
        root = Path(tmp)
        specialized_scope = {
            "status": "ready",
            "selected_checks": ["security", "deployment", "ai_output"],
            "unmapped_risks": [],
            "expected_version": "specialized-commit",
        }
        missing = pre_acceptance.evaluate(root, specialized_scope)
        check("专项缺逐项证据时不通知用户", missing["notify_user"] is False)
        check("专项缺逐项证据时状态不是 ready", missing["status"] != "ready")

        record_all(root, {"security", "deployment", "ai_output", "evidence"})
        ready = pre_acceptance.evaluate(root, specialized_scope)
        check("专项证据齐全后 ready", ready["status"] == "ready")
        check("ready 后才允许通知用户验收", ready["notify_user"] is True)
        check("专项没有漏进默认基础范围",
              not ({"security", "deployment", "ai_output"} & set(pre_acceptance._DEFAULT_SCOPE)))

        pre_acceptance.record(
            root, "security", "pass",
            acceptance_dictionary.BY_ID["SEC-001"]["evidence_required"],
            item_id="SEC-001", version="old-commit", summary="旧版本安全证据")
        stale = pre_acceptance.evaluate(root, specialized_scope)
        check("旧版本证据不能让专项 ready", stale["status"] != "ready")
        check("旧版本证据不通知用户", stale["notify_user"] is False)

        unmapped = {"status": "ready", "selected_checks": ["security"],
                    "unmapped_risks": ["performance：当前没有性能门禁"],
                    "expected_version": "specialized-commit"}
        blocked = pre_acceptance.evaluate(root, unmapped)
        check("无执行器的风险仍阻断", blocked["status"] == "blocked")
        check("无执行器风险不通知用户", blocked["notify_user"] is False)
        check("未映射风险数量可审计", blocked["unmapped_risk_count"] == 1)

    print("specialized-acceptance-executors: PASS")


if __name__ == "__main__":
    main()
