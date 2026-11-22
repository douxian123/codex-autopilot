#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：打开 Codex Autopilot 面板的自然语言必须映射到 render_autopilot_panel 工具。"""

from pathlib import Path

OPS = Path(__file__).resolve().parent
ROOT = OPS.parent

setup_path = ROOT / "plugin" / "skills" / "setup" / "SKILL.md"
if not setup_path.is_file():
    # 不可变 release 目录把 plugin/skills 摊平成顶层 skills。
    setup_path = ROOT / "skills" / "setup" / "SKILL.md"
setup = setup_path.read_text(encoding="utf-8")
mcp = (OPS / "central_mcp.py").read_text(encoding="utf-8")

def has_any(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


# “自动挡”是内部旧称，只作为兼容触发语；公开品牌和面板文案使用 Codex Autopilot。
checks = {
    "setup 写明必须调用工具": "必须马上调用\n   `render_autopilot_panel` 工具" in setup,
    "setup 提供可复制精确指令": has_any(
        setup,
        "请调用 render_autopilot_panel 工具打开 Codex Autopilot 面板。",
        "请调用 render_autopilot_panel 工具打开自动挡面板。",
    ),
    "MCP 描述写明自然语言触发": has_any(
        mcp,
        "用户说“打开 Codex Autopilot 面板 / 打开自动挡面板 / 面板打不开 / 面板在哪看”时必须立即调用本工具",
        "用户说“打开自动挡面板 / 面板打不开 / 自动挡在哪看”时必须立即调用本工具",
    ),
    "MCP 明确禁止只解释入口": "不要只解释入口、不要只去侧栏找按钮" in mcp,
}

failed = [name for name, ok in checks.items() if not ok]
for name, ok in checks.items():
    print(("  ok   " if ok else "  FAIL ") + name)
if failed:
    print("panel intent trigger: FAIL (%d)" % len(failed))
    raise SystemExit(1)
print("panel intent trigger: PASS")
