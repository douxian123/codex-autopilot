#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""根据当前产品和变更生成最小充分的预验收范围。

范围生成是纯函数：只提出本次需要验证的类别，不执行门禁、不写台账。
当前没有独立执行器的风险必须显式进入 ``unmapped_risks``，不能被基础门禁
伪装成已验证。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


SUPPORTED = {"engineering", "frontend", "comprehension", "alignment", "evidence",
             "security", "deployment", "ai_output"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _texts(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return []
    return [_text(value) for value in values if _text(value)]


def _has(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _has_ai_token(text: str) -> bool:
    """只把独立 AI 词当成输出质量风险，不让 mail/detail/main 误触发。"""
    return re.search(r"(?<![a-z])ai(?![a-z])", text) is not None


def infer_impact(text: str, kind: str, files: list[str]) -> list[str]:
    """从任务线索推导影响标签；未知影响不降级为低风险。"""
    haystack = " ".join([text, kind, *files]).lower()
    impacts: set[str] = set()
    if _has(haystack, ("页面", "面板", "前端", "视觉", "样式", "交互", "按钮",
                       "tsx", "jsx", "css", "html")):
        impacts |= {"ui", "interaction"}
    if _has(haystack, ("接口", "api", "后端", "服务", "请求", "数据库",
                       "sqlite", "prisma", "迁移")):
        impacts |= {"api_contract", "business_rule"}
    if _has(haystack, ("权限", "登录", "账号", "租户", "敏感", "token", "密钥")):
        impacts |= {"authorization", "security"}
    if _has(haystack, ("上线", "发布", "部署", "生产", "灰度", "回滚")):
        impacts.add("deployment")
    if _has(haystack, ("性能", "并发", "慢", "延迟", "大数据", "内存")):
        impacts.add("performance")
    if _has_ai_token(haystack) or _has(haystack, ("模型", "prompt", "引用", "来源", "生成")):
        impacts.add("ai_output")
    return sorted(impacts)


def generate_scope(*, text: str = "", kind: str = "", files: list[str] | None = None,
                   product_shape: list[str] | None = None,
                   delivery_goal: str = "acceptance_ready",
                   explicit_impacts: list[str] | None = None,
                   expected_version: str = "") -> dict[str, Any]:
    """生成 acceptance_scope.json 内容，不产生任何副作用。"""
    files = _texts(files)
    shape = _texts(product_shape)
    impacts = sorted(set(_texts(explicit_impacts)) |
                     set(infer_impact(text, kind, files)))
    selected: set[str] = set()
    reasons: dict[str, str] = {}
    code_like = any(Path(path).suffix.lower() in {
        ".py", ".js", ".jsx", ".ts", ".tsx", ".css", ".html", ".vue",
        ".go", ".rs", ".java"
    } for path in files)
    non_behavioral_kinds = {"documentation", "design_artifact", "prototype", "copy_only"}
    behavioral = (bool(code_like) or _text(kind).lower() not in non_behavioral_kinds) and (
        bool(_text(text)) or bool(_text(kind)) or code_like)
    if behavioral:
        selected.add("engineering")
        reasons["engineering"] = "本次存在产品行为、代码或工程逻辑变化"

    if {"ui", "interaction"} & set(impacts) or any(
        name in {"web_app", "single_page_frontend", "browser_extension", "mobile_app"}
        for name in shape
    ):
        selected |= {"frontend", "comprehension"}
        reasons["frontend"] = "存在用户界面或交互影响，需要真实界面验证"
        reasons["comprehension"] = "用户需要在真实入口完成或理解本次变化"

    if _has(" ".join([text, kind, delivery_goal]).lower(),
            ("验收", "预期", "需求", "用户", "发布", "上线", "release", "production")):
        selected.add("alignment")
        reasons["alignment"] = "本次交付涉及用户承诺、验收目标或发布目标"

    # 证据审计是横切门禁：任何非空验收包都必须能证明版本、来源和现场，
    # 不能因为本次只改后端或只改前端就把 provenance 关掉。
    if selected:
        selected.add("evidence")
        reasons["evidence"] = "所有适用验收项都必须具备当前版本和可复核来源证据"

    if {"authorization", "security"} & set(impacts):
        selected.add("security")
        reasons["security"] = "存在权限、账号、密钥或敏感数据影响，需要真实安全与授权证据"
    if "deployment" in impacts:
        selected.add("deployment")
        reasons["deployment"] = "存在发布、灰度、回滚或生产影响，需要部署和健康检查证据"
    if "ai_output" in impacts:
        selected.add("ai_output")
        reasons["ai_output"] = "存在模型、提示、引用或生成输出影响，需要事实性和来源证据"

    unmapped: list[str] = []
    if "performance" in impacts:
        unmapped.append("performance：当前预验收执行器没有性能基线和负载门禁")

    return {
        "schema": "acceptance-scope/v1",
        "status": "ready" if not unmapped else "needs_review",
        "expected_version": _text(expected_version),
        "product_shape": shape,
        "delivery_goal": _text(delivery_goal) or "acceptance_ready",
        "change": {"kind": _text(kind), "text": _text(text), "files": files},
        "impact_tags": impacts,
        "selected_checks": sorted(selected),
        "selection_reasons": reasons,
        "not_applicable": {
            item: "当前产品或本次变更没有该类用户/系统影响"
            for item in sorted(SUPPORTED - selected)
        },
        "unmapped_risks": unmapped,
        "policy": "只评估 selected_checks；unmapped_risks 必须补充对应门禁或明确风险处理。",
    }

