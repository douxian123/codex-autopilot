#!/usr/bin/env python3
"""Regression tests for the pre-land experience contract gate."""

import copy
import importlib.util
import json
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("experience_contract", ROOT / "ops" / "experience_contract.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
OPS = ROOT / "ops"


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
    "surfaces": ["host_panel"],
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
    panel = root / "plugin" / "ui" / "panel.html"
    panel.parent.mkdir(parents=True, exist_ok=True)
    panel.write_text("<!doctype html><title>test panel</title>", encoding="utf-8")
    contract_path = root / "docs" / "experience" / "experience-contract.json"
    contract_path.parent.mkdir(parents=True)
    contract_path.write_text(json.dumps(base), encoding="utf-8")
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

    panel = root / "plugin" / "ui" / "panel.html"
    panel.parent.mkdir(parents=True, exist_ok=True)
    panel.write_text("<!doctype html><title>fixture</title><div id='stateLabel'></div>", encoding="utf-8")

    unknown = copy.deepcopy(base)
    unknown["machine_checks"] = ["trust-me"]
    contract_path.write_text(json.dumps(unknown), encoding="utf-8")
    # Static validation does not know runtime check names; evidence evaluation does.
    evidence = {
        "source": "chrome-cdp",
        "browser": "Chrome/158.0.0.0",
        "panel_sha256": module._sha256(panel),
        "contract_sha256": module._sha256_text(contract_path.read_text(encoding="utf-8")),
        "checks": [],
        "screenshots": [],
    }
    reasons = module.evaluate_machine_evidence(unknown, evidence, root, root)
    check(any("未注册的 machine check" in reason for reason in reasons), reasons)

    evidence_dir = root / "evidence"
    evidence_dir.mkdir()
    evidence_path = evidence_dir / "experience-evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    missing_machine = module.evaluate_machine_evidence(base, evidence, root, evidence_dir)
    check(any("缺少机器检查证据" in reason for reason in missing_machine), missing_machine)
    check(any("截图" in reason for reason in missing_machine), missing_machine)

    acceptance = root / "acceptance.jsonl"
    missing_human = module.evaluate_human_acceptance(base, acceptance)
    check(any("没有台账记录" in reason for reason in missing_human), missing_human)

    acceptance.write_text(json.dumps({
        "id": "A-9001",
        "ts": "2026-11-21 00:00:00",
        "type": "acceptance",
        "domain": "experience",
        "criterion": "attention-order",
        "contract_version": "1",
        "needs_human": True,
        "status": "verified",
        "verified_by": "agent",
        "screenshots": ["shot.png"],
    }) + "\n", encoding="utf-8")
    self_verified = module.evaluate_human_acceptance(base, acceptance)
    check(any("未由人确认" in reason for reason in self_verified), self_verified)

    human_ok = {
        "id": "A-9002",
        "ts": "2026-11-21 00:00:01",
        "type": "acceptance",
        "domain": "experience",
        "criterion": "attention-order",
        "contract_version": "1",
        "needs_human": True,
        "status": "verified",
        "verified_by": "user",
        "screenshots": ["shot.png"],
    }
    with acceptance.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(human_ok) + "\n")
    check(module.evaluate_human_acceptance(base, acceptance) == [],
          module.evaluate_human_acceptance(base, acceptance))

    check(module.changes_require_live_experience(["plugin/ui/panel.html"]) is True,
          "panel change must require live evidence")
    check(module.changes_require_live_experience(
        ["docs/experience/experience-contract.json"]) is True,
        "contract change must require live evidence")
    check(module.changes_require_live_experience(
        ["ops/experience_contract.py", "ops/kgctl.py"]) is False,
        "gate implementation change must not require live visual evidence")

    check(module.infer_experience_kind("面板间距不对，按钮被裁切", [], "unknown") == "visual",
          "visual intake must be classified as visual")
    check(module.infer_experience_kind("这个流程体验别扭", [], "unknown") == "ux",
          "ux intake must be classified as ux")
    check(module.infer_experience_kind("体验契约门禁要验证", [], "unknown") == "unknown",
          "gate wording must not be hijacked by the experience marker")
    check(module.infer_experience_kind("修面板", [], "bug") == "bug",
          "explicit non-ux kind must not be overridden")

    # The runtime runner and the evaluator must agree on surface names and check keys.
    # This test uses the real runner once; if it passes evidence that the evaluator
    # rejects, the land gate would either lie or block valid visual changes forever.
    real_spec = importlib.util.spec_from_file_location(
        "experience_evidence", OPS / "experience_evidence.py")
    real_runner = importlib.util.module_from_spec(real_spec)
    real_spec.loader.exec_module(real_runner)
    real_evidence_dir = root / "real-evidence"
    real_evidence_path = real_runner.write_evidence(
        real_evidence_dir / "experience-evidence.json",
        panel=ROOT / "plugin" / "ui" / "panel.html",
        contract=ROOT / "docs" / "experience" / "experience-contract.json")
    real_contract = module.load(ROOT / "docs" / "experience" / "experience-contract.json")
    real_reasons = module.evaluate_machine_evidence(
        real_contract, real_evidence_path, ROOT, real_evidence_dir)
    check(not real_reasons, "real runner evidence must satisfy evaluator: %s" % real_reasons)

print("experience contract evidence gate: PASS (compat/valid/invalid/unknown/missing/self-verified/live)")
