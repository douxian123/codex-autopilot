#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""project_router —— 「万法归一」入口后面的归属判定（stdlib only）。

要解决的真实问题
----------------
插件安装时 `.mcp.json` 里写死了一个 `KG_REPO`。于是不管用户在哪个窗口、说的是哪个
项目，`inbox` / `intake` 都会落进那一个账本：用户在 A 项目窗口里说 B 项目的事，记录
却进了 A。用户的原话是「为什么我在别的窗口说别的东西都会落到另一个账本里…这不对呀」。

这一层把「归属判定」从「安装时写死」改成「每次投稿现算」：

  1. 注册表（`~/.codex-autopilot/projects.json`）记录已知项目：名字、状态根、识别线索；
  2. `match()` 按**分层证据**判定（不是加权求和，理由见下）；
  3. 唯一胜出 → 路由到它；说不清 → `ambiguous`，**不猜**；
  4. 谁都不像 → `none`，交回调用方决定（默认仍是当前项目，保持旧行为）。

为什么是分层而不是加权求和
--------------------------
2026-11-16 冒烟实测：加权求和在「alpha项目和beta项目都要改缓存」上给出 47 比 46，
靠名字长度那 1 分差就把活判给了 alpha —— 用户同时点了两个项目，系统却装作看懂了。
同一类证据内部的 1 分差没有任何含义，跨类相加更会把「用户人在哪个窗口」这种环境
信号，压过「用户原话点名了哪个项目」这种意图信号。

所以改成按证据强度分层，只在同一层里比较：

  L1 原话点名（项目名 / 别名）—— 用户明确说了是哪个项目，最强
  L2 文件路径                  —— 具体到文件，次强
  L3 cwd                       —— 只是「他人在哪儿」，环境信号
  L4 自定义线索词              —— 最容易碰巧命中的模糊词，最弱

取第一个非空的层；该层只有一个候选才算 resolved，多个接近的一律 ambiguous。

硬规则
------
**归属不明时不许静默落进当前账本。** 猜错比说「我不确定」贵得多 —— 活会被派去改
错的仓库。判成 `ambiguous` 的记录进全局待路由箱（`~/.codex-autopilot/routing/inbox.jsonl`），
等人或后续规则认领，不混进任何一个项目的真相层。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import state_root

DEFAULT_ROOT = Path.home() / ".codex-autopilot"
# 打分门槛：低于它的匹配不算「认识这个项目」。单字线索、泛词都不该触发路由。
MIN_SCORE = 40
# 近似并列：两条文本线索的分数区间只差 (40+2)~(40+20)，靠 1 分之差「胜出」纯属
# 名字长度的偶然。2026-11-16 冒烟实测：「alpha项目和beta项目都要改缓存」被判给
# alpha（47 vs 46）——用户同时点了两个项目，系统却装作看懂了。凡是最高分和次高分
# 咬在这么近的范围内，就是「说不清」，必须交出来而不是猜。
NEAR_TIE_MARGIN = 15


def _home() -> Path:
    """注册表 / 待路由箱的家。尊重代码库里既有的 `KG_DAEMON_HOME`，

    好处是测试只要把 `KG_DAEMON_HOME` 指到临时目录，就同时隔离了运行态和这里，
    不会把临时状态根写进用户真实的 `~/.codex-autopilot/projects.json`。
    """
    raw = (os.environ.get("KG_AUTOPILOT_HOME")
           or os.environ.get("KG_DAEMON_HOME")
           or str(DEFAULT_ROOT))
    return Path(raw).expanduser()


def registry_path() -> Path:
    return Path(os.environ.get("KG_PROJECTS_REGISTRY",
                               str(_home() / "projects.json"))).expanduser()


def routing_inbox_path() -> Path:
    return Path(os.environ.get("KG_ROUTING_INBOX",
                               str(_home() / "routing" / "inbox.jsonl"))).expanduser()


