#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把模糊需求编译成可判定验收标准（stdlib only）。

这和 `acceptance_criteria.py` 不是一回事：

  * `acceptance_criteria.py` 是验收管家的候选链路：从入口原话提名「怎么算完成」，
    人确认后才登记正式验收项；
  * 本模块是规划层的确定性编译器：把已路由的工作项翻译成机器门禁和人工复核步骤，
    供执行者和工程详情工单表消费。

它不创建验收项、不写 evidence，也不把随口一句需求变成项目门禁。机器命令来自项目
配置和文件线索；推不出可判定条件时如实返回 `unknown`。
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def _dedup(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for check in checks:
        key = (_text(check.get("name")), _text(check.get("cmd")), _text(check.get("cwd")))
        if key in seen:
            continue
        seen.add(key)
        out.append(check)
    return out


def _machine(name: str, cmd: str, cwd: str, why: str) -> dict[str, str]:
    return {
        "name": _text(name) or "gate",
        "cmd": _text(cmd),
        "cwd": _text(cwd) or ".",
        "pass_condition": "exit 0",
        "why": _text(why),
    }


def _configured_gates(cfg: dict[str, Any]) -> list[dict[str, str]]:
    try:
        import central_config
    except ImportError:
        return []
    gates = central_config.verify_gates(cfg or {})
    return [
        _machine(gate.get("name"), gate.get("cmd"), gate.get("cwd"), "项目配置的验证门禁")
        for gate in gates
        if _text(gate.get("cmd"))
    ]


def _file_gates(files: list[str]) -> list[dict[str, str]]:
    paths = [_text(path) for path in (files or []) if _text(path)]
    checks: list[dict[str, str]] = []
    py_files = [path for path in paths if path.endswith(".py")]
    if py_files:
        quoted = " ".join(shlex.quote(path) for path in py_files[:8])
        checks.append(_machine(
            "py-compile", f"python3 -m py_compile {quoted}", ".",
            "这次改动包含 Python 文件，至少要保证可编译"))
    if any("panel.html" in path or path.endswith((".tsx", ".ts", ".jsx", ".js", ".css"))
           for path in paths):
        checks.append(_machine(
            "panel-logic", "python3 ops/panel_logic_check.py", ".",
            "界面代码改动必须过面板首屏逻辑门禁"))
        checks.append(_machine(
            "panel-render", "python3 ops/panel_render_check.py --no-shot", ".",
            "界面代码改动必须真实渲染"))
    if any(Path(path).name in ("central.config.json", "AGENTS.md") for path in paths):
        checks.append(_machine(
            "selftest", "python3 ops/selftest.py", ".",
            "控制面/全局约定改动必须过总闸"))
    return checks


def _human_checks(text: str, kind: str) -> list[dict[str, str]]:
    low = f"{text} {kind}".lower()
    visual = kind in ("visual", "ux") or any(
        key in low for key in (
            "视觉", "排版", "样式", "颜色", "字号", "间距", "按钮位置", "遮挡", "裁切",
            "手感", "顺手", "体验", "交互", "点击", "跳转", "入口", "操作路径", "看起来", "观感"))
    if not visual:
        return []
    return [
        {
            "step": "打开真实面板/页面，按需求描述的路径操作一遍",
            "pass_condition": "用户看到预期结果，且没有遮挡、错位或多余提示",
            "why": "视觉/交互结果不能由命令退出码代替人眼确认",
        },
        {
            "step": "刷新后再看一次，确认状态不是缓存假象",
            "pass_condition": "刷新后结果保持，关键文字和状态没有回退",
            "why": "防止只在单次渲染里看起来正确",
        },
    ]


def compile_criteria(text: str, kind: str, files: list[str] | None,
                     lane: str = "", cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """把一条需求编译成可判定验收标准。纯函数，不写文件、不执行命令。"""
    machine = _dedup(_configured_gates(cfg or {}) + _file_gates(list(files or [])))
    human = _human_checks(_text(text), _text(kind))
    status = "ok" if machine or human else "unknown"
    parts: list[str] = []
    if machine:
        parts.append(f"机器门禁 {len(machine)} 条（每条 exit 0）")
    if human:
        parts.append(f"人工复核 {len(human)} 条")
    summary = "；".join(parts) or "没有推导出可判定条件"
    return {
        "status": status,
        "summary": summary,
        "lane": _text(lane),
        "machine_checks": machine,
        "human_checks": human,
        "needs_human": bool(human),
        "source": "acceptance-criteria-compiler",
    }


def summary_label(criteria: Any) -> str:
    """工单表用的一行摘要：只报条数，不把内部命令堆到首屏。"""
    if not isinstance(criteria, dict):
        return ""
    machine = criteria.get("machine_checks") or []
    human = criteria.get("human_checks") or []
    if not isinstance(machine, list):
        machine = []
    if not isinstance(human, list):
        human = []
    if not machine and not human:
        return ""
    return f"验收标准：机器 {len(machine)} 条 / 人工 {len(human)} 条"


def render_for_prompt(criteria: Any) -> list[str]:
    """把验收标准转成执行 prompt 能照抄的硬约束。"""
    if not isinstance(criteria, dict):
        return []
    lines = ["", "本工单的可判定验收标准（不是建议，做完必须逐条自证）："]
    checks = criteria.get("machine_checks") or []
    for check in checks:
        if not isinstance(check, dict):
            continue
        lines.append(
            f"- [{check.get('name') or 'gate'}] `{check.get('cmd')}` "
            f"(cwd={check.get('cwd') or '.'}) 必须真跑，通过条件是 "
            f"{check.get('pass_condition') or 'exit 0'}；在总结里写命令、cwd 和退出码。")
    for i, check in enumerate(criteria.get("human_checks") or [], 1):
        if not isinstance(check, dict):
            continue
        lines.append(
            f"- 人工复核 {i}：{check.get('step') or ''}；"
            f"通过条件：{check.get('pass_condition') or ''}。")
    if len(lines) == 1:
        lines.append("- 当前没有推导出可判定条件；先补文件线索或验收标准，不许自己宣布完成。")
    return lines
