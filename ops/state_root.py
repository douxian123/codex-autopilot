#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""编排状态根：项目真相只有一处权威目录。

为什么需要这个模块
------------------
`ops/kgctl.py`（唯一入口 + 合并串行）和 `ops/truthctl.py`（真相台账）必须读写
同一份 `ops/tasks/inbox.jsonl`、`ops/truth/*.jsonl`。2026-10-12 之前两边各用各的
规则：kgctl 取 `git worktree list` 的第一项（共享检出），truthctl 取调用者所在的
worktree。于是同一个项目里长出两份互不相认的账本——

    kgctl status   -> 唯一入口 待路由 10 / 累计 10 条
    kgctl tick     -> route 说「没有待路由项」（它读的是另一个文件）

真实用户需求就卡在这条缝里：10 条停在共享检出、2 条躺在别的 worktree，谁都不知道
对方存在。两边共用本模块之后，不论从哪个 worktree、哪个对话调用，看到的都是同一份
状态；`KG_REPO` 仍然是最高优先的显式覆盖。

选择标准是「这个目录真的带着编排状态」，而不是「Git 说它是什么目录」——共享检出
的 ops/ 可能因为历史脏度不完整，那它就不配当状态根。

2026-10-13 起，运行时代码（kgctl.py / truthctl.py / central_mcp.py）不再要求待在
目标项目里：插件自带一份，目标项目只留 ops/tasks + ops/truth 这份状态。所以状态根
的判据从「有 kgctl.py 且有 tasks/truth」放宽成「有 tasks/truth」——否则自包含插件
在只放账本的项目里会认不出自己的状态根。
"""

import json
import os
import subprocess
from pathlib import Path


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def worktrees():
    """按 `git worktree list` 顺序返回所有检出目录（第一项 = 主工作树）。"""
    out = _git("worktree", "list", "--porcelain")
    if out.returncode != 0:
        return []
    return [Path(line[len("worktree "):].strip())
            for line in out.stdout.splitlines() if line.startswith("worktree ")]


def main_worktree() -> Path:
    """共享检出（git 主工作树）。只用来读诊断信息和当软链源，不当状态根。"""
    wts = worktrees()
    if wts:
        return wts[0].resolve()
    out = _git("rev-parse", "--show-toplevel")
    if out.returncode == 0 and out.stdout.strip():
        return Path(out.stdout.strip()).resolve()
    return Path.cwd().resolve()


def current_worktree() -> Path:
    out = _git("rev-parse", "--show-toplevel")
    if out.returncode == 0 and out.stdout.strip():
        return Path(out.stdout.strip()).resolve()
    return Path(__file__).resolve().parents[1]


def looks_like_state_root(path: Path) -> bool:
    """真正的状态根 = 同时带着输入账本目录和真相目录。

    运行时代码不参与判定：插件自带的 ops/ 负责执行，项目里的 ops/ 只负责存账。
    """
    path = Path(path)
    return ((path / "ops" / "tasks").is_dir()
            and (path / "ops" / "truth").is_dir())


def has_usable_config(path: Path) -> bool:
    """候选树是否带着可用项目配置。

    `ops/tasks` 和 `ops/truth` 已签入版本库，每个 worktree 都像状态根；真正能区分
    活状态根的是 `ops/central.config.json`。配置不存在或坏掉时按不可用处理，不能
    静默退回内置默认（否则 land 门禁会在 merge 之后才失败）。
    """
    path = Path(path)
    config = path / "ops" / "central.config.json"
    if not config.is_file():
        return False
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    return isinstance(data, dict)


def _head_commit_time(path: Path) -> int:
    """HEAD commit 的 Unix 秒；取不到时返回 0，不把「不知道」说成「最新」。"""
    out = _git("log", "-1", "--format=%ct", cwd=path)
    if out.returncode != 0:
        return 0
    text = out.stdout.strip()
    return int(text) if text.isdigit() else 0


def resolve_state_root() -> Path:
    env = os.environ.get("KG_REPO")
    if env:
        return Path(env).expanduser().resolve()

    main = main_worktree()
    trunk = Path(os.environ.get("KG_TRUNK",
                                str(main.parent / (main.name + "-trunk")))).expanduser()
    candidates = [main, trunk, current_worktree()]
    usable = []
    for cand in candidates:
        if looks_like_state_root(cand):
            usable.append((cand.resolve(), has_usable_config(cand),
                           _head_commit_time(cand)))

    configured = [item for item in usable if item[1]]
    if configured:
        # 配置可用优先；同为可用配置时按 HEAD 时间选最新，避免停在旧分支的
        # 共享检出压过活的主干工作树。同时间按候选顺序取第一个，保持确定性。
        return max(configured, key=lambda item: item[2])[0]

    for cand in candidates:
        if looks_like_state_root(cand):
            return cand.resolve()

    # 都不像状态根时给一个确定答案，让调用方在错误信息里能打印出来。
    return main.resolve()
