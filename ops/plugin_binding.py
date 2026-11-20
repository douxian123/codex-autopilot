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

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def plugin_root() -> Path:
    """本文件在 `<plugin>/ops/` 下，插件根是它的上一级。"""
    return Path(__file__).resolve().parent.parent


def running_ops() -> Path:
    """本进程正在执行的那份代码所在的 ops/。

    注意：这只是「跑的是哪一份」，不等于「现在装的是哪一份」——升级之后两者会分叉。
    """
    return Path(__file__).resolve().parent


def _abs_from_home(raw) -> Path | None:
    """install-state / marketplace 里的路径是相对 ~ 写的，例如
    `./plugins/codex-autopilot/releases/0.6.0-preview.86`。"""
    if not raw:
        return None
    cand = Path(str(raw)).expanduser()
    if not cand.is_absolute():
        cand = Path.home() / str(raw).lstrip("./")
    return cand


def _usable_ops(cand: Path | None) -> Path | None:
    """一个 ops/ 只有同时带着 kgctl 和 central_mcp 才算能跑。"""
    if cand is None:
        return None
    ops = cand if cand.name == "ops" else cand / "ops"
    if (ops / "kgctl.py").exists() and (ops / "central_mcp.py").exists():
        return ops
    return None


def current_ops() -> Path:
    """当前**真正装好、可执行**的那份 ops/（只读解析，不写任何东西）。

    为什么不能拿 `Path(__file__).resolve().parent` 就算完（2026-11-16 实测）：
    Codex 把插件快照到 `~/.codex/plugins/cache/personal/codex-autopilot/<版本>/`，
    并以那个版本目录为 cwd 常驻拉起 `./ops/central_mcp.py`。发新版时 Codex 会用新
    版本目录替换旧的，**却不重启已经在跑的 MCP 进程**。于是那些进程握着一份已被
    删除的目录：`OPS_DIR / "kgctl.py"` 根本不存在，每次工具调用都只拿到
    `can't open file .../ops/kgctl.py`，而宿主那边只表现为「工具坏了」。
    实测：13 个在跑的 `central_mcp.py`，cwd 全是已删除的 `.82`，而 `.86` 早已装好。

    这和 disk_reap 那次「每小时跑着 44 个版本前的化石代码」是同一类失效，所以复用
    同一套解析顺序：本目录能用就用本目录；否则跟随 install-state；再否则跟随
    marketplace。全都读不到时才报自己。
    """
    here = _usable_ops(running_ops())
    if here is not None:
        return here
    state = Path.home() / "plugins/codex-autopilot/install-state.json"
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
        found = _usable_ops(_abs_from_home(data.get("source")))
        if found is not None:
            return found
    except (OSError, ValueError):
        pass
    market = Path.home() / ".agents/plugins/marketplace.json"
    try:
        data = json.loads(market.read_text(encoding="utf-8"))
        for plugin in data.get("plugins") or []:
            if not isinstance(plugin, dict) or plugin.get("name") != "codex-autopilot":
                continue
            src = plugin.get("source")
            found = _usable_ops(
                _abs_from_home(src.get("path") if isinstance(src, dict) else src))
            if found is not None:
                return found
    except (OSError, ValueError):
        pass
    return running_ops()


def stale_code_problem() -> str | None:
    """在跑的这份代码目录还在不在——不在，就等于「装好了，但整条 MCP 已经哑了」。

    这一条必须是**错误**而不是提醒：它不抛异常，只让每个工具调用安静地失败，正是
    本项目硬规则里最难发现的那种假绿灯。
    """
    running = running_ops()
    if (running / "kgctl.py").exists():
        return None
    live = current_ops()
    return (f"正在运行的插件代码目录已被删除：{running}。"
            "Codex 升级插件时会替换版本目录，但不会重启已经在跑的 MCP 进程，"
            "于是那些进程每次调用 kgctl / truthctl 都只拿到 can't open file。"
            f"重启 Codex，让 MCP 用当前版本（{live.parent.name}）重新拉起即可。")


def binding_path() -> Path:
    """绑定真相 `.mcp.json` 的位置。

    正常就是本插件根下那一份。但当正在跑的目录已被升级替换掉时，那份 `.mcp.json`
    也一起没了 —— 这时候得去**当前安装版本**读绑定，否则陈旧进程连「账该写到哪个
    项目」都不知道，`binding_health()` 还会因此误判成「传统布局」直接 skip。
    """
    here = plugin_root() / ".mcp.json"
    if here.exists():
        return here
    return current_ops().parent / ".mcp.json"


def binding_env() -> dict:
    """读插件 `.mcp.json` 里为 codex-autopilot 声明的环境变量。"""
    path = binding_path()
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
    env = binding_env()
    repo_raw = env.get("KG_REPO") or os.environ.get("KG_REPO")
    trunk_raw = env.get("KG_TRUNK") or os.environ.get("KG_TRUNK")
    problems: list[str] = []

    # 「装的版本」和「跑的版本」是两件事：Codex 升级插件时替换版本目录，却不重启
    # 已经在跑的 MCP 进程。那些进程的 OPS_DIR 会指向已删除的目录，每次调用 kgctl
    # 都只拿到 can't open file —— 必须在这里变成明确的 broken，而不是静默假绿灯。
    stale = stale_code_problem()
    if stale:
        problems.append(stale)

    if not binding_path().exists():
        if stale:
            # 陈旧进程的 `.mcp.json` 跟着版本目录一起没了，但这**不是**「传统布局」；
            # 直接判 skip 就是拿假绿灯盖住「整条 MCP 已经哑了」。
            return {"status": "broken", "repo": repo_raw, "trunk": trunk_raw,
                    "problems": problems, "warnings": []}
        return {"status": "skip", "repo": None, "trunk": None,
                "problems": [], "warnings": []}

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


def _running_source_commit() -> str | None:
    """Which plugin source commit the running code was packaged from.

    Installed releases carry `.codex-autopilot-source.json`; a dev checkout usually
    does not. Missing metadata is None, never an inferred commit.
    """
    for candidate in (plugin_root() / ".codex-autopilot-source.json",
                      plugin_root().parent / ".codex-autopilot-source.json"):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        commit = str(data.get("commit") or "").strip()
        if commit:
            return commit
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
    installed_commit = None
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state_version = state.get("version")
            installed_commit = str(state.get("source_commit") or "").strip() or None
        except (OSError, ValueError):
            state_version = None
            installed_commit = None

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
    running_commit = None
    # 发布前自检跑在 .staging-<version>-pid 里，此时 install-state / marketplace 还是上一版，
    # 不该因为「正在跑临时目录」被判成不一致。
    if not plugin_root().name.startswith(".staging-"):
        running_version = _running_release_version()
        running_commit = _running_source_commit()

    versions = {"install_state": state_version, "marketplace": market_version,
                "running": running_version, "installed_commit": installed_commit,
                "running_commit": running_commit}

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
    if installed_commit and running_commit and installed_commit != running_commit:
        warnings.append(
            f"正在跑的代码来自 {running_commit[:12]}，install-state 写 "
            f"{installed_commit[:12]}；需要受控重载或换代，不能自动重启。")

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