def load_registry(path=None) -> list:
    """读注册表。文件不存在 / 内容坏都回空表 —— 不把「没注册过」当成错误。"""
    p = Path(path) if path else registry_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("projects") or []
    if not isinstance(data, list):
        return []
    out = []
    for row in data:
        if not isinstance(row, dict) or not row.get("name") or not row.get("root"):
            continue
        row = dict(row)
        row["root"] = str(Path(str(row["root"])).expanduser().resolve())
        row["clues"] = [str(c) for c in (row.get("clues") or []) if str(c).strip()]
        row["aliases"] = [str(a) for a in (row.get("aliases") or []) if str(a).strip()]
        out.append(row)
    return out


def save_registry(projects, path=None) -> Path:
    p = Path(path) if path else registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps({"projects": projects}, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(p)
    return p


def register(name, root, clues=None, aliases=None, path=None) -> dict:
    """登记一个项目。同一个状态根只留一条，重复登记是更新而不是追加。"""
    root_resolved = str(Path(str(root)).expanduser().resolve())
    projects = load_registry(path)
    merged = None
    for row in projects:
        if row["root"] == root_resolved:
            merged = row
            break
    if merged is None:
        merged = {"name": str(name), "root": root_resolved, "clues": [], "aliases": []}
        projects.append(merged)
    else:
        merged["name"] = str(name)
    for c in (clues or []):
        if str(c).strip() and str(c) not in merged["clues"]:
            merged["clues"].append(str(c))
    for a in (aliases or []):
        if str(a).strip() and str(a) not in merged["aliases"]:
            merged["aliases"].append(str(a))
    save_registry(projects, path)
    return merged


def ensure_registered(root, name=None, clues=None, path=None) -> dict | None:
    """把当前状态根登记进注册表（幂等）。只在它真的像状态根时才登记。

    为什么要在投稿路径里做这件事：只按「安装时写死的那一个项目」判归属，用户换个
    项目说话时系统根本没有第二个候选可比。当前项目至少要在候选里，`match()` 才有
    机会用 cwd / 文件路径把它和别的项目区分开。

    **不许改已有条目的名字**：自动登记只知道目录名，而用户可能已经用 `projects
    register` 显式给过它一个好认的名字。2026-11-16 冒烟实测就踩到了这条 ——
    `ensure_registered` 把 `alpha` 覆盖成目录名 `stateA`，于是「候选：stateA、beta」
    这种半截名字直接暴露给用户。
    """
    try:
        p = Path(str(root)).expanduser().resolve()
    except (OSError, ValueError):
        return None
    try:
        if not state_root.looks_like_state_root(p):
            return None
    except OSError:
        return None
    root_resolved = str(p)
    for row in load_registry(path):
        if row["root"] == root_resolved:
            return row  # 已登记过：保留用户给的名字和线索，只当候选
    return register(name or p.name, p, clues=clues, path=path)


def resolve_hint(hint, registry=None) -> dict | None:
    """把调用方给的 project 提示（名字 / 别名 / 状态根路径）解析成一条注册项。"""
    h = _norm(hint)
    if not h:
        return None
    registry = registry if registry is not None else load_registry()
    for p in registry:
        if h == _norm(p["name"]) or h == _norm(p["root"]):
            return p
    for p in registry:
        names = [_norm(p["name"]), *(_norm(a) for a in (p.get("aliases") or []))]
        if h in names or any(n and n in h for n in names):
            return p
    for p in registry:
        try:
            if _is_under(hint, p["root"]):
                return p
        except (OSError, ValueError):
            continue
    return None


def _norm(value) -> str:
    return str(value or "").strip().lower()


def _longest_hit(tokens, text_l) -> str:
    """在文本里找最长的一个线索词。越长越具体，所以命中「最长的那个」。"""
    hit = ""
    for token in tokens:
        t = _norm(token)
        if len(t) >= 2 and t in text_l and len(t) > len(hit):
            hit = t
    return hit


def _is_under(path, root) -> bool:
    try:
        p = Path(str(path)).expanduser().resolve()
        r = Path(str(root)).expanduser().resolve()
    except (OSError, ValueError):
        return False
    return p == r or r in p.parents


def _entry(project, score, reason) -> dict:
    return {"name": project["name"], "root": project["root"],
            "score": score, "reasons": [reason]}


def _pick(entries, layer, min_score) -> dict:
    """在**同一层证据**里挑唯一胜出者。并列或咬得太近 → ambiguous，不猜。"""
    entries = sorted(entries, key=lambda r: (-r["score"], r["name"]))
    top = entries[0]
    if top["score"] < min_score:
        return {"decision": "none", "project": None, "root": None,
                "score": top["score"], "layer": layer, "reasons": [],
                "candidates": entries}
    runner_up = entries[1]["score"] if len(entries) > 1 else None
    if runner_up is not None and top["score"] - runner_up < NEAR_TIE_MARGIN:
        close = [r for r in entries if top["score"] - r["score"] < NEAR_TIE_MARGIN]
        return {"decision": "ambiguous", "project": None, "root": None,
                "score": top["score"], "layer": layer, "reasons": [],
                "candidates": close}
    return {"decision": "resolved", "project": top["name"], "root": top["root"],
            "score": top["score"], "layer": layer, "reasons": top["reasons"],
            "candidates": entries}


def match(text, files=None, registry=None, cwd=None, min_score=MIN_SCORE) -> dict:
    """判定这条投稿属于哪个项目。只做判定，不写任何文件。

    返回：`{"decision": "resolved|ambiguous|none", "project": <name|None>,
            "root": <path|None>, "score": int, "layer": str,
            "reasons": [...], "candidates": [...]}`。
    """
    registry = registry if registry is not None else load_registry()
    text_l = _norm(text)
    files = [str(f) for f in (files or [])]
    named, filed, cwdded, hinted = [], [], [], []
    for p in registry:
        # L1 原话点名：项目名 / 别名。
        token = _longest_hit([p["name"], *(p.get("aliases") or [])], text_l)
        if token:
            named.append(_entry(p, 100 + min(len(token), 20),
                                "原话里点名了「%s」" % token))
        else:
            # L4 自定义线索词：最弱的证据，只在其它层全空时才轮到它。
            token = _longest_hit(p.get("clues") or [], text_l)
            if token:
                hinted.append(_entry(p, 40 + min(len(token), 20),
                                     "原话里出现了这个项目的线索词「%s」" % token))
        # L2 文件路径。
        fhits = [f for f in files if _is_under(f, p["root"])]
        if fhits:
            filed.append(_entry(p, 100, "文件路径属于这个项目：%s" % fhits[0]))
        # L3 cwd。
        if cwd and _is_under(cwd, p["root"]):
            cwdded.append(_entry(p, 100, "会话目录在这个项目里：%s" % p["root"]))

    for layer, entries in (("named", named), ("files", filed),
                           ("cwd", cwdded), ("clue", hinted)):
        if entries:
            return _pick(entries, layer, min_score)
    return {"decision": "none", "project": None, "root": None, "score": 0,
            "layer": None, "reasons": [], "candidates": []}


def stash_ambiguous(record, path=None) -> Path:
    """把分不清归属的投稿写进全局待路由箱（加法，不改任何项目账本）。"""
    p = Path(path) if path else routing_inbox_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    row = dict(record)
    row.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
    row["status"] = "ambiguous"
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return p


def discover(base, max_depth=2) -> list:
    """只读扫描：把某个目录下看起来像状态根的项目列出来，供 `projects scan` 用。"""
    base = Path(str(base)).expanduser().resolve()
    found = []
    if not base.is_dir():
        return found
    stack = [(base, 0)]
    while stack:
        cur, depth = stack.pop()
        try:
            if state_root.looks_like_state_root(cur):
                found.append({"name": cur.name, "root": str(cur)})
                continue
        except OSError:
            continue
        if depth >= max_depth:
            continue
        try:
            for child in cur.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    stack.append((child, depth + 1))
        except OSError:
            continue
    return sorted(found, key=lambda r: r["root"])
