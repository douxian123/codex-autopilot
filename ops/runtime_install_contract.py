#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""现场生效判据：已装版本必须同时满足版本、commit、plist 与 live 路径一致。

源码合入 main 只证明代码进了仓库，不等于本机已装版本和 daemon 已经吃到。
2026-11-22 的宪法页修复出现过「源码已合入、当前安装版仍不含修复」的缺口；
这一模块把换代后的回读要求固化成可复用判据，供门禁和人工排障共用。

只读：不改 install-state、plist、launchd 或服务进程。
"""

from __future__ import annotations

import json
import plistlib
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
PLUGIN_ROOT = HOME / "plugins" / "codex-autopilot"
INSTALL_STATE = PLUGIN_ROOT / "install-state.json"
PLIST = HOME / "Library" / "LaunchAgents" / "com.codex-autopilot.daemon.plist"


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _plist_script() -> str | None:
    try:
        with PLIST.open("rb") as fh:
            data = plistlib.load(fh)
    except (OSError, ValueError, TypeError):
        return None
    args = data.get("ProgramArguments") or []
    scripts = [str(a) for a in args if str(a).endswith("central_daemon.py")]
    return scripts[0] if scripts else None


def _launchctl_pid() -> str | None:
    try:
        proc = subprocess.run(["launchctl", "list"], capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "com.codex-autopilot.daemon":
            return parts[0] if parts[0].isdigit() else None
    return None


def _pid_script(pid: str | None) -> str | None:
    if not pid or not pid.isdigit():
        return None
    try:
        proc = subprocess.run(["ps", "-p", pid, "-o", "command="],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    parts = proc.stdout.split()
    scripts = [p for p in parts if p.endswith("central_daemon.py")]
    return scripts[0] if scripts else None


def check() -> dict:
    """回读现场；unknown 与 0/不一致严格分开，不把取数失败说成坏状态。"""
    state = _read_json(INSTALL_STATE)
    version = str((state or {}).get("version") or "") or None
    source = str((state or {}).get("source") or "") or None
    commit = str((state or {}).get("source_commit") or "") or None
    release_root = PLUGIN_ROOT
    meta = None
    source_path = None
    if source:
        source_path = (HOME / source.lstrip("./")).resolve()
        meta = _read_json(source_path / ".codex-autopilot-source.json")
    meta_version = str((meta or {}).get("version") or "") or None
    meta_commit = str((meta or {}).get("commit") or "") or None
    meta_dirty = (meta or {}).get("dirty")
    plist_script = _plist_script()
    pid = _launchctl_pid()
    pid_script = _pid_script(pid)
    expected_script = str(source_path / "ops" / "central_daemon.py") if source_path else None

    unknowns = []
    if state is None:
        unknowns.append("install-state 取不到或 JSON 损坏")
    if source and meta is None:
        unknowns.append("版本目录来源元数据取不到或 JSON 损坏")
    if plist_script is None:
        unknowns.append("daemon plist 取不到脚本路径")
    if pid is None:
        unknowns.append("launchctl 取不到 daemon PID")
    if pid and pid_script is None:
        unknowns.append("ps 取不到 daemon 命令行")

    failures = []
    if version and source and not source.endswith(version):
        failures.append("install-state 版本与 source 路径不一致")
    if commit and meta_commit and commit != meta_commit:
        failures.append("install-state commit 与版本目录元数据不一致")
    if meta_version and version and meta_version != version:
        failures.append("版本目录版本与 install-state 不一致")
    if meta_dirty is True:
        failures.append("版本目录 dirty=true，不是可追溯不可变发布")
    if expected_script and plist_script and expected_script != plist_script:
        failures.append("daemon plist 未指向当前安装版本")
    if expected_script and pid_script and expected_script != pid_script:
        failures.append("live daemon 仍在跑旧版本代码")

    if unknowns:
        status = "unknown"
    elif failures:
        status = "stale"
    else:
        status = "ok"
    return {
        "status": status,
        "version": version,
        "commit": commit,
        "source": source,
        "plist_script": plist_script,
        "pid": pid,
        "pid_script": pid_script,
        "unknown": unknowns,
        "failures": failures,
    }


def main() -> int:
    report = check()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] == "ok":
        return 0
    if report["status"] == "unknown":
        print("现场生效判据取不到，不能当作不生效或生效。", file=sys.stderr)
    else:
        print("现场生效判据失败：当前安装版与 daemon 不是同一代。", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
