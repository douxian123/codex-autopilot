#!/usr/bin/env python3
"""Regression tests for the pre-land experience contract gate."""

import importlib.util
import json
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("experience_contract", ROOT / "ops" / "experience_contract.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


base = {
    "schema_version": "1",
    "experience_required": True,
    "scope": ["visual_hierarchy"],
    "primary_task": "test",
    "attention_order": ["current"],
    "states": [{"name": "default", "applicable": True, "evidence_required": True}],
    "surfaces": ["panel"],
    "accessibility": {
        "wcag_level": "AA",
        "keyboard_required": True,
        "contrast_required": True,
        "non_color_status_required": True,
    },
    "machine_checks": ["no-overflow"],
    "human_checks": ["attention-order"],
    "exceptions": [],
    "evidence_policy": {
        "real_content_required": True,
        "real_state_required": True,
        "screenshots_required": True,
    },
}


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    missing = module.land_gate(root / "missing.json")
    check(missing["ok"] and missing["status"] == "not_applicable", missing)

    path = root / "contract.json"
    path.write_text(json.dumps(base), encoding="utf-8")
    valid = module.land_gate(path)
    check(valid["ok"] and valid["status"] == "required", valid)

    broken = dict(base)
    broken["machine_checks"] = []
    broken["human_checks"] = []
    path.write_text(json.dumps(broken), encoding="utf-8")
    invalid = module.land_gate(path)
    check(not invalid["ok"] and invalid["status"] == "invalid", invalid)

print("experience contract land gate: PASS (compat + valid + invalid)")
