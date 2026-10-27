#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression: the launchd reaper entry must forward to the installed version.

Why (measured 2026-10-27):
  `com.danta.codex-disk-reap` runs `~/.codex-autopilot/ops/disk_reap.py` directly,
  every hour. That path is NOT inside an immutable release directory, so nothing
  in install.sh ever wrote it. It sat at whatever version was hand-copied into
  place weeks earlier -- while the release directory moved on.

  Consequence seen in the real logs: orphan-residue reclamation landed in
  `releases/<version>/ops/`, the daemon path started using it (its log line shows
  the new step), but the hourly path still printed only
  "worktree / kg tmp" -- the new step never executed at all.

  Fix: the entry becomes a thin forwarder with no logic of its own. These
  assertions keep it that way, because "someone helpfully re-copies a full
  implementation in here" is exactly how it rots again.
"""
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
REPO = OPS.parent
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


def extract_entry(install_sh: str) -> str:
    """Pull the forwarder body out of install.sh's heredoc."""
    start = install_sh.index("<<'ENTRYEOF'")
    start = install_sh.index("\n", start) + 1
    end = install_sh.index("\nENTRYEOF", start)
    return install_sh[start:end] + "\n"


def main():
    install_sh = (REPO / "install.sh").read_text(encoding="utf-8")
    check("install.sh 里存在这个入口的生成步骤", "<<'ENTRYEOF'" in install_sh)

    body = extract_entry(install_sh)

    # 1. It must not contain any reaping logic of its own. The moment it grows a
    #    second copy of the implementation, the two drift apart again.
    for forbidden in ("_reap_dead_worktrees", "worktree_removable", "orphan_residue",
                      "shutil.rmtree", "_reap_kg_tmp"):
        check(f"入口里不许自带实现（{forbidden}）", forbidden not in body)

    # 2. It must be a byte-identical forward: runpy with the resolved target.
    check("入口是 runpy 转发", "runpy.run_path" in body)
    check("入口会把 argv[0] 换成目标脚本", "sys.argv = [str(target), *sys.argv[1:]]" in body)

    # 3. It must fail loudly when the installed version cannot be found. Silently
    #    returning 0 would make "reclamation is running" a lie.
    check("找不到安装版本时非零退出", "return 1" in body)
    check("找不到安装版本时打印原因", "找不到当前安装版本" in body)

    # 4. Functional proof: write the forwarder to a temp HOME with a fake
    #    installed release and confirm it actually reaches the release's script.
    with tempfile.TemporaryDirectory(prefix="reap-entry-") as tmp:
        home = Path(tmp)
        release = home / "plugins/codex-autopilot/releases/9.9.9"
        (release / "ops").mkdir(parents=True)
        (release / "ops" / "disk_reap.py").write_text(
            "import sys\n"
            "print('RELEASE_RAN', sys.argv[1:])\n"
            "raise SystemExit(7)\n",
            encoding="utf-8")
        (home / "plugins/codex-autopilot").mkdir(parents=True, exist_ok=True)
        (home / "plugins/codex-autopilot/install-state.json").write_text(
            json.dumps({"version": "9.9.9",
                        "source": "./plugins/codex-autopilot/releases/9.9.9"}),
            encoding="utf-8")
        entry = home / "entry.py"
        entry.write_text(body, encoding="utf-8")
        env = {**os.environ, "HOME": str(home)}
        r = subprocess.run([sys.executable, str(entry), "--dry-run"],
                           capture_output=True, text=True, env=env)
        check("转发后真的执行了安装版本里的脚本",
              "RELEASE_RAN" in r.stdout and "--dry-run" in r.stdout,
              f"stdout={r.stdout!r} stderr={r.stderr[-200:]!r}")

        # 5. No installed version -> loud failure, not a silent no-op.
        (home / "plugins/codex-autopilot/install-state.json").write_text(
            json.dumps({"version": "9.9.9", "source": "./plugins/nonexistent"}),
            encoding="utf-8")
        r2 = subprocess.run([sys.executable, str(entry)], capture_output=True, text=True, env=env)
        check("没有安装版本时退出码非零", r2.returncode != 0, f"rc={r2.returncode}")
        check("没有安装版本时不装成功", "找不到当前安装版本" in (r2.stderr + r2.stdout),
              f"stderr={r2.stderr[-200:]!r}")

    # 6. The daemon path and the hourly path must agree on *which* file they use:
    #    both resolve through install-state, neither hardcodes an old dir.
    disk_reap = (OPS / "disk_reap.py").read_text(encoding="utf-8")
    check("实现副本仍按 install-state 解析当前版本",
          "_resolve_ops" in disk_reap and "install-state.json" in disk_reap)

    if failures:
        print("\n失败项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\nreap entry forwarding unit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
