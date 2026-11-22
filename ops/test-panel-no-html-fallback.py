#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：打开自动挡面板时禁止用 HTML 驾驶舱冒充原生面板。"""

from pathlib import Path

OPS = Path(__file__).resolve().parent
ROOT = OPS.parent


def read_skill(name: str) -> str:
    path = ROOT / "plugin" / "skills" / name / "SKILL.md"
    if not path.is_file():
        path = ROOT / "skills" / name / "SKILL.md"
    return path.read_text(encoding="utf-8")


setup = read_skill("setup")
central = read_skill("central-command")
mcp = (OPS / "central_mcp.py").read_text(encoding="utf-8")

checks = {
    "setup 禁止 CONTROL_PLANE 当面板": "任何浏览器页面当作“自动挡面板”交付" in setup,
    "setup 声明原生成功判据": "MCP App / 内联卡片 /\n对话侧栏 / 全局侧栏出现时，才算“面板已打开”" in setup,
    "central 禁止 companion fallback": "不得改用 `docs/CONTROL_PLANE.html`、`companion_api.py panel`" in central,
    "central 明确内部诊断命名": "`CONTROL_PLANE.html` 只能叫“内部诊断驾驶舱”" in central,
    "MCP 工具硬性触发": "用户说“打开自动挡面板 / 面板打不开 / 自动挡在哪看”时必须立即调用本工具" in mcp,
}

failed = [name for name, ok in checks.items() if not ok]
for name, ok in checks.items():
    print(("  ok   " if ok else "  FAIL ") + name)
if failed:
    print("panel no html fallback: FAIL (%d)" % len(failed))
    raise SystemExit(1)
print("panel no html fallback: PASS")
