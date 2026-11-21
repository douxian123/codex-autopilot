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
COMPLETION_LAYERS = ("implemented", "machine_verified", "real_entry_verified",
                     "closed", "blocked")
RELEASE_CLAIMS = ("5/5", "可以对外")
RELEASE_EVIDENCE_FIELDS = ("eval_command", "eval_result", "negative_controls",
                           "evaluated_at", "scope_note")
RELEASE_BOUNDARY = "完整愿景对外 Beta"


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

    completion = data.get("completion_layers")
    if not isinstance(completion, dict):
        errors.append("必须写明 completion_layers，区分实现、机器验证、真实入口、收口和阻塞")
    else:
        for layer in COMPLETION_LAYERS:
            if layer not in completion:
                errors.append(f"completion_layers 缺 {layer}")

    release_evidence = data.get("release_evidence")
    evidence_ok = isinstance(release_evidence, dict)
    readiness = ""
    estimates = data.get("progress_estimates")
    if isinstance(estimates, dict):
        readiness = str(estimates.get("对外发布就绪度") or "")
    readiness_claims = any(phrase in readiness for phrase in RELEASE_CLAIMS)
    eval_result = str(release_evidence.get("eval_result") or "") if evidence_ok else ""
    evidence_claims = any(phrase in eval_result for phrase in RELEASE_CLAIMS)
    if readiness_claims != evidence_claims:
        errors.append("对外发布就绪度与 release_evidence.eval_result 结论不一致")
    claims_release = readiness_claims or evidence_claims
    if evidence_ok:
        for key in RELEASE_EVIDENCE_FIELDS:
            if not str(release_evidence.get(key) or "").strip():
                errors.append(f"release_evidence 缺字段 {key}")
                evidence_ok = False
        if evidence_ok and "可以对外（5/5 通过）" not in str(
                release_evidence.get("eval_result") or ""):
            errors.append("release_evidence.eval_result 必须写清当前 product_eval 原文结论")
            evidence_ok = False
        if evidence_ok and "8/8" not in str(
                release_evidence.get("negative_controls") or ""):
            errors.append("release_evidence.negative_controls 必须写清阴性对照结果")
            evidence_ok = False
        if evidence_ok and RELEASE_BOUNDARY not in str(
                release_evidence.get("scope_note") or ""):
            errors.append("release_evidence.scope_note 必须写清完整愿景对外 Beta 的未验证边界")
            evidence_ok = False
    if claims_release and not evidence_ok:
        errors.append("施工计划声明 5/5 / 可以对外时，必须带当前可复核的 release_evidence")

    by_id = {s.get("id"): s for s in stages if isinstance(s, dict)}
    for sid in ("release_foundation", "acceptance_loop", "e2e_closure"):
        stage = by_id.get(sid)
        if stage and stage.get("status") == "done" and not evidence_ok:
            errors.append(f"{sid} 标 done 但发布证据不成立，不能宣称收敛")
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
