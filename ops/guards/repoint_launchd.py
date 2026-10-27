#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本机 launchd 里「自动挡自己的」定时任务改指到当前安装版本。

为什么需要这个文件（真实事故，2026-10-27 实测）
------------------------------------------------
`com.danta.codex-disk-reap` 每小时直接执行 `~/.codex-autopilot/ops/disk_reap.py`。
那个路径不在任何不可变版本目录里，`install.sh` 的 cp 全都写不到它，于是它永远
停在第一次手工拷进来的那一版。修好的回收逻辑发版、守护进程那条路生效了，
但每小时这条独立路径还在跑几周前的代码 —— 新加的那一步从来没执行过。

同一病灶不止一处。本机还有 5 条同类任务（watchdog / 快照 / hooks 看门狗 /
3000 巡检 / 代理日志轮转），它们的脚本在 `~/.codex-guard/`、`~/.codex/hooks/`、
`~/.kg-guard/`、`~/.codex-proxy/` 里，全部手工维护、全部不在任何仓、全部
不会被 install 刷新。用户感受到的表象就是「跑着跑着又得清一轮」。

所以修法不是再补一个薄转发（那只能救一条），而是把这一类整体收编：
  1. 源文件搬进版本目录 `ops/guards/`（随发版一起不可变）；
  2. 本脚本按 `ops/guards/registry.json` 把对应 plist 的 ProgramArguments
     改指到 `releases/<version>/ops/...`，然后 reload；
  3. `--check` 只读核验，给门禁 `verify-launchd-entry-freshness.py` 用。

