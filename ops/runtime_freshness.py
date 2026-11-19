#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""runtime_freshness —— 只读判断「源码 / 已装版本 / 运行代码 / daemon 是不是同一代」。

这是状态管家内部的一个判据，不是第八个管家。它只回答：
  * 主干源码 / 已装版本 / marketplace / 运行代码的版本是否一致；
  * 主干 commit / 已装 commit / 运行代码 commit 是否一致；
  * launchd plist、daemon 绑定和 daemon 代码是否跟随当前安装；
  * 判据不足是否只能报 unknown。

硬边界：不重启 Codex、不 reload launchd、不换代、不改配置、不杀进程、不派工单。
"""

from __future__ import annotations


def _version_from_script(script):
    """Extract a release version from a plist script path without touching disk."""
    parts = [str(p) for p in str(script or "").replace("\\", "/").split("/") if p]
    if "releases" in parts:
        idx = len(parts) - 1 - parts[::-1].index("releases")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    for i, part in enumerate(parts[:-1]):
        if part.startswith("codex-autopilot") and "." in part and part[0].isdigit():
            return part
        if part == "codex-autopilot" and parts[i + 1][0].isdigit():
            return parts[i + 1]
    return None


def evaluate(consistency=None, source_commit=None, daemon=None):
    consistency = consistency or {}
    daemon = daemon or {}
    versions = consistency.get("versions") or {}
    problems = list(consistency.get("problems") or [])
    warnings = list(consistency.get("warnings") or [])
    source_commit = str(source_commit or "").strip() or None

    installed = str(versions.get("install_state") or "").strip() or None
    running = str(versions.get("running") or "").strip() or None
    installed_commit = str(versions.get("installed_commit") or "").strip() or None
    running_commit = str(versions.get("running_commit") or "").strip() or None

    if consistency.get("status") == "skip" and not installed and not running:
        return {
            "status": "ok", "level": "unknown",
            "headline": "运行态判据取不到：当前是开发检出，没有安装版本锚点",
            "unknown": ["没有安装版本锚点"], "reasons": [], "action": "none",
        }

    if problems:
        return {
            "status": "ok", "level": "attention",
            "headline": "安装绑定不一致，运行态是否生效取不到",
            "unknown": [], "reasons": problems[:2], "action": "recommend_stable_reload",
        }

    if (installed and installed_commit and running_commit and source_commit
            and installed_commit != source_commit):
        return {
            "status": "ok", "level": "attention",
            "headline": "已装版本落后于主干源码，源码改动还没进入运行态",
            "unknown": [],
            "reasons": ["已装代码 %s，主干源码 %s；需要受控换代或重载后才能现场生效"
                        % (installed_commit[:12], source_commit[:12])],
            "action": "recommend_stable_upgrade",
        }

    mismatch = []
    if installed and running and installed != running:
        mismatch.append("运行版本 %s，已装版本 %s" % (running, installed))
    if installed_commit and running_commit and installed_commit != running_commit:
        mismatch.append("运行代码 %s，已装代码 %s"
                        % (running_commit[:12], installed_commit[:12]))
    mismatch.extend(warnings[:1])

    if consistency.get("status") != "skip":
        daemon_installed = daemon.get("installed")
        daemon_script = str(daemon.get("script") or "").strip() or None
        daemon_live = daemon.get("live")
        daemon_commit = str(daemon.get("source_commit") or "").strip() or None
        daemon_repo_matches = daemon.get("repo_matches")
        target_repo = str(daemon.get("target_repo") or "").strip() or None
        if daemon_installed is None or (daemon_live is None and daemon_script is None):
            return {
                "status": "ok", "level": "unknown",
                "headline": "运行态判据取不到：daemon 代际信息不完整",
                "unknown": ["daemon 是否跟随当前版本判据不足"], "reasons": [],
                "action": "none",
            }
        daemon_script_version = _version_from_script(daemon_script)
        if installed and daemon_script_version and daemon_script_version != installed:
            mismatch.append("daemon 指向 %s，已装版本 %s；需受控重装 daemon 才能跟随当前版本"
                            % (daemon_script_version, installed))
        if installed_commit and daemon_commit and installed_commit != daemon_commit:
            mismatch.append("daemon 代码来自 %s，已装代码 %s"
                            % (daemon_commit[:12], installed_commit[:12]))
        if daemon_live is True and daemon_repo_matches is False:
            mismatch.append("daemon 正在服务别的账本（%s），不是当前项目 %s"
                            % (daemon.get("bound_repo") or "取不到", target_repo or "取不到"))

    if mismatch:
        return {
            "status": "ok", "level": "attention",
            "headline": "运行代码尚未跟上已装版本，源码改动需要受控换代后才会现场生效",
            "unknown": [], "reasons": mismatch, "action": "recommend_stable_reload",
        }

    if (not installed or not running or not installed_commit
            or (source_commit and not running_commit)):
        return {
            "status": "ok", "level": "unknown",
            "headline": "运行态判据取不到：版本信息不完整",
            "unknown": ["版本信息不完整（主干/已装/运行 commit 任一取不到时不报正常）"],
            "reasons": [], "action": "none",
        }

    return {
        "status": "ok", "level": "ok",
        "headline": "运行代码与已装版本一致（%s）" % installed,
        "unknown": [], "reasons": [], "action": "none",
    }
