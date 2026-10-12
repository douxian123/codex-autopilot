#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取自包含插件安装时写下的项目绑定（stdlib only）。

插件的 `.mcp.json` 是这套编排唯一的「绑定真相」：运行时代码来自插件自己的 `ops/`，
账本写在 `KG_REPO` 指向的项目里。

Codex 拉起 MCP server 时会带上 `.mcp.json` 的 `env`；但从终端手敲 `kgctl.py` /
`truthctl.py`、或 launchd 拉起常驻守护时没有那层注入。缺了这层，脚本会回退去问当前
cwd 的 git 仓库，在 Codex 自己的目录或任意 worktree 里解析出错误状态根。

所以三个入口在解析状态根之前都先调用一次这里：没有显式 `KG_REPO` 时，用插件目录旁
的 `.mcp.json` 自动补上；显式环境变量优先，不覆盖。
"""

import json
import os
from pathlib import Path


def plugin_root() -> Path:
    """本文件在 `<plugin>/ops/` 下，插件根是它的上一级。"""
    return Path(__file__).resolve().parent.parent


def binding_env() -> dict:
    """读插件 `.mcp.json` 里为 codex-autopilot 声明的环境变量。"""
    path = plugin_root() / ".mcp.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    env = ((data.get("mcpServers") or {}).get("codex-autopilot") or {}).get("env") or {}
    return {str(k): str(v) for k, v in env.items() if v}


def apply_plugin_env() -> bool:
    """把 `.mcp.json` 里的 `KG_REPO` / `KG_TRUNK` 补进当前环境（已设置的优先）。

    返回是否真的补上了绑定。传统布局（ops/ 在项目里）没有这个文件时直接跳过。
    """
    applied = False
    env = binding_env()
    for key in ("KG_REPO", "KG_TRUNK"):
        value = env.get(key)
        if value and not os.environ.get(key):
            os.environ[key] = str(Path(value).expanduser())
            applied = True
    return applied