纪律
----
* 幂等：ProgramArguments 已相等就什么都不做（不 reload、不写文件）。
* 非破坏：改之前备份成 `<plist>.bak-<时间戳>-guard-repoint`。
* 显式：改了哪条、从哪改到哪逐条打印；找不到脚本就报错退出，不装作成功。
* 不越界：只碰 registry.json 里登记的 label，别人的 launchd 任务一律不动。
"""
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

OPS = Path(__file__).resolve().parent


def _home() -> Path:
    """允许门禁/单测用假的 HOME，避免碰到真实 launchd。"""
    raw = os.environ.get("KG_GUARD_HOME")
    return Path(raw).expanduser() if raw else Path.home()


def _registry_path() -> Path:
    """registry.json 的位置。单测可用环境变量指到临时目录。"""
    raw = os.environ.get("KG_GUARD_REGISTRY")
    return Path(raw).expanduser() if raw else OPS / "registry.json"


def _script_base() -> Path:
    """「当前安装版本」的根目录 —— 所有脚本路径都相对它解析。

    为什么不直接用 registry.json 所在目录：门禁和单测是在源码 worktree 里跑的，
    而真正被执行的是 `~/plugins/codex-autopilot/releases/<version>/`。如果拿源码
    目录当基准，门禁永远看不到「plist 还指着上一版」这个真问题 —— 那正是这次
    要根治的病。所以基准必须是 install-state 记录的那一版；读不到才退回本仓，
    并让调用方知道这是「降级判断」。
    """
    home = _home()
    state = home / "plugins/codex-autopilot/install-state.json"
    try:
        raw = json.loads(state.read_text(encoding="utf-8")).get("source") or ""
        if raw:
            cand = Path(raw).expanduser()
            if not cand.is_absolute():
                cand = home / raw.lstrip("./")
            if cand.is_dir():
                return cand
    except Exception:
        pass
    mkt = home / ".agents/plugins/marketplace.json"
    try:
        for plugin in (json.loads(mkt.read_text(encoding="utf-8")).get("plugins") or []):
            if isinstance(plugin, dict) and plugin.get("name") == "codex-autopilot":
                src = plugin.get("source")
                raw = src.get("path") if isinstance(src, dict) else src
                if raw:
                    cand = Path(str(raw)).expanduser()
                    if not cand.is_absolute():
                        cand = home / str(raw).lstrip("./")
                    if cand.is_dir():
                        return cand
    except Exception:
        pass
    return _registry_path().parent.parent.parent


def load_registry(path: Path | None = None) -> dict:
    return json.loads((path or _registry_path()).read_text(encoding="utf-8"))


def _plist_dir(home: Path) -> Path:
    return home / "Library" / "LaunchAgents"


def _existing_program_args(plist_path: Path) -> list[str]:
    try:
        data = plistlib.loads(plist_path.read_bytes())
    except Exception:
        return []
    args = data.get("ProgramArguments")
    if isinstance(args, list):
        return [str(a) for a in args]
    prog = data.get("Program")
    return [str(prog)] if prog else []


def desired_program_args(script: Path, current: list[str]) -> list[str]:
    """算出这条 plist 应该写成什么。

    解释器不换：沿用 plist 里已有的绝对路径解释器（前提是它还在）。
    这与 install.sh 里「重装要沿用已有解释器」是同一条纪律 —— 静默换掉别人
    能用的解释器，等于顺手改变别人的东西。
    """
    if script.suffix == ".sh":
        return ["/bin/bash", str(script)]
    interp = ""
    if current and Path(current[0]).exists() and "python" in current[0]:
        interp = current[0]
    if not interp:
        interp = shutil.which("python3") or "/usr/bin/python3"
    return [interp, str(script)]


def _reload(label: str, plist_path: Path) -> tuple[bool, str]:
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{label}"],
                   capture_output=True, text=True)
    r = subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        # 已经在跑时 bootstrap 会报 Input/output error；这不是失败，只是
        # 「它本来就在」。判据是下面 print 能不能看到这条 job。
        probe = subprocess.run(["launchctl", "print", f"{domain}/{label}"],
                               capture_output=True, text=True)
        if probe.returncode != 0:
            return False, (r.stderr or r.stdout or "").strip()[:400]
    subprocess.run(["launchctl", "enable", f"{domain}/{label}"],
                   capture_output=True, text=True)
    return True, ""


def plan(home: Path, only: list[str] | None = None) -> list[dict]:
    """产出每一登记项此刻是哪种状态。--check 与 --install 共用这一份判断。"""
    reg = load_registry()
    base = _script_base()
    pdir = _plist_dir(home)
    rows = []
    for entry in reg.get("entries") or []:
        label = entry["label"]
        if only and label not in only:
            continue
        plist_path = pdir / f"{label}.plist"
        script = base / entry["script"]
        row = {"label": label, "plist": plist_path, "script": script,
               "class": entry.get("class", "managed"),
               "state": "absent", "current": [], "desired": []}
        if not plist_path.exists():
            rows.append(row)
            continue
        row["current"] = _existing_program_args(plist_path)
        if entry.get("class") == "managed":
            # 由 install.sh 的 5.5 段负责；这里只核验它确实指着当前版本。
            ok = any(str(script) == arg for arg in row["current"])
            row["state"] = "ok" if ok else "stale"
            row["desired"] = [str(script)]
            rows.append(row)
            continue
        if not script.exists():
            row["state"] = "missing-script"
            rows.append(row)
            continue
        row["desired"] = desired_program_args(script, row["current"])
        row["state"] = "ok" if row["current"] == row["desired"] else "stale"
        rows.append(row)
    return rows


def check(home: Path, only: list[str] | None = None) -> int:
    bad = 0
    for row in plan(home, only):
        if row["state"] == "absent":
            print(f"  -        {row['label']}（本机没装，跳过）")
            continue
        if row["state"] == "ok":
            print(f"  ok       {row['label']}")
            continue
        bad += 1
        if row["state"] == "missing-script":
            print(f"  FAIL     {row['label']} 指向的脚本不在版本目录：{row['script']}")
        else:
            print(f"  FAIL     {row['label']} 仍在跑旧路径：{row['current']}")
            print(f"           应指向：{row['desired']}")
    if bad:
        print(f"\n{bad} 条定时任务没有跟上当前安装版本。")
        print("修法：在干净快照里跑一次 ./install.sh（它会重写这些 plist 并 reload）。")
        return 1
    print("\n所有登记的定时任务都指向当前安装版本。")
    return 0


def install(home: Path, only: list[str] | None = None,
            dry_run: bool = False) -> int:
    bad = 0
    changed = 0
    for row in plan(home, only):
        if row["state"] in ("absent", "ok"):
            continue
        if row["state"] == "missing-script":
            print(f"× {row['label']}：版本目录里没有 {row['script']}，先修打包再装。")
            bad += 1
            continue
        plist_path: Path = row["plist"]
        print(f"→ {row['label']}")
        print(f"   旧：{row['current']}")
        print(f"   新：{row['desired']}")
        if dry_run:
            changed += 1
            continue
        data = plistlib.loads(plist_path.read_bytes())
        backup = plist_path.with_name(
            plist_path.name + time.strftime(".bak-%Y%m%d-%H%M%S-guard-repoint"))
        backup.write_bytes(plist_path.read_bytes())
        data["ProgramArguments"] = row["desired"]
        tmp = plist_path.with_name(plist_path.name + ".tmp")
        tmp.write_bytes(plistlib.dumps(data, sort_keys=False))
        os.replace(tmp, plist_path)
        ok, err = _reload(row["label"], plist_path)
        if not ok:
            print(f"   × 已改文件但 reload 失败：{err}")
            print(f"     手工重试：launchctl bootout gui/$(id -u)/{row['label']}"
                  f" && launchctl bootstrap gui/$(id -u) {plist_path}")
            bad += 1
            continue
        print(f"   ✓ 已改指并 reload（旧 plist 备份：{backup.name}）")
        changed += 1
    if bad:
        return 1
    print(f"定时任务改指完成：{changed} 条已更新。")
    return 0


def main(argv: list[str]) -> int:
    mode = None
    only: list[str] = []
    dry = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--install", "--check"):
            mode = a.lstrip("-")
        elif a == "--dry-run":
            dry = True
        elif a == "--only":
            i += 1
            only.append(argv[i])
        else:
            print(f"未知参数：{a}", file=sys.stderr)
            return 2
        i += 1
    if mode is None:
        print(__doc__)
        return 2
    home = _home()
    if mode == "check":
        return check(home, only or None)
    return install(home, only or None, dry)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
