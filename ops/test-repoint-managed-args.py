#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression: 改指 managed 任务时不许把常驻参数吃掉。

真实事故（2026-10-27 实测）
--------------------------
`com.codex-autopilot.daemon` 的 ProgramArguments 是

    [python3.14, releases/<v>/ops/central_daemon.py, --loop, --interval, 15, --max-land, 3]

`repoint_launchd.py` 的 `class: managed` 分支把 desired 算成 `[str(script)]`，
于是改指后 plist 只剩一个 .py 路径：解释器没了、`--loop` 没了。launchd 直接
exec 一个 .py，报 exec format error 后放弃，守护进程静默停摆 —— 而脚本照样
打印「✓ 已改指并 reload」。用户感受是「自动挡突然不干活」，现场却全绿。

这条单测用假 HOME 造现场，锁死三件事：
  1. 改指只换脚本路径那一段，`--loop` 这类参数原样保留；
  2. 解释器沿用原来的绝对路径；
  3. 本来就该被清掉的 `--install` 不许留在常驻 argv 里。

不碰真实 launchd：plist 与版本目录全部造在临时目录。
"""
import json
import os
import plistlib
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS / "guards"))
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


def build_home(home: Path, version: str = "9.9.9") -> None:
    rel = f"plugins/codex-autopilot/releases/{version}"
    (home / rel / "ops").mkdir(parents=True, exist_ok=True)
    (home / rel / "ops/central_daemon.py").write_text("# daemon\n", encoding="utf-8")
    (home / "plugins/codex-autopilot/install-state.json").write_text(
        json.dumps({"version": version, "source": f"./{rel}"}), encoding="utf-8")
    (home / "Library/LaunchAgents").mkdir(parents=True, exist_ok=True)


def write_plist(home: Path, label: str, args: list) -> Path:
    p = home / "Library/LaunchAgents" / f"{label}.plist"
    p.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": args},
                                 sort_keys=False))
    return p


def main():
    with tempfile.TemporaryDirectory(prefix="repoint-managed-") as tmp:
        home = Path(tmp)
        build_home(home)
        old = home / "plugins/codex-autopilot/releases/9.8.8/ops/central_daemon.py"
        interp = sys.executable
        write_plist(home, "com.codex-autopilot.daemon", [
            interp, str(old), "--loop", "--interval", "15", "--max-land", "3",
        ])
        reg = home / "registry.json"
        reg.write_text(json.dumps({"entries": [{
            "label": "com.codex-autopilot.daemon", "script": "ops/central_daemon.py",
            "siblings": [], "interval_s": 0, "class": "managed",
        }]}), encoding="utf-8")
        os.environ["KG_GUARD_HOME"] = str(home)
        os.environ["KG_GUARD_REGISTRY"] = str(reg)
        import importlib
        repoint = importlib.import_module("repoint_launchd")
        importlib.reload(repoint)

        rows = repoint.plan(home)
        row = rows[0]
        new = home / "plugins/codex-autopilot/releases/9.9.9/ops/central_daemon.py"
        print(f"   desired = {row['desired']}")
        check("改指后的 argv 保留 --loop", "--loop" in row["desired"])
        check("改指后的 argv 保留 --interval 15",
              "--interval" in row["desired"] and "15" in row["desired"])
        check("改指后的 argv 保留 --max-land 3",
              "--max-land" in row["desired"] and "3" in row["desired"])
        check("脚本路径已换成当前版本", str(new) in row["desired"])
        check("旧版本路径已消失", str(old) not in row["desired"])
        check("解释器沿用原来的绝对路径", row["desired"][0] == interp,
              str(row["desired"][:1]))
        check("argv 长度与原 argv 相同（安装开关除外）",
              len(row["desired"]) == 7, f"len={len(row['desired'])}")

        # 幂等：改完之后再算一次，state 必须是 ok、desired 必须与 current 相等。
        write_plist(home, "com.codex-autopilot.daemon", row["desired"])
        importlib.reload(repoint)
        row2 = repoint.plan(home)[0]
        check("改指后立刻判为 ok（幂等，不重复 reload）", row2["state"] == "ok",
              row2["state"])
        check("改指后 desired 与 current 一致", row2["desired"] == row2["current"],
              f"{row2['desired']} vs {row2['current']}")

        # `--install` 是安装期开关：留在常驻 argv 里会让进程每次被拉起都重跑安装。
        write_plist(home, "com.codex-autopilot.daemon", [
            interp, str(old), "--install", "--loop",
        ])
        importlib.reload(repoint)
        row3 = repoint.plan(home)[0]
        check("--install 不会留在常驻 argv 里", "--install" not in row3["desired"],
              str(row3["desired"]))
        check("去掉 --install 后 --loop 仍在", "--loop" in row3["desired"],
              str(row3["desired"]))

    if failures:
        print(f"\n{len(failures)} 项失败")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nrepoint managed args: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
