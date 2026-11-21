#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动挡产品预验收字典。

这里定义的是“什么叫通过”，而不是一组建议文案。每个条目都有稳定 ID、适用
范围、严重级别、通过/失败条件和证据类型。`pre_acceptance` 只负责读取结果，
不替任何门禁伪造结果。
"""

from __future__ import annotations

from typing import Any


def _item(item_id: str, category: str, title: str, scope: str, method: str,
          evidence: tuple[str, ...], *, severity: str = "blocker",
          required: bool = True, source: str = "autopilot:product-pre-acceptance") -> dict[str, Any]:
    return {
        "id": item_id,
        "category": category,
        "title": title,
        "scope": scope,
        "method": method,
        "severity": severity,
        "required": required,
        "evidence_required": evidence,
        "pass_condition": f"{title}已在真实当前版本中完成，并有对应{method}证据。",
        "fail_condition": f"{title}缺失、失败、只在fixture中成立，或证据无法关联当前版本。",
        "source": source,
    }


def _group(category: str, prefix: str, scope: str, method: str,
           names: tuple[str, ...], evidence: tuple[str, ...], *,
           source: str = "autopilot:product-pre-acceptance") -> list[dict[str, Any]]:
    return [_item(f"{prefix}-{n:03d}", category, title, scope, method, evidence,
                  source=source) for n, title in enumerate(names, 1)]


ENGINEERING = _group("engineering", "ENG", "backend", "工程与后端", (
    "需求、范围和完成定义已锁定", "实现已落盘且能从当前提交定位", "编译或类型检查通过",
    "相关单元测试通过", "相关集成测试通过", "真实入口可达", "成功路径真实可用",
    "错误路径有明确结果", "数据写入后可真实回读", "权限边界正确", "重试行为可控且幂等",
    "停止或回滚路径可用", "并发冲突不会覆盖用户修改", "不会影响用户当前工作进程",
    "证据包含版本、命令、cwd和退出码"), ("command", "result", "version"))

FRONTEND = _group("frontend", "FE", "frontend", "真实浏览器", (
    "真实首屏加载完成", "页面展示真实数据而非fixture", "桌面首屏无遮挡和裁切", "窄屏无横向溢出",
    "文本完整落在父容器内", "动态内容不会造成布局跳动", "视觉层级符合用户注意力顺序",
    "字号、行高和间距可读", "色彩对比度和状态表达合格", "圆角、边界和密度符合设计系统",
    "首屏不展示worker、lease、heartbeat等工程噪音", "关键结果和操作容易找到",
    "空态说明当前状态和下一步", "错误态说明原因和恢复路径", "成功态有明确反馈",
    "loading不会遮挡或无限等待", "禁用态不会误导为可操作", "hover、focus、active、disabled完整",
    "键盘Tab顺序和Escape行为正常", "reduced motion不会破坏体验", "关键按钮真实可点击",
    "输入、菜单、弹窗和跳转真实闭环", "刷新后状态保持", "后退或重新打开状态正确",
    "pageerror为零", "console error为零", "关键API无未处理4xx或5xx", "页面结果与后端状态一致",
    "截图覆盖默认态和关键状态", "截图能让小白定位复核动作", "重要状态不只靠颜色表达",
    "页面职责和入口不重复", "信息密度适合重复工作", "没有明显视觉噪音和空转动效"),
    ("browser_assertion", "screenshot", "console_clean", "version"),
    source="knowledge-graph:真实浏览器/视觉验收共性规范")

COMPREHENSION = _group("comprehension", "UX", "fullstack", "小白用户走查", (
    "第一次用户能说出当前状态", "第一次用户能说出系统完成了什么", "第一次用户能说出下一步",
    "第一次用户知道是否需要介入", "首屏不要求用户理解内部术语", "不需要读代码或日志才能使用",
    "入口命名符合用户心智", "首次路径能按步骤完成", "关键错误能自助恢复或明确求助",
    "用户不会误以为机器替他完成了人工判断", "截图和实际页面一致", "能区分完成、进行中、待确认和失败",
    "重要信息位于首屏注意力区域", "次要信息不会抢主任务注意力", "说明面向用户而非内部实现"),
    ("user_walkthrough", "screenshot", "plain_language"),
    source="knowledge-graph:用户走查/读者面降噪共性规范")

ALIGNMENT = _group("alignment", "PRD", "fullstack", "产品对齐审查", (
    "当前PRD版本明确", "需求原话和来源可追溯", "已确认约束已加载", "相关用户反馈已处理或标记不适用",
    "验收标准已确认", "非目标已明确", "新能力已完成竞品或开源研究", "adopt、adapt、build、defer或reject决策已记录",
    "不确定项保持unknown或进入ask_user", "实现路线与产品目标一致", "结果回写产品状态",
    "需求变化生成PRD修订候选", "没有证据的产品结论不写成完成", "质量问题生成返修或重规划项"),
    ("prd", "decision", "traceability"))

EVIDENCE = _group("evidence", "EVD", "fullstack", "证据审计", (
    "每个必检项都有证据", "证据能关联工程项和验收项", "证据有时间和版本", "证据含命令、cwd、退出码或现场记录",
    "截图是当前版本真实产物", "截图覆盖关键状态", "失败证据不会被覆盖", "unknown不计为pass",
    "证据不是fixture-only", "发布、提交和测试对象一致", "用户入口可以重复验证",
    "发布动作与工程完成分离", "人工验收只接收预验收后的剩余项", "验收失败可重开并保留历史"),
    ("evidence", "version", "provenance"))

ITEMS = tuple(ENGINEERING + FRONTEND + COMPREHENSION + ALIGNMENT + EVIDENCE)
BY_ID = {item["id"]: item for item in ITEMS}
CATEGORIES = ("engineering", "frontend", "comprehension", "alignment", "evidence")


def applicable(scope: str, requested: str | None) -> list[dict[str, Any]]:
    """按工程项范围选择标准；未知范围不默认为只测后端。"""
    if not requested or requested == "fullstack":
        return list(ITEMS)
    if requested not in {"backend", "frontend", "config", "docs"}:
        return list(ITEMS)
    return [item for item in ITEMS if item["scope"] in (requested, "fullstack")]


def evaluate_item(spec: dict[str, Any], row: dict[str, Any] | None) -> dict[str, Any]:
    """严格判定一条标准，避免空字段、fixture或旧版本证据伪装通过。"""
    row = row or {}
    state = str(row.get("status") or row.get("state") or "unknown").strip().lower()
    if state in {"na", "not-applicable", "not_applicable"}:
        state = "not_applicable"
    evidence = row.get("evidence") or row.get("evidence_types") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    evidence = [x for x in evidence if isinstance(x, str) and x.strip()]
    reason = str(row.get("reason") or "").strip()
    missing = sorted(set(spec["evidence_required"]) - set(evidence))
    version_ok = bool(str(row.get("version") or row.get("commit") or "").strip())
    fixture_only = bool(row.get("fixture_only")) or any("fixture" in x.lower() for x in evidence)
    if state == "not_applicable" and not reason:
        state = "unknown"
        reason = "not_applicable 必须提供理由"
    elif state == "pass" and (missing or not version_ok or fixture_only):
        state = "unknown"
        reason = reason or ("缺少证据：" + ", ".join(missing or ["version"]))
    elif state not in {"pass", "fail", "blocked", "unknown", "not_applicable"}:
        state = "unknown"
        reason = "未知状态不能作为通过"
    return {
        "id": spec["id"], "category": spec["category"], "title": spec["title"],
        "state": state, "scope": spec["scope"], "severity": spec["severity"],
        "required": spec["required"], "method": spec["method"],
        "pass_condition": spec["pass_condition"], "fail_condition": spec["fail_condition"],
        "evidence_required": list(spec["evidence_required"]), "evidence": evidence,
        "missing_evidence": missing, "reason": reason or str(row.get("summary") or ""),
        "version": row.get("version") or row.get("commit") or "",
        "source": spec["source"],
    }
