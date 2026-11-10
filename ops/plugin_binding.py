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
import subprocess
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


def _git(*args, cwd=None) -> str:
    """只读 git 查询；任何失败都返回空串，由调用方当成不是仓库。"""
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                             text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def binding_health() -> dict:
    """体检当前绑定到底指没指到一个真项目（只读，不修任何东西）。

    为什么要有这条：2026-11-08 实测，插件的 ``.mcp.json`` 把 ``KG_REPO`` 指到
    ``~/autopilot-worktrees/install-49``。那目录只有 3 个 truth 文件、没有
    ``ops/tasks``、也不是 git 仓库。于是「装了、开了、显示 enabled」全都成立，
    而每一条需求其实无处可落。这种失效不会抛异常，只会让自动挡安静地空转，
    用户唯一的感受就是「装了跟没装一样」。

    因此把它变成一条明确的判据，由 status / doctor / 面板共同读：
      - 传统布局（ops/ 在项目里、没有 .mcp.json） -> status=skip
      - 自包含安装但没有任何 KG_REPO           -> status=broken
      - 目录不存在 / 不是 git 仓库 / 缺账本目录 -> status=broken
      - KG_TRUNK 等于 KG_REPO 或不是 worktree   -> status=broken
      - KG_TRUNK 尚未创建                       -> status=ok + warning（首次落盘时自建）
      - 全部通过                               -> status=ok
    """
    if not (plugin_root() / ".mcp.json").exists():
        return {"status": "skip", "repo": None, "trunk": None,
                "problems": [], "warnings": []}

    env = binding_env()
    repo_raw = env.get("KG_REPO") or os.environ.get("KG_REPO")
    trunk_raw = env.get("KG_TRUNK") or os.environ.get("KG_TRUNK")
    problems: list[str] = []

    if not repo_raw:
        return {"status": "broken", "repo": None, "trunk": trunk_raw, "problems": [
            "插件没有绑定任何项目：.mcp.json 里缺 KG_REPO。"
            "重跑 ./install.sh <你的项目目录> 即可。"], "warnings": []}

    repo = Path(repo_raw).expanduser()
    if not repo.is_dir():
        problems.append(f"绑定的状态根不存在：{repo}")
    else:
        top = _git("rev-parse", "--show-toplevel", cwd=str(repo))
        if not top:
            problems.append(
                f"绑定的状态根不是 git 仓库：{repo}"
                "（自动挡要靠分支和 worktree 落盘，普通目录无法闭环）")
        elif Path(top).resolve() != repo.resolve():
            problems.append(
                f"绑定的状态根不是仓库根：{repo}（仓库根是 {top}）")
        for sub in ("ops/tasks", "ops/truth"):
            if not (repo / sub).is_dir():
                problems.append(f"绑定的状态根缺少账本目录：{repo / sub}")

    trunk = Path(trunk_raw).expanduser() if trunk_raw else None
    warnings: list[str] = []
    if trunk is not None:
        if not trunk.exists():
            warnings.append(
                f"落盘主干尚未创建：{trunk}（首次合入时由 kgctl 自动创建）")
        elif trunk.resolve() == repo.resolve():
            problems.append(f"落盘主干与状态根是同一个目录：{trunk}（merge 会直接改写账本）")
        elif not _git("rev-parse", "--show-toplevel", cwd=str(trunk)):
            problems.append(f"落盘主干不是 git worktree：{trunk}")

    consistency = install_consistency()
    for p_msg in consistency.get("problems") or []:
        problems.append(p_msg)
    warnings.extend(consistency.get("warnings") or [])

    return {
        "status": "broken" if problems else "ok",
        "repo": str(repo),
        "trunk": str(trunk) if trunk is not None else None,
        "problems": problems,
        "warnings": warnings,
        "versions": consistency.get("versions"),
    }


