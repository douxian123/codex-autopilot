#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate the authoritative panel renderer contract.

The current HTML panel is an interim renderer. This contract is what survives
the final visual redesign: it owns attention order, state semantics and the
missing-data policy, while layout and components remain replaceable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = ROOT / "docs" / "experience" / "panel-renderer-contract.json"

REQUIRED = (
    "schema_version", "contract_name", "status", "renderer_mode",
    "final_visual_approved", "resource_uri", "primary_task", "attention_order",
    "required_status_fields", "states", "field_semantics", "action_policy",
    "user_hidden_fields", "accessibility", "machine_checks", "human_checks",
    "replacement_policy",
)

EXPECTED_ATTENTION_ORDER = (
    "需要你做什么",
    "是不是在干对的事",
    "已经替你完成什么",
    "PM 简报",
    "工程证据",
)

EXPECTED_FIELDS = (
    "needs_human",
    "current_work",
    "recent_completed",
    "pm_briefing",
    "engineering_evidence",
)


class ContractError(ValueError):
    pass


def load(path=None):
    return json.loads(Path(path or CONTRACT_PATH).read_text(encoding="utf-8"))


def validate(contract):
    if not isinstance(contract, dict):
        raise ContractError("渲染契约必须是 JSON object")
    missing = [key for key in REQUIRED if key not in contract]
    if missing:
        raise ContractError("缺少必填字段: " + ", ".join(missing))
    if contract["schema_version"] != "1":
        raise ContractError("不支持的 schema_version")
    if contract["final_visual_approved"] is not False:
        raise ContractError("当前面板未定稿，final_visual_approved 必须为 false")
    if contract["renderer_mode"] != "interim":
        raise ContractError("当前 renderer_mode 必须为 interim")
    if tuple(contract["attention_order"]) != EXPECTED_ATTENTION_ORDER:
        raise ContractError("注意力顺序不得被当前面板实现改写")
    if tuple(contract["required_status_fields"]) != EXPECTED_FIELDS:
        raise ContractError("首屏依赖字段不得被当前面板实现删改")
    if contract["resource_uri"] != "ui://autopilot/panel-v2.html":
        raise ContractError("resourceUri 必须与 MCP UI tool 一致")

    states = contract["states"]
    if not isinstance(states, list) or not states:
        raise ContractError("states 不能为空")
    names = set()
    for state in states:
        if not isinstance(state, dict):
            raise ContractError("state 必须是 object")
        name = state.get("name")
        if not name or name in names:
            raise ContractError("state name 必须非空且不重复")
        names.add(name)
        if state.get("applicable") is True and not str(state.get("required_copy", "")).strip():
            raise ContractError("适用 state 必须定义 required_copy: %s" % name)

    semantics = contract["field_semantics"]
    for field in EXPECTED_FIELDS:
        row = semantics.get(field) if isinstance(semantics, dict) else None
        if not isinstance(row, dict) or not all(row.get(k) for k in ("meaning", "empty", "unknown")):
            raise ContractError("字段缺少 meaning/empty/unknown 语义: %s" % field)

    action = contract["action_policy"]
    if not isinstance(action, dict):
        raise ContractError("action_policy 必须是 object")
    if action.get("max_primary_actions") != 2:
        raise ContractError("首屏最多两个主操作")
    if "点了会怎样" not in action.get("required_explanations", []):
        raise ContractError("动作必须解释点了会怎样")

    accessibility = contract["accessibility"]
    for key in ("keyboard_required", "visible_focus_required",
                "non_color_status_required", "accessible_names_required"):
        if accessibility.get(key) is not True:
            raise ContractError("accessibility.%s 必须为 true" % key)

    replacement = contract["replacement_policy"]
    if replacement.get("current_panel_role") != "interim_renderer_reference_implementation_only":
        raise ContractError("当前面板必须标记为中间态参考实现")
    if replacement.get("final_panel_must_preserve_contract") is not True:
        raise ContractError("最终面板必须保留同一份渲染契约")
    locked = set(replacement.get("final_panel_may_not_change_without_new_decision") or [])
    for key in ("attention_order", "state_semantics", "missing_data_policy", "human_boundary"):
        if key not in locked:
            raise ContractError("最终面板不可擅改的边界缺少: %s" % key)
    return True


def validate_repo(root=None):
    """Validate both the contract and the consumer wiring that keeps it honest."""
    root = Path(root or ROOT)
    validate(load(root / "docs" / "experience" / "panel-renderer-contract.json"))
    # 源码仓库把插件放在 plugin/，release 根目录自身就是插件根。
    # 两种布局都必须能被校验，否则安装后门禁会把正确发布误判成坏包。
    panel_path = root / "plugin" / "ui" / "panel.html"
    if not panel_path.is_file():
        panel_path = root / "ui" / "panel.html"
    panel = panel_path.read_text(encoding="utf-8")
    if 'id="rendererContract"' not in panel:
        raise ContractError("面板没有消费 renderer contract 的落点")
    if 'data-renderer-mode="interim"' not in panel:
        raise ContractError("当前面板没有声明 interim renderer")
    central = (root / "ops" / "central_mcp.py").read_text(encoding="utf-8")
    if "def _renderer_contract_summary()" not in central:
        raise ContractError("状态投影没有 renderer contract 摘要")
    if '"renderer_contract": _renderer_contract_summary()' not in central:
        raise ContractError("_status_payload 没有输出 renderer_contract")
    design = (root / "docs" / "PANEL_INTERACTION_DESIGN.md").read_text(encoding="utf-8")
    if "interim renderer" not in design.lower():
        raise ContractError("面板设计文档没有声明当前实现是中间态")
    return True


def main(argv):
    try:
        if len(argv) > 1 and argv[1] == "--repo":
            validate_repo(Path(argv[2]))
        else:
            validate_repo(ROOT)
    except (OSError, ValueError, ContractError) as exc:
        print("panel-renderer-contract: FAIL: %s" % exc, file=sys.stderr)
        return 1
    print("panel-renderer-contract: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
