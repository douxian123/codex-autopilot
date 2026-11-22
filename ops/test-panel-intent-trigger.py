#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：打开 Codex Autopilot 面板的自然语言必须映射到 render_autopilot_panel 工具。"""

import json
import os
import subprocess
from pathlib import Path

OPS = Path(__file__).resolve().parent
ROOT = OPS.parent

setup_path = ROOT / "plugin" / "skills" / "setup" / "SKILL.md"
if not setup_path.is_file():
    # 不可变 release 目录把 plugin/skills 摊平成顶层 skills。
    setup_path = ROOT / "skills" / "setup" / "SKILL.md"
setup = setup_path.read_text(encoding="utf-8")
mcp = (OPS / "central_mcp.py").read_text(encoding="utf-8")
installer = (ROOT / "install.sh").read_text(encoding="utf-8")
panel_candidates = (ROOT / "plugin" / "ui" / "panel.html", ROOT / "ui" / "panel.html")
panel = next((path for path in panel_candidates if path.is_file()), panel_candidates[0])
panel_html = panel.read_text(encoding="utf-8") if panel.is_file() else ""

def has_any(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def tool_definition(text: str, name: str) -> str:
    marker = '"name": "%s"' % name
    start = text.find(marker)
    if start < 0:
        return ""
    return text[start:start + 3000]


render_tool = tool_definition(mcp, "render_autopilot_panel")


# “自动挡”是内部旧称，只作为兼容触发语；公开品牌和面板文案使用 Codex Autopilot。
checks = {
    "setup 写明必须调用工具": "必须马上调用\n   `render_autopilot_panel` 工具" in setup,
    "setup 提供可复制精确指令": has_any(
        setup,
        "请调用 render_autopilot_panel 工具打开 Codex Autopilot 面板。",
        "请调用 render_autopilot_panel 工具打开自动挡面板。",
    ),
    "setup 写明 deferred 工具不算现场成功": (
        "工具能被 `tool_search` 搜到，不等于当前会话能直接调用" in setup
    ),
    "setup 写明直接调用 unsupported 的止损": (
        "unsupported call: mcp__codex_autopilot__render_autopilot_panel" in setup
    ),
    "MCP 描述写明自然语言触发": has_any(
        mcp,
        "用户说“打开 Codex Autopilot 面板 / 打开自动挡面板 / 面板打不开 / 面板在哪看”时必须立即调用本工具",
        "用户说“打开自动挡面板 / 面板打不开 / 自动挡在哪看”时必须立即调用本工具",
    ),
    "MCP 明确禁止只解释入口": "不要只解释入口、不要只去侧栏找按钮" in mcp,
    "setup 写明 unsupported 止损": (
        "unsupported call: mcp__codex_autopilot__render_autopilot_panel" in setup
    ),
    "安装器把面板工具排除 deferred 桥": (
        '"omit_tools_from": ["deferred"]' in installer
    ),
    "面板工具声明本地只读语义": all(
        needle in render_tool
        for needle in (
            '"readOnlyHint": True',
            '"destructiveHint": False',
            '"openWorldHint": False',
        )
    ),
    "面板完成 MCP App 初始化握手": all(
        needle in panel_html
        for needle in (
            'request("ui/initialize"',
            'protocolVersion: "2025-11-21"',
            'notify("ui/notifications/initialized"',
            'initializeMcpApp()',
        )
    ),
}


def _run(argv):
    try:
        done = subprocess.run(
            argv,
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return done.returncode, done.stdout


# 工具描述存在不等于宿主能调用。历史上发生过 tool_search 能搜到
# render_autopilot_panel，但实际调用返回 unsupported call。修复点是让插件工具
# 绕过 deferred bridge；prompt-input 不应再把 MCP 命名空间塞给模型。这里只验证
# 用户指令原样进入 prompt、deferred namespace 不在 prompt 里，以及 installed
# manifest 的 omit_tools_from 接线。
codex_cli = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
if codex_cli.is_file():
    code, mcp_list = _run([str(codex_cli), "mcp", "list", "--json"])
    server_registered = False
    if code == 0:
        try:
            servers = json.loads(mcp_list)
            server_registered = any(
                item.get("name") in ("codex-autopilot", "codex_autopilot")
                and item.get("enabled") is True
                for item in servers
            )
        except (TypeError, ValueError):
            server_registered = False
    checks["宿主已注册自动挡 MCP"] = server_registered

    env = dict(os.environ)
    env["CODEX_HOME"] = str(Path.home() / ".codex")
    try:
        done = subprocess.run(
            [
                str(codex_cli),
                "debug",
                "prompt-input",
                "请调用 render_autopilot_panel 工具打开 Codex Autopilot 面板。",
            ],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
            env=env,
        )
        prompt_ok = done.returncode == 0 and (
            "请调用 render_autopilot_panel 工具打开 Codex Autopilot 面板。"
            in done.stdout
        ) and "mcp__codex_autopilot" not in done.stdout
    except (OSError, subprocess.SubprocessError):
        prompt_ok = False
    checks["宿主 prompt 保留 direct tool 指令且不带 deferred 命名空间"] = prompt_ok

    # 过期 release 路径会让宿主 MCP 启动失败，进而丢掉整组动态工具。
    config_path = Path.home() / ".codex" / "config.toml"
    if config_path.is_file():
        config = config_path.read_text(encoding="utf-8")
        stale_app_tools = "codex-app-tools/26.1103.60933" in config
        checks["宿主 app-tools MCP 未指向过期版本"] = not stale_app_tools

    install_state_path = Path.home() / "plugins" / "codex-autopilot" / "install-state.json"
    if install_state_path.is_file():
        try:
            install_state = json.loads(install_state_path.read_text(encoding="utf-8"))
            version = install_state.get("version")
        except (OSError, ValueError, TypeError):
            version = None
        if version:
            installed_mcp = (
                Path.home()
                / ".codex"
                / "plugins"
                / "cache"
                / "personal"
                / "codex-autopilot"
                / str(version)
                / ".mcp.json"
            )
            if installed_mcp.is_file():
                try:
                    manifest = json.loads(installed_mcp.read_text(encoding="utf-8"))
                    server = manifest.get("mcpServers", {}).get("codex-autopilot", {})
                    checks["已安装面板工具排除 deferred 桥"] = (
                        "deferred" in (server.get("omit_tools_from") or [])
                    )
                except (OSError, ValueError, TypeError):
                    checks["已安装面板工具排除 deferred 桥"] = False

failed = [name for name, ok in checks.items() if not ok]
for name, ok in checks.items():
    print(("  ok   " if ok else "  FAIL ") + name)
if failed:
    print("panel intent trigger: FAIL (%d)" % len(failed))
    raise SystemExit(1)
print("panel intent trigger: PASS")
