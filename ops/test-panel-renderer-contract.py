#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Positive and negative tests for the authoritative panel renderer contract."""

import copy
import importlib.util
import json
import sys
from pathlib import Path


OPS = Path(__file__).resolve().parent
ROOT = OPS.parent
spec = importlib.util.spec_from_file_location(
    "panel_renderer_contract", OPS / "panel_renderer_contract.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def expect_fail(contract, fragment):
    try:
        module.validate(contract)
    except module.ContractError as exc:
        assert fragment in str(exc), (str(exc), fragment)
        return
    raise AssertionError("expected failure: %s" % fragment)


def main():
    valid = module.load()
    assert module.validate(valid) is True
    assert module.validate_repo(ROOT) is True

    wrong_order = copy.deepcopy(valid)
    wrong_order["attention_order"] = list(reversed(wrong_order["attention_order"]))
    expect_fail(wrong_order, "注意力顺序")

    approved = copy.deepcopy(valid)
    approved["final_visual_approved"] = True
    expect_fail(approved, "final_visual_approved")

    missing_field = copy.deepcopy(valid)
    missing_field["required_status_fields"].remove("needs_human")
    expect_fail(missing_field, "首屏依赖字段")

    missing_semantics = copy.deepcopy(valid)
    missing_semantics["field_semantics"]["needs_human"].pop("unknown")
    expect_fail(missing_semantics, "unknown")

    actions = copy.deepcopy(valid)
    actions["action_policy"]["max_primary_actions"] = 3
    expect_fail(actions, "最多两个主操作")

    replacement = copy.deepcopy(valid)
    replacement["replacement_policy"]["final_panel_must_preserve_contract"] = False
    expect_fail(replacement, "同一份渲染契约")

    payload_spec = importlib.util.spec_from_file_location("central_mcp", OPS / "central_mcp.py")
    central = importlib.util.module_from_spec(payload_spec)
    payload_spec.loader.exec_module(central)
    summary = central._renderer_contract_summary()
    assert summary["status"] == "ok", summary
    assert summary["mode"] == "interim", summary
    assert summary["final_visual_approved"] is False, summary
    assert "需要你做什么" in summary["attention_order"], summary

    schema = json.loads((ROOT / "docs/experience/panel-renderer-contract.json").read_text(encoding="utf-8"))
    assert schema["replacement_policy"]["current_panel_role"] == "interim_renderer_reference_implementation_only"
    print("panel renderer contract: PASS (valid + 6 negative cases + provider projection)")


if __name__ == "__main__":
    main()
