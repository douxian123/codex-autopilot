#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""七管家职责契约的机器读取与校验（stdlib only）。"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "docs" / "steward_contract.json"

ROLE_IDS = {
    "context",
    "pm",
    "acceptance",
    "routing",
    "implementation",
    "merge",
    "status",
}
LIFECYCLE_FIELDS = (
    "trigger",
    "input",
    "output",
    "machine_gate",
    "user_entry",
    "human_boundary",
    "history",
)
VALID_STATUS = {"implemented", "partial", "missing"}


def load(path=None):
    """读取契约；JSON 损坏或缺失是错误，不返回空契约。"""
    target = Path(path) if path else CONTRACT
    return json.loads(target.read_text(encoding="utf-8"))


def _missing_fields(entry):
    return [key for key in LIFECYCLE_FIELDS
            if not isinstance(entry.get(key), str) or not entry[key].strip()]


def validate(data):
    """返回全部错误；空列表表示职责契约完整。"""
    errors = []
    if not isinstance(data, dict):
        return ["契约根节点必须是对象"]

    roles = data.get("roles")
    if not isinstance(roles, list):
        return ["roles 必须是数组"]
    ids = [r.get("id") for r in roles if isinstance(r, dict)]
    if len(ids) != 7 or set(ids) != ROLE_IDS:
        errors.append("必须是七个固定管家，且不能增删或重名")
    if len(ids) != len(set(ids)):
        errors.append("管家 id 不能重复")

    for role in roles:
        if not isinstance(role, dict):
            errors.append("管家条目必须是对象")
            continue
        rid = role.get("id", "?")
        errors.extend(f"{rid}: 缺字段 {key}" for key in _missing_fields(role))
        if role.get("plane") not in ("project_truth", "execution"):
            errors.append(f"{rid}: plane 必须属于两个平面之一")
        if role.get("status") not in VALID_STATUS:
            errors.append(f"{rid}: status 只能是 implemented/partial/missing")
        if not isinstance(role.get("gap"), str) or not role["gap"].strip():
            errors.append(f"{rid}: 必须写出真实缺口，不能只写状态")

    health = data.get("global_health")
    if not isinstance(health, dict):
        errors.append("必须有全局健康层")
    else:
        errors.extend(f"global_health: 缺字段 {key}"
                      for key in _missing_fields(health))
        if health.get("id") in ROLE_IDS:
            errors.append("全局健康不能算作第八个管家")
        if health.get("kind") != "layer":
            errors.append("全局健康的 kind 必须是 layer")
        if health.get("status") not in VALID_STATUS:
            errors.append("global_health: status 非法")
        if not isinstance(health.get("gap"), str) or not health["gap"].strip():
            errors.append("global_health: 必须写出真实缺口")

    views = data.get("semantic_views")
    if not isinstance(views, list) or len(views) != 3:
        errors.append("必须有三个语义视图：预期、判断、决策")
    else:
        allowed = ROLE_IDS | {"global_health", "human"}
        names = [v.get("name") for v in views if isinstance(v, dict)]
        if set(names) != {"预期", "判断", "决策"}:
            errors.append("语义视图名称必须是预期、判断、决策")
        for view in views:
            if not isinstance(view, dict):
                errors.append("语义视图必须是对象")
                continue
            composed = view.get("composed_of")
            if not isinstance(composed, list) or not composed:
                errors.append(f"{view.get('name')}: composed_of 不能为空")
                continue
            unknown = [item for item in composed if item not in allowed]
            if unknown:
                errors.append(f"{view.get('name')}: 含未知职责 {unknown}")

    decisions = data.get("human_decision_types")
    if set(decisions or []) != {"product_direction", "delete_data", "release"}:
        errors.append("人的拍板边界只能是产品方向、删数据、发布")

    policy = data.get("unknown_policy", "")
    if not all(token in policy for token in ("unknown", "0", "无", "正常")):
        errors.append("unknown 策略必须明确不得写成 0、无或正常")

    questions = data.get("first_screen_questions")
    if not isinstance(questions, list) or len(questions) != 3:
        errors.append("首屏必须且只能回答三个问题")

    entries = data.get("secondary_entries")
    required_entries = {"PM 简报", "上下文档案", "验收标准候选", "运行状态"}
    if not isinstance(entries, list) or not required_entries.issubset(entries):
        errors.append("次入口必须覆盖 PM、上下文、验收候选与运行状态")

    if data.get("lifecycle_fields") != list(LIFECYCLE_FIELDS):
        errors.append("职责生命周期字段顺序或内容被改动")
    return errors


def main():
    errors = validate(load())
    if errors:
        for error in errors:
            print(f"FAIL {error}")
        return 1
    print("PASS 七管家职责契约完整")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
