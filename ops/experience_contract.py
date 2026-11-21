#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate the small, project-neutral experience contract.

This validator checks contract completeness and evidence obligations. It does
not score visual taste and does not prescribe project design tokens.
"""

from __future__ import print_function

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "docs" / "experience" / "experience-contract.schema.json"
REQUIRED = (
    "schema_version", "experience_required", "scope", "primary_task",
    "attention_order", "states", "surfaces", "accessibility",
    "machine_checks", "human_checks", "exceptions", "evidence_policy",
)
ALLOWED_SCOPE = {
    "information_architecture", "visual_hierarchy", "interaction_states",
    "responsive", "accessibility", "unknown",
}


class ContractError(ValueError):
    pass


def validate_contract(contract):
    if not isinstance(contract, dict):
        raise ContractError("契约必须是 JSON object")
    missing = [key for key in REQUIRED if key not in contract]
    if missing:
        raise ContractError("缺少必填字段: " + ", ".join(missing))
    if contract["schema_version"] != "1":
        raise ContractError("不支持的 schema_version: %r" % contract["schema_version"])
    if not isinstance(contract["experience_required"], bool):
        raise ContractError("experience_required 必须是 boolean")
    scope = contract["scope"]
    if not isinstance(scope, list) or not scope or any(item not in ALLOWED_SCOPE for item in scope):
        raise ContractError("scope 必须是非空的受支持范围列表")
    for field in ("primary_task",):
        if not isinstance(contract[field], str) or not contract[field].strip():
            raise ContractError("%s 必须是非空字符串" % field)
    for field in ("attention_order", "surfaces", "machine_checks", "human_checks", "exceptions"):
        if not isinstance(contract[field], list):
            raise ContractError("%s 必须是数组" % field)
    if not contract["attention_order"] or not contract["surfaces"]:
        raise ContractError("attention_order 和 surfaces 不能为空")

    states = contract["states"]
    if not isinstance(states, list) or not states:
        raise ContractError("states 必须是非空数组")
    names = set()
    for state in states:
        if not isinstance(state, dict):
            raise ContractError("每个 state 必须是 object")
        for field in ("name", "applicable", "evidence_required"):
            if field not in state:
                raise ContractError("state 缺少字段: %s" % field)
        name = state["name"]
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ContractError("state name 必须非空且不能重复")
        names.add(name)
        if not isinstance(state["applicable"], bool) or not isinstance(state["evidence_required"], bool):
            raise ContractError("state 的 applicable/evidence_required 必须是 boolean")
        if not state["applicable"] and not str(state.get("reason", "")).strip():
            raise ContractError("不适用的 state 必须填写 reason: %s" % name)
        if state["applicable"] and state["evidence_required"] is not True:
            raise ContractError("适用的 state 必须要求证据: %s" % name)

    accessibility = contract["accessibility"]
    if not isinstance(accessibility, dict):
        raise ContractError("accessibility 必须是 object")
    for field in ("wcag_level", "keyboard_required", "contrast_required", "non_color_status_required"):
        if field not in accessibility:
            raise ContractError("accessibility 缺少字段: %s" % field)
    if not str(accessibility["wcag_level"]).strip():
        raise ContractError("wcag_level 不能为空")
    for field in ("keyboard_required", "contrast_required", "non_color_status_required"):
        if not isinstance(accessibility[field], bool):
            raise ContractError("accessibility.%s 必须是 boolean" % field)

    policy = contract["evidence_policy"]
    if not isinstance(policy, dict):
        raise ContractError("evidence_policy 必须是 object")
    for field in ("real_content_required", "real_state_required", "screenshots_required"):
        if field not in policy or not isinstance(policy[field], bool):
            raise ContractError("evidence_policy.%s 必须是 boolean" % field)
    if contract["experience_required"]:
        if not contract["machine_checks"] and not contract["human_checks"]:
            raise ContractError("体验任务至少声明 machine_checks 或 human_checks")
    return True


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def land_gate(path):
    """Return a truthful pre-land result for a committed contract.

    A missing contract is deliberately compatible with older projects. Once a
    project declares an experience contract, malformed or empty evidence
    obligations are a real land failure rather than a context-only warning.
    """
    contract_path = Path(path)
    if not contract_path.exists():
        return {"ok": True, "status": "not_applicable", "reason": "未声明体验契约"}
    try:
        contract = load(contract_path)
        validate_contract(contract)
    except (OSError, ValueError, ContractError) as exc:
        return {"ok": False, "status": "invalid", "reason": str(exc)}
    required = bool(contract.get("experience_required"))
    checks = list(contract.get("machine_checks") or []) + list(contract.get("human_checks") or [])
    if required and not checks:
        return {"ok": False, "status": "missing_checks", "reason": "体验任务没有机器或人工验收项"}
    return {
        "ok": True,
        "status": "required" if required else "optional",
        "reason": "体验契约已校验，验收项已声明",
        "machine_checks": list(contract.get("machine_checks") or []),
        "human_checks": list(contract.get("human_checks") or []),
    }


def main(argv):
    if len(argv) != 2:
        print("用法: python3 ops/experience_contract.py <contract.json>", file=sys.stderr)
        return 2
    try:
        validate_contract(load(argv[1]))
    except (OSError, ValueError, ContractError) as exc:
        print("experience-contract: FAIL: %s" % exc, file=sys.stderr)
        return 1
    print("experience-contract: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
