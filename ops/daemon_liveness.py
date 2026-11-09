#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""daemon_liveness - 常驻守护进程是否还在跑（stdlib only）。

2026-11-10 实测：control.json 里 enabled=true、daemon_enabled=true，
面板显示「后台常驻=开」，但 launchd 里那个 job 早就没了：
    launchctl print gui/501/com.codex-autopilot.daemon
    -> Could not find service "com.codex-autopilot.daemon"
state.json 的 last_cycle_iso 停在八天前。开关全绿、界面正常、什么异常都不抛，
而自动挡其实一步都没动。这和「安装绑定失效」是同一类病 - 静默死亡。
本模块把这条判据抽出来，由 kgctl status / doctor 与面板共用一份。
"""

import os
import re
import subprocess
from pathlib import Path

DEFAULT_LABEL = "com.codex-autopilot.daemon"


def label() -> str:
    return os.environ.get("KG_DAEMON_LABEL", DEFAULT_LABEL)


def parse_launchctl_print(text: str) -> dict:
    """从 launchctl print 的文本取出 state / pid / 绑定项目（纯解析，可单测）。"""
    state = re.search(r"(?m)^\s*state = (\w+)\s*$", text or "")
    pid = re.search(r"(?m)^\s*pid = (\d+)\s*$", text or "")
    repo = re.search(r"(?m)^\s*KG_REPO => (.+?)\s*$", text or "")
    return {
        "state": state.group(1) if state else None,
        "pid": int(pid.group(1)) if pid else None,
        "bound_repo": str(Path(repo.group(1)).expanduser().resolve()) if repo else None,
    }


def _launchctl_print(launcher=None):
    """跑一次 launchctl print；拿不到回 (-1, "")，表示 unknown 而不是失败。"""
    uid = os.getuid() if hasattr(os, "getuid") else 0
    argv = ["launchctl", "print", "gui/{}/{}".format(uid, label())]
    if launcher is not None:
        return launcher(argv)
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""
    return p.returncode, p.stdout or ""


def is_macos() -> bool:
    """非 macOS 没有 launchd，守护进程本就不是这么托管的。"""
    try:
        return os.uname().sysname == "Darwin"
    except (AttributeError, OSError):
        return Path("/bin/launchctl").exists()


def daemon_liveness(repo=None, launcher=None) -> dict:
    """体检常驻守护进程。只读，绝不抛异常。"""
    code, out = _launchctl_print(launcher)

    if code == -1 or (code != 0 and not out.strip() and launcher is None
                      and not is_macos()):
        status = "unknown"
        info = {"state": None, "pid": None, "bound_repo": None}
    elif code != 0:
        status = "not_installed"
        info = {"state": None, "pid": None, "bound_repo": None}
    else:
        info = parse_launchctl_print(out)
        live = info["state"] == "running" and info["pid"] is not None
        status = "running" if live else "loaded_not_running"

    want = str(Path(repo).expanduser().resolve()) if repo else None
    matches = None
    if status == "running" and info.get("bound_repo") and want:
        matches = info["bound_repo"] == want

    return {
        "status": status,
        "live": status == "running",
        "pid": info.get("pid"),
        "bound_repo": info.get("bound_repo"),
        "repo": want,
        "matches_repo": matches,
    }


def liveness_line(state: dict, daemon_enabled=None) -> str:
    """压成一行人话，给 kgctl status 直接用。"""
    st = state.get("status")
    if st == "running":
        line = "常驻守护       OK（pid {}".format(state.get("pid"))
        if state.get("bound_repo"):
            line += "，绑定 {}".format(state["bound_repo"])
        if state.get("matches_repo") is False:
            line += " [!] 与你当前项目不一致"
        return line + "）"
    if st == "not_installed":
        if daemon_enabled is False:
            return "常驻守护       未安装（用户级开关也关着，符合预期）"
        return ("常驻守护       未运行：launchd 里没有这个 job - "
                "开关显示「开」但没有进程接活")
    if st == "loaded_not_running":
        return "常驻守护       未运行：job 在但没起来（pid 缺失，可能崩溃后未重启）"
    return "常驻守护       看不清（拿不到 launchctl 信息）- 不当作正常"


def liveness_problem(state: dict, daemon_enabled=None):
    """doctor 用的判定：什么时候该把它当「需要处理」。"""
    st = state.get("status")
    if st == "running":
        if state.get("matches_repo") is False:
            return ("④ 常驻守护进程：在跑，但绑定的是 {}，不是你当前项目 {} - "
                    "它在替别的账本干活".format(state.get("bound_repo"), state.get("repo")))
        return None
    if st == "not_installed":
        if daemon_enabled is False:
            return None
        return ("④ 常驻守护进程：launchd 里没有这个 job，自动挡不会自己跑"
                "（tick / 落盘 / 收口都不会发生）。装回："
                "python3 ops/central_daemon.py --install")
    if st == "loaded_not_running":
        return ("④ 常驻守护进程：job 在但没起来（pid 缺失）- 崩溃后没有重启；"
                "自动挡当前不在动。")
    return ("④ 常驻守护进程：拿不到 launchctl 信息，无法判断它是否在跑 - "
            "这不是「正常」，是「看不清」。")
