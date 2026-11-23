# Verify the user-facing handoff contract remains part of the shipped skill.
from pathlib import Path

skill = Path(__file__).resolve().parents[1] / "plugin/skills/central-command/SKILL.md"
text = skill.read_text()

required = (
    "## 路由后的责任与验收",
    "尚未分配",
    "autopilot_status",
    "docs/PROJECT_STATUS.md",
    "docs/PRODUCT_MAINLINE.md",
    "register_acceptance",
    "真实门禁证据",
    "实际打开地址",
    "不能把“已路由”或“分支已创建”当成验收通知",
)
missing = [item for item in required if item not in text]
if missing:
    raise SystemExit(f"central-command follow-up contract missing: {missing}")

print("central-command follow-up contract ok")
