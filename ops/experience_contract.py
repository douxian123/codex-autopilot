#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate the small, project-neutral experience contract.

This validator checks contract completeness and evidence obligations. It does
not score visual taste and does not prescribe project design tokens.
"""

from __future__ import print_function

import hashlib
import json
import sys
import tempfile
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
SUPPORTED_MACHINE_CHECKS = {
    "no-overflow", "no-clipping", "accessible-names", "focus-visible",
}
SUPPORTED_SURFACES = {"host_panel", "minimum_viewport"}
SURFACE_TO_VIEWPORT = {
    "host_panel": "host_panel",
    "minimum_viewport": "minimum_viewport",
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


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _panel_path(root):
    """Resolve the panel from both source and immutable release layouts."""
    root = Path(root)
    for candidate in (root / "ui" / "panel.html",
                      root / "plugin" / "ui" / "panel.html"):
        if candidate.is_file():
            return candidate
    return root / "plugin" / "ui" / "panel.html"


def _latest_by_id(rows):
    latest = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        current = latest.get(row["id"])
        if current is None or str(row.get("ts", "")) >= str(current.get("ts", "")):
            latest[row["id"]] = row
    return latest


def evaluate_machine_evidence(contract, evidence, root, evidence_dir):
    """Prove declared machine checks against a fresh browser run."""
    root = Path(root)
    evidence_dir = Path(evidence_dir)
    policy = contract["evidence_policy"]
    reasons = []
    if evidence.get("source") != "chrome-cdp":
        reasons.append("证据来源不是真实 Chrome 运行")
    if not str(evidence.get("browser", "")).startswith(("Chrome/", "Chromium/")):
        reasons.append("未记录真实 Chrome/Chromium 版本")
    panel = _panel_path(root)
    if not evidence.get("panel_sha256") or evidence.get("panel_sha256") != _sha256(panel):
        reasons.append("证据的面板哈希与当前树不一致")
    if not evidence.get("contract_sha256"):
        reasons.append("证据缺少契约哈希")
    else:
        contract_path = root / "docs" / "experience" / "experience-contract.json"
        expected_contract_hash = _sha256_text(
            contract_path.read_text(encoding="utf-8")) if contract_path.exists() else ""
        if evidence["contract_sha256"] != expected_contract_hash:
            reasons.append("证据的契约哈希与当前树不一致")

    surfaces = contract["surfaces"]
    unsupported = [name for name in surfaces if name not in SUPPORTED_SURFACES]
    if unsupported:
        reasons.append("不支持的 surface: " + ", ".join(unsupported))
    states = [state["name"] for state in contract["states"]
              if state.get("applicable") and state.get("evidence_required")]
    checks = list(contract.get("machine_checks") or [])
    unknown_checks = [name for name in checks if name not in SUPPORTED_MACHINE_CHECKS]
    if unknown_checks:
        reasons.append("未注册的 machine check: " + ", ".join(unknown_checks))

    observed = {}
    for item in evidence.get("checks", []):
        key = (item.get("viewport"), item.get("state"), item.get("name"))
        observed[key] = item
    missing = []
    failing = []
    for surface in surfaces:
        viewport = SURFACE_TO_VIEWPORT.get(surface)
        if not viewport:
            continue
        for state in states:
            for check in checks:
                item = observed.get((viewport, state, check))
                if not item:
                    missing.append("%s/%s/%s" % (viewport, state, check))
                elif item.get("status") != "pass":
                    failing.append("%s/%s/%s: %s" % (
                        viewport, state, check, item.get("details") or item.get("status")))
                elif policy.get("real_content_required") and int(item.get("text_length", 0)) < 50:
                    failing.append("%s/%s/%s: 缺少真实内容" % (viewport, state, check))
                elif str(item.get("source", "")).startswith("chrome-cdp") is not True:
                    failing.append("%s/%s/%s: 来源不可信" % (viewport, state, check))
    if missing:
        reasons.append("缺少机器检查证据: " + ", ".join(missing[:8]))
    if failing:
        reasons.append("机器检查失败: " + ", ".join(failing[:8]))

    if policy.get("screenshots_required"):
        screenshots = evidence.get("screenshots") or []
        if not screenshots:
            reasons.append("契约要求截图，但证据没有截图")
        for shot in screenshots:
            path = evidence_dir / str(shot.get("path", ""))
            if not path.is_file():
                reasons.append("截图文件不存在: %s" % shot.get("path"))
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest() != shot.get("sha256"):
                reasons.append("截图哈希不一致: %s" % shot.get("path"))
    return reasons


def evaluate_human_acceptance(contract, acceptance_path):
    """Prove declared human checks against verified acceptance records."""
    path = Path(acceptance_path)
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    latest = _latest_by_id(rows)
    reasons = []
    policy = contract["evidence_policy"]
    for criterion in contract.get("human_checks") or []:
        matches = []
        for rec in latest.values():
            if rec.get("criterion") != criterion or rec.get("domain") != "experience":
                continue
            if rec.get("applicable") is False:
                continue
            contract_version = rec.get("contract_version")
            if contract_version and contract_version != contract["schema_version"]:
                continue
            matches.append(rec)
        if not matches:
            reasons.append("人工验收项没有台账记录: %s" % criterion)
            continue
        verified = [rec for rec in matches
                    if rec.get("status") == "verified"
                    and rec.get("needs_human") is True
                    and rec.get("verified_by") in ("user", "human")]
        if not verified:
            reasons.append("人工验收项未由人确认: %s" % criterion)
            continue
        if policy.get("screenshots_required") and not any(
                rec.get("screenshots") for rec in verified):
            reasons.append("人工验收项缺复核截图: %s" % criterion)
    return reasons


EXPERIENCE_PATH_MARKERS = (
    "plugin/ui/",
    "docs/experience/experience-contract.json",
    "docs/experience/GENERAL_EXPERIENCE_CONSTITUTION.md",
    "docs/experience/AUTOPILOT_UI_IMPLEMENTATION.md",
    "frontend/",
    "web/",
    "panel.html",
    ".css",
    ".tsx",
    ".jsx",
    ".vue",
)

# Keep these markers narrow on purpose. A gate/engineering request that merely
# mentions "experience contract" must not be routed to design just because it
# contains the word "体验"; the first bucket below protects that case.
EXPERIENCE_GATE_MARKERS = (
    "门禁", "gate", "verify", "断言", "ci", "test", "构建失败",
    "build 失败", "编译失败", "测试失败",
)
EXPERIENCE_VISUAL_MARKERS = (
    "视觉", "排版", "样式", "颜色", "字号", "间距", "按钮位置", "遮挡", "裁切",
    "对齐", "图标", "圆角", "阴影", "hover", "动画", "边距", "layout", "css",
    "panel", "ui",
)
EXPERIENCE_UX_MARKERS = (
    "体验", "交互", "流程", "点击", "跳转", "反馈", "入口", "操作路径",
    "上手", "可用性", "ux", "别扭", "顺手",
)


def infer_experience_kind(text, files, current="unknown"):
    """Classify only visual/ux at intake; never override an explicit kind."""
    current = str(current or "unknown").strip().lower()
    if current in ("visual", "ux"):
        return current
    if current not in ("", "unknown"):
        return current
    haystack = " ".join([str(text or ""), " ".join(str(f) for f in (files or []))]).lower()
    if any(marker in haystack for marker in EXPERIENCE_GATE_MARKERS):
        return "unknown"
    if any(marker in haystack for marker in EXPERIENCE_VISUAL_MARKERS):
        return "visual"
    if any(marker in haystack for marker in EXPERIENCE_UX_MARKERS):
        return "ux"
    if any(marker in haystack for marker in EXPERIENCE_PATH_MARKERS):
        return "visual"
    return "unknown"


def changes_require_live_experience(changed_files):
    """Only experience-affecting changes need fresh browser/human evidence."""
    for name in changed_files or []:
        normalized = str(name).replace("\\", "/")
        if any(marker in normalized for marker in EXPERIENCE_PATH_MARKERS):
            return True
    return False


def land_gate(path, verify_evidence=False, root=None):
    """Return a truthful pre-land result for a committed contract.

    A missing contract is deliberately compatible with older projects. Once a
    project declares an experience contract, malformed obligations are a real
    land failure. With ``verify_evidence=True`` the gate also runs the real
    browser checks against the target tree and requires human checks to have
    verified acceptance records; stale or self-declared evidence cannot pass.
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
    if not required or not verify_evidence:
        return {
            "ok": True,
            "status": "required" if required else "optional",
            "reason": "体验契约已校验，验收项已声明",
            "machine_checks": list(contract.get("machine_checks") or []),
            "human_checks": list(contract.get("human_checks") or []),
        }

    tree = Path(root) if root else contract_path.parents[2]
    reasons = []
    machine_reason = None
    if contract.get("machine_checks"):
        try:
            ops_dir = str(Path(__file__).resolve().parent)
            if ops_dir not in sys.path:
                sys.path.insert(0, ops_dir)
            import experience_evidence
            panel = _panel_path(tree)
            with tempfile.TemporaryDirectory(prefix="experience-land-evidence-") as tmp:
                evidence_path = Path(tmp) / "experience-evidence.json"
                evidence = experience_evidence.write_evidence(
                    evidence_path, panel=panel, contract=contract_path)
                machine_reasons = evaluate_machine_evidence(
                    contract, evidence, tree, evidence_path.parent)
                if machine_reasons:
                    machine_reason = "；".join(machine_reasons)
                    reasons.append("机器证据：" + machine_reason)
        except Exception as exc:
            reasons.append("机器证据运行失败: %s" % exc)
    if contract.get("human_checks"):
        acceptance = tree / "ops" / "truth" / "acceptance.jsonl"
        human_reasons = evaluate_human_acceptance(contract, acceptance)
        if human_reasons:
            reasons.append("人工证据：" + "；".join(human_reasons))
    if reasons:
        return {
            "ok": False,
            "status": "evidence_fail",
            "reason": "；".join(reasons),
            "machine_checks": list(contract.get("machine_checks") or []),
            "human_checks": list(contract.get("human_checks") or []),
            "machine_reason": machine_reason,
        }
    return {
        "ok": True,
        "status": "required" if required else "optional",
        "reason": "体验契约已校验，机器证据现场通过，人工验收已由人确认",
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
