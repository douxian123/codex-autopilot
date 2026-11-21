#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""施工闭环计划的机器校验（stdlib only）。"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAN = ROOT / "docs" / "closure_plan.json"

STAGE_IDS = (
    "steward_contract",
    "release_foundation",
    "context_archive",
    "pm_loop",
    "acceptance_loop",
    "runtime_loop",
    "status_health_exit",
    "panel_layering",
    "e2e_closure",
)
VALID_STATUS = {"pending", "in_progress", "done", "blocked"}
REQUIRED = ("id", "title", "depends_on", "status", "exit", "gate", "evidence", "user_entry")


def load(path=None):
    target = Path(path) if path else PLAN
    return json.loads(target.read_text(encoding="utf-8"))


def validate(data):
    errors = []
    if not isinstance(data, dict):
        return ["计划根节点必须是对象"]
    stages = data.get("stages")
    if not isinstance(stages, list):
        return ["stages 必须是数组"]
    ids = [s.get("id") for s in stages if isinstance(s, dict)]
    if ids != list(STAGE_IDS):
        errors.append("施工阶段必须按固定顺序且完整列出九项")
    seen = set()
    for stage in stages:
        if not isinstance(stage, dict):
            errors.append("施工阶段必须是对象")
            continue
        sid = stage.get("id", "?")
        for key in REQUIRED:
            value = stage.get(key)
            if not isinstance(value, (str, list)):
                errors.append(f"{sid}: 缺字段 {key}")
                continue
            # depends_on 为空是合法的：第一阶段没有前置依赖。
            if key != "depends_on" and not value:
                errors.append(f"{sid}: 缺字段 {key}")
        if stage.get("status") not in VALID_STATUS:
            errors.append(f"{sid}: status 非法")
        depends = stage.get("depends_on")
        if not isinstance(depends, list):
            errors.append(f"{sid}: depends_on 必须是数组")
            continue
        for dep in depends:
            if dep not in seen:
                errors.append(f"{sid}: 依赖 {dep} 不存在或排在后面，路径不成环约束被破坏")
        seen.add(sid)
    estimates = data.get("progress_estimates")
    if not isinstance(estimates, dict) or not estimates.get("口径"):
        errors.append("进度估计必须写明口径，不能把估算当自动计量")
    return errors


def main():
    errors = validate(load())
    if errors:
        for error in errors:
            print(f"FAIL {error}")
        return 1
    print("PASS 施工闭环计划完整且依赖无前指")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
