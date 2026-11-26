#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上下文管家：项目约束台账（constraints.jsonl）的读取与「按 scope 精准匹配」。

一条约束从用户在任意入口说过的一句话里蒸馏出来，记成：

    {id, scope, statement, source, kind, enforceable, policy_version, status, supersedes}

- `scope`：管哪些路径/模块（glob 列表）。这是「精准调取」的根据——实现某个改动时，
  只带和它相关的约束，不是把全部上下文喂进去。
- `kind`：expectation(预期) / rework(返修) / decision(决策) / taboo(禁忌)。
- `enforceable`：none(纯记录) / gate(有机器门禁) / checklist(人验收项)。
  这个字段诚实说明哪条约束其实没有机器兜底。

本模块只做「读 + 匹配 + 渲染」，不写文件；写入走 `truthctl constraints add`，
沿用同一条追加式台账与文件锁，不另造第二套真相。
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path

KINDS = ("expectation", "rework", "decision", "taboo")
ENFORCEABLE = ("none", "gate", "checklist")

KIND_LABEL = {
    "expectation": "预期", "rework": "返修", "decision": "决策", "taboo": "禁忌",
    "预期": "预期", "返修": "返修", "决策": "决策", "禁忌": "禁忌",
}
ENFORCEABLE_LABEL = {
    "none": "无门禁", "gate": "有门禁", "checklist": "人验收",
    "无门禁": "无门禁", "有门禁": "有门禁", "人验收": "人验收",
}

# 标了 gate 却没写命令：这不是「有门禁」，是「声称有、实际没有」。
GATE_UNWIRED = "门禁未接"


def read_rows(path) -> list:
    """读 JSONL；坏行跳过，不因为一行坏就整份取不到。"""
    path = Path(path)
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def active_constraints(path) -> list:
    """追加式台账按 id 收敛到最新，只留 status=active 的。"""
    latest = {}
    for row in read_rows(path):
        rid = row.get("id")
        if rid:
            latest[rid] = row
    return [r for r in latest.values() if (r.get("status") or "active") == "active"]


def normalize_scope(scope) -> list:
    """scope 允许 list / ':' / ',' 分隔的字符串；统一成非空字串列表。"""
    if scope is None:
        return []
    if isinstance(scope, (list, tuple)):
        items = scope
    else:
        items = str(scope).replace(",", ":").split(":")
    return [str(s).strip() for s in items if str(s).strip()]


def _glob_matches(glob: str, path: str) -> bool:
    glob = glob.strip().replace("\\", "/")
    path = path.strip().replace("\\", "/")
    if not glob or not path:
        return False
    if fnmatch.fnmatch(path, glob):
        return True
    # scope 写成目录时（backend）命中该目录下的所有文件（backend/...）
    prefix = glob.rstrip("*").rstrip("/")
    if prefix and (path == prefix or path.startswith(prefix + "/")):
        return True
    # scope 只写模块名 / 文件名（无斜杠、无通配）时按路径段命中
    if "/" not in glob and "*" not in glob:
        if path.rsplit("/", 1)[-1] == glob:
            return True
        if ("/" + glob + "/") in ("/" + path + "/"):
            return True
    return False


def scope_matches(scope, files) -> bool:
    """这条约束的 scope 是否命中给定的改动文件集合。

    约定：**scope 为空的约束是全局约束，命中一切**（例如「永远不要 --force push」）。
    这是有意的——全局红线不该因为没写 scope 就被漏掉。
    """
    globs = normalize_scope(scope)
    if not globs:
        return True
    files = normalize_scope(files)
    if not files:
        return False
    for g in globs:
        for f in files:
            if _glob_matches(g, f):
                return True
    return False


def matching_constraints(path, files) -> list:
    """按改动文件集合挑出相关约束；全局约束恒命中。"""
    return [r for r in active_constraints(path) if scope_matches(r.get("scope"), files)]


def gate_commands(path, files) -> list:
    """把 enforceable=gate 且真写了命令的约束，转成落盘门禁。

    只有「gate + 有 cmd」才算门禁。标了 gate 却没命令的走 `gate_gaps()`，
    单独如实报出来——旧实现把这类也算「有门禁」，面板与 CLI 都在撒谎。
    """
    out, seen = [], set()
    for r in matching_constraints(path, files):
        if str(r.get("enforceable") or "").strip() != "gate":
            continue
        cmd = str(r.get("cmd") or "").strip()
        if not cmd:
            continue
        name = f"constraint-{r.get('id')}" if r.get("id") else "constraint-gate"
        cwd = str(r.get("cwd") or ".").strip() or "."
        key = (name, cmd, cwd)
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "cmd": cmd, "cwd": cwd,
                    "source": "constraint", "constraint": r.get("id")})
    return out


def gate_gaps(path, files) -> list:
    """标了 gate 但没写命令的约束：它们其实没有机器兜底，必须如实说。"""
    return [r for r in matching_constraints(path, files)
            if str(r.get("enforceable") or "").strip() == "gate"
            and not str(r.get("cmd") or "").strip()]


def enforceable_label(row) -> str:
    """这条约束的兜底实在哪一档。gate 但没命令的回答是「门禁未接」，不是「有门禁」。"""
    raw = str((row or {}).get("enforceable") or "").strip()
    if raw == "gate" and not str((row or {}).get("cmd") or "").strip():
        return GATE_UNWIRED
    return ENFORCEABLE_LABEL.get(raw, "")


def render_line(row) -> str:
    kind = KIND_LABEL.get(str(row.get("kind") or ""), str(row.get("kind") or "预期"))
    enf = enforceable_label(row)
    tag = f"{kind}/{enf}" if enf else kind
    scope = normalize_scope(row.get("scope"))
    where = f"（范围：{', '.join(scope)}）" if scope else "（全局）"
    return f"- `{row.get('id', '')}` [{tag}] {row.get('statement') or row.get('title', '')}{where}"
