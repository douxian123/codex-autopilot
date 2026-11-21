#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Negative and positive tests for the experience contract gate."""

import copy
import importlib.util
import json
import sys
from pathlib import Path


OPS = Path(__file__).resolve().parent
ROOT = OPS.parent
spec = importlib.util.spec_from_file_location("experience_contract", OPS / "experience_contract.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def load_fixture():
    return {
        "schema_version": "1",
        "experience_required": True,
        "scope": ["visual_hierarchy", "responsive", "accessibility"],
        "primary_task": "识别当前工作并决定下一步",
        "attention_order": ["当前工作", "阻塞", "下一步", "证据"],
        "keep_information": ["当前工作", "阻塞", "下一步"],
        "demote_information": ["历史", "高级诊断"],
        "remove_information": ["内部调试字段"],
        "states": [
            {"name": "default", "applicable": True, "evidence_required": True},
            {"name": "error", "applicable": True, "evidence_required": True},
            {"name": "permission-denied", "applicable": False,
             "evidence_required": False, "reason": "当前面板没有权限分层"},
        ],
        "surfaces": ["host_panel", "minimum_viewport"],
        "accessibility": {
            "wcag_level": "AA-applicable-checks",
            "keyboard_required": True,
            "contrast_required": True,
            "non_color_status_required": True,
        },
        "machine_checks": ["no-overflow", "focus-visible"],
        "human_checks": ["attention-order"],
        "exceptions": [],
        "evidence_policy": {
            "real_content_required": True,
            "real_state_required": True,
            "screenshots_required": True,
        },
    }


def expect_fail(contract, fragment):
    try:
        module.validate_contract(contract)
    except module.ContractError as exc:
        assert fragment in str(exc), (str(exc), fragment)
        return
    raise AssertionError("expected failure: %s" % fragment)


def main():
    valid = load_fixture()
    assert module.validate_contract(valid) is True

    missing = copy.deepcopy(valid)
    del missing["primary_task"]
    expect_fail(missing, "primary_task")

    skipped = copy.deepcopy(valid)
    skipped["states"][1]["applicable"] = False
    skipped["states"][1].pop("reason", None)
    expect_fail(skipped, "reason")

    weak = copy.deepcopy(valid)
    weak["states"][0]["evidence_required"] = False
    expect_fail(weak, "要求证据")

    duplicate = copy.deepcopy(valid)
    duplicate["states"].append(copy.deepcopy(duplicate["states"][0]))
    expect_fail(duplicate, "不能重复")

    no_checks = copy.deepcopy(valid)
    no_checks["machine_checks"] = []
    no_checks["human_checks"] = []
    expect_fail(no_checks, "machine_checks")

    bad_scope = copy.deepcopy(valid)
    bad_scope["scope"] = ["make-it-premium"]
    expect_fail(bad_scope, "scope")

    schema = json.loads((ROOT / "docs/experience/experience-contract.schema.json").read_text(encoding="utf-8"))
    assert schema["$schema"].endswith("2020-12/schema")
    assert "states" in schema["required"]
    assert module.validate_contract(valid) is True
    print("experience contract: PASS (valid + 6 negative cases)")


if __name__ == "__main__":
    main()