def _release_version_from_path(raw) -> str | None:
    """From a path like ``.../releases/<version>``, take ``<version>``.

    Falls back to the last path segment; returns None when nothing is derivable.
    """
    if not raw:
        return None
    parts = Path(str(raw).rstrip("/")).parts
    if "releases" in parts:
        idx = len(parts) - 1 - parts[::-1].index("releases")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    name = Path(str(raw)).name
    return name or None


def _running_release_version() -> str | None:
    """Which release directory the running code lives in; None for dev checkouts."""
    root = plugin_root()
    if root.parent.name == "releases":
        return root.name
    # Codex snapshots installed plugins to ~/.codex/plugins/cache/<market>/<name>/<version>/
    if "cache" in root.parts and len(root.parts) >= 2:
        return root.name
    return None


def install_consistency() -> dict:
    """Version-consistency check: install-state / marketplace / running code must agree.

    2026-11-10 实测：一次升级只写了 ``install-state.json``（version=0.6.0-preview.52），
    而 ``marketplace.json`` 仍指向 ``releases/0.6.0-preview.51``，Codex 实际加载的还是
    .51。于是新版本才带来的门禁（``binding_health`` 本身就是 .52 才加的）根本没上线，
    用户面对的仍是旧行为。这种「半完成升级」不抛异常，只在几份配置之间自相矛盾，与
    11-08 那次空壳绑定同属「装了 / 开了 / enabled 全成立，却没有一处真的生效」。

    只读，不修任何东西。传统布局 / 开发检出（既无 install-state 也无入口）跳过。
    """
    home = Path.home()
    state_path = home / "plugins/codex-autopilot/install-state.json"
    market_path = home / ".agents/plugins/marketplace.json"

    state_version = None
    if state_path.exists():
        try:
            state_version = json.loads(
                state_path.read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError):
            state_version = None

    market_version = None
    market_path_raw = None
    if market_path.exists():
        try:
            data = json.loads(market_path.read_text(encoding="utf-8"))
            entry = next((p for p in data.get("plugins", [])
                          if isinstance(p, dict) and p.get("name") == "codex-autopilot"), None)
            if entry:
                src = entry.get("source")
                market_path_raw = src.get("path") if isinstance(src, dict) else src
                market_version = _release_version_from_path(market_path_raw)
        except (OSError, ValueError):
            pass

    running_version = None
    # 发布前自检跑在 .staging-<version>-pid 里，此时 install-state / marketplace 还是上一版，
    # 不该因为「正在跑临时目录」被判成不一致。
    if not plugin_root().name.startswith(".staging-"):
        running_version = _running_release_version()

    versions = {"install_state": state_version, "marketplace": market_version,
                "running": running_version}

    if state_version is None and market_version is None:
        return {"status": "skip", "problems": [], "warnings": [], "versions": versions}

    problems: list[str] = []
    warnings: list[str] = []
    if state_version and market_version and state_version != market_version:
        problems.append(
            f"安装版本不一致：install-state 写 {state_version}，"
            f"marketplace 入口仍指向 {market_version}（{market_path_raw}）。"
            "这是一次没装完的升级：重跑 ./install.sh 或 "
            "`codex plugin add codex-autopilot@personal` 让两者对齐。")
    if state_version and running_version and running_version != state_version:
        warnings.append(
            f"正在跑的代码是 {running_version}，install-state 写 {state_version}；"
            "重启 Codex 或重新加载插件后再确认。")

    return {"status": "broken" if problems else "ok", "problems": problems,
            "warnings": warnings, "versions": versions}


def binding_line(health: dict) -> str:
    """把体检结果压成一行人话，给 status / 面板直接用。"""
    st = health.get("status")
    if st == "ok":
        ver = ((health.get("versions") or {}).get("install_state")) or "开发检出"
        return f"安装绑定   OK（{ver} · 状态根 {health.get('repo')}）"
    if st == "skip":
        return "安装绑定   传统布局（ops/ 在项目里，无 .mcp.json）"
    first = (health.get("problems") or ["原因未知"])[0]
    return f"安装绑定   × 失效：{first}"
