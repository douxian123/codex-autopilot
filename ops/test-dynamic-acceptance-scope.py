#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""动态验收范围生成器回归。"""

import dynamic_acceptance_scope as scope


def check(label, condition):
    if not condition:
        raise AssertionError(label)
    print("  PASS", label)


def main():
    backend = scope.generate_scope(
        text="修改内部 API 的数据写入逻辑", kind="bug",
        files=["backend/src/write.ts"], product_shape=["backend_service"])
    check("后端任务选择工程验收", backend["selected_checks"] == ["engineering"])
    check("后端任务不强制前端验收", "frontend" in backend["not_applicable"])
    check("无未映射风险时可直接进入执行", backend["status"] == "ready")

    visual = scope.generate_scope(
        text="调整面板按钮位置和空状态", kind="visual",
        files=["frontend/src/Panel.tsx", "frontend/src/panel.css"],
        product_shape=["web_app"])
    check("视觉任务选择工程、前端和理解性验收",
          set(visual["selected_checks"]) == {"engineering", "frontend", "comprehension"})
    check("视觉任务识别 UI 影响", "ui" in visual["impact_tags"])

    production = scope.generate_scope(
        text="发布生产版本并增加租户权限", kind="gate",
        files=["backend/auth.ts", "ops/deploy.sh"],
        product_shape=["production_service"], delivery_goal="production_release")
    check("生产任务识别部署和安全风险",
          {"deployment", "security", "authorization"} <= set(production["impact_tags"]))
    check("未实现的发布安全门禁不会假装通过", production["status"] == "needs_review")
    check("未映射风险被明确列出", len(production["unmapped_risks"]) >= 2)

    docs = scope.generate_scope(
        text="更新使用说明", kind="documentation", files=["README.md"],
        product_shape=["documentation"], delivery_goal="reviewable")
    check("纯文档变更可以没有预验收项", docs["selected_checks"] == [])
    check("纯文档变更空集合不制造未知风险", docs["status"] == "ready")
    print("dynamic-acceptance-scope: PASS")


if __name__ == "__main__":
    main()
