#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exec_overlap —— 并发施工项的「文件占地」判据（stdlib only，无 I/O）。

为什么需要这一层
----------------
自动挡可以同时开多个 worker，每个 worker 有自己的 worktree。worktree 隔离解决了
「互相踩文件」，但没有解决**同一件事被做两遍**：两条需求都声明要改
`ops/central_daemon.py`，两个 worker 各自改一版，落盘时必有一条白干、或者变成
一个语义冲突要人来拆。用户提过这是 PM 管家该管的判断：

  「如果有多个开工施工项的对话，或许它们不相互影响，或许它们的工作区和修改区域
   是重合的……要判断重复项，规划最优雅的施工路线，让它们的修改不会相互冲突和覆盖。」

所以派发前多一道判据：新的一条要改的文件，如果和**已经在改这些文件**的条目重叠，
就先不发新会话，排在后面等前一件落地。这是调度，不是阻塞，所以：

  * 判据每拍重算，不落成永久状态（见 central_daemon 里 overlap_hold 的自愈）；
  * 没有文件线索时**不做判断**，也不假装「不重叠」——把「判不了」交回调用方记录；
  * 只挡「要开新会话」的条目，已经在途的成果照常走收口门禁。

本模块不做 I/O、不读文件、不调子进程：给事实，还结论。这样才能被单测直接穷举。
"""

from __future__ import annotations

from typing import Any, Iterable

REASON_OVERLAP_HOLD = "overlap_hold"


def declared_files(item: Any) -> list[str]:
    """取一条需求声明的文件线索，归一成可比较的仓库相对路径。

    台账里 `files` 允许缺失（很多需求只写一句话），那就返回空表 —— 表示
    「不知道会改哪些文件」，不是「不会改任何文件」。调用方必须区分这两件事。
    """
    if not isinstance(item, dict):
        return []
    raw = item.get("files") or []
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    for value in raw:
        text = str(value or "").strip()
        while text.startswith("./"):
            text = text[2:]
        text = text.lstrip("/")
        if text:
            out.append(text)
    return out


def overlap_hold(item: Any, holders: Iterable[tuple[str, list[str]]]) -> dict | None:
    """新条目是否该因为「和正在改的条目撞文件」而排队。

    `holders` 是 `(条目 id, 它声明的文件)` 的序列：已经在跑会话的、以及已经有
    在途成果（未提交改动或未落提交）的条目。返回 None 表示可以派发。

    返回的结构直接能写进 `exec_blocked`，reason 固定为 `overlap_hold`，
    由 central_daemon 每拍重算后自愈，不会变成永久阻塞。
    """
    if not isinstance(item, dict):
        return None
    rid = item.get("id")
    files = declared_files(item)
    if not files or not rid:
        return None
    mine = set(files)
    hits = []
    for hid, hfiles in holders:
        if not hid or hid == rid:
            continue
        # 持有者那边也要归一化：守护传进来的已经是 declared_files() 的结果，
        # 但别处（CLI、单测、未来的调用方）可能直接传原始路径。两边口径不一致
        # 会让 `/ops/a.py` 和 `ops/a.py` 这种同一个文件漏判成"不重叠"。
        common = sorted(mine & set(declared_files({"files": hfiles})))
        if common:
            hits.append({"id": hid, "files": common})
    if not hits:
        return None
    files_involved = sorted({f for hit in hits for f in hit["files"]})
    return {
        "reason": REASON_OVERLAP_HOLD,
        "title": (item.get("title") or "")[:60],
        # 面板和日志只说「等谁、等哪几个文件」，不印整份台账。
        "holders": hits,
        "files": files_involved,
    }
