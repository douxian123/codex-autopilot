#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Resolve the project supplied by one MCP call."""

from __future__ import annotations

import subprocess
from pathlib import Path

from state_root import looks_like_state_root


class RequestContextError(ValueError):
    pass


def _path(value: str | None) -> Path | None:
    text = str(value or "").strip()
    return Path(text).expanduser().resolve() if text else None


def _git_root(path: Path) -> Path | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return Path(proc.stdout.strip()).resolve()


def _ancestors(path: Path) -> list[Path]:
    return [path, *path.parents]


def resolve_project(*, project: str | None = None, cwd: str | None = None,
                    bound: str | None = None) -> Path:
    """Return the state root for one request, or raise instead of guessing."""
    explicit = _path(project)
    if explicit is not None:
        if not looks_like_state_root(explicit):
            raise RequestContextError(
                f"project 不是有效的自动挡项目状态根：{explicit}（缺少 ops/tasks 与 ops/truth）")
        return explicit

    request_cwd = _path(cwd)
    if request_cwd is not None:
        candidates = _ancestors(request_cwd)
        root = _git_root(request_cwd)
        if root is not None:
            candidates.extend(_ancestors(root))
        for candidate in candidates:
            if looks_like_state_root(candidate):
                return candidate
        raise RequestContextError(
            f"cwd 无法定位有效的自动挡项目：{request_cwd}（缺少 ops/tasks 与 ops/truth）；"
            "未写入任何项目账本")

    fallback = _path(bound)
    if fallback is not None and looks_like_state_root(fallback):
        return fallback
    raise RequestContextError(
        "无法确认当前项目归属；请在 MCP 调用中传入 project 或 cwd，未写入任何项目账本")
