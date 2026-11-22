#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：自然语言“打开自动挡面板”必须映射到 render_autopilot_panel 工具。"""

from pathlib import Path

OPS = Path(__file__).resolve().parent
ROOT = OPS.parent

setup = (ROOT / "plugin" / "skills" / "setup" / "SKILL.md").read_text(encoding="utf-8")
mcp = (OPS / "central_mcp.py").read_text(encoding="utf-8")

checks = {
    "setup 写明必须调用工具": "必须马上调用\n   `render_autopilot_panel` 工具" in setup,
    "setup 提供可复制精确指令": "请调用 render_autopilot_panel 工具打开自动挡面板。" in setup,
    "MCP 描述写明自然语言触发": "用户说“打开自动挡面板 / 面板打不开 / 自动挡在哪看”时必须立即调用本工具" in mcp,
    "MCP 明确禁止只解释入口": "不要只解释入口、不要只去侧栏找按钮" in mcp,
}

failed = [name for name, ok in checks.items() if not ok]
for name, ok in checks.items():
    print(("  ok   " if ok else "  FAIL ") + name)
if failed:
    print("panel intent trigger: FAIL (%d)" % len(failed))
    raise SystemExit(1)
print("panel intent trigger: PASS")
