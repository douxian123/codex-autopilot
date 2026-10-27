#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression: launchd 入口新鲜度门禁必须真的能红。

为什么这条单测本身必要：门禁最容易的腐化方式是「永远绿」。一个只在真机上跑、
只断言「能跑完」的检查，会在某次文件搬家之后静默变成空转 —— 而它守护的正是
「没人会升级它」这个已经发生过两次的病。

所以这里用假 HOME 造三档现场，逐个断言：
  1. 版本目录里的任务        → 绿
  2. 登记过的薄转发          → 黄
  3. 手工维护的化石副本      → 红（必须失败）
  4. 薄转发里被塞进实现      → 红（薄转发一旦自带逻辑，就和实现各自腐烂）
  5. 安装版本缺文件          → 红
"""
import json
import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
GATE = OPS / "verify-launchd-entry-freshness.py"
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


def write_plist(path: Path, label: str, args: list[str], interval: int = 300) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps({
        "Label": label,
        "ProgramArguments": args,
        "StartInterval": interval,
    }, sort_keys=False))


def run_gate(home: Path, registry: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "KG_GUARD_HOME": str(home),
           "KG_GUARD_REGISTRY": str(registry)}
    return subprocess.run([sys.executable, str(GATE)],
                          capture_output=True, text=True, env=env)


FORWARDER_BODY = '''#!/usr/bin/env python3
import runpy, sys
from pathlib import Path

def main():
    target = Path.home() / "plugins/codex-autopilot/releases/9.9.9/ops/disk_reap.py"
    if not target.exists():
        sys.stderr.write("找不到当前安装版本")
        return 1
    sys.argv = [str(target), *sys.argv[1:]]
    runpy.run_path(str(target), run_name="__main__")
    return 0

if __name__ == "__main__":
    sys.exit(main())
'''


def build(home: Path, *, forwarder_dirty: bool = False,
          versioned_ok: bool = True, missing_version_file: bool = False,
          fossil_versioned: bool = False) -> Path:
    """造一个假的机器现场，返回 registry 路径。"""
    rel = "plugins/codex-autopilot/releases/9.9.9"
    ver = home / rel
    (ver / "ops" / "guards").mkdir(parents=True, exist_ok=True)
    (home / "plugins/codex-autopilot").mkdir(parents=True, exist_ok=True)
    (home / "plugins/codex-autopilot/install-state.json").write_text(
        json.dumps({"version": "9.9.9", "source": f"./{rel}"}), encoding="utf-8")

    for name in ("autopilot-watchdog.sh", "snapshot.sh", "hooks-guard.sh",
                 "serve-3000-guard.sh", "log-rotate.sh", "reap-codesign-clone.py",
                 "sync-live-3000.sh"):
        (ver / "ops" / "guards" / name).write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    (ver / "ops" / "disk_reap.py").write_text("# reap\n", encoding="utf-8")
    (ver / "ops" / "central_daemon.py").write_text("# daemon\n", encoding="utf-8")
    if missing_version_file:
        (ver / "ops" / "guards" / "snapshot.sh").unlink()

    pdir = home / "Library/LaunchAgents"
    # 绿：版本目录
    if versioned_ok:
        write_plist(pdir / "com.codex-autopilot.daemon.plist", "com.codex-autopilot.daemon",
                    ["/usr/bin/python3", str(ver / "ops/central_daemon.py")])
    else:
        write_plist(pdir / "com.codex-autopilot.daemon.plist", "com.codex-autopilot.daemon",
                    ["/usr/bin/python3", str(home / "old/central_daemon.py")])
    # 黄：登记过的薄转发
    fw = home / ".codex-autopilot/ops/disk_reap.py"
    fw.parent.mkdir(parents=True, exist_ok=True)
    body = FORWARDER_BODY
    if forwarder_dirty:
        body += "\n# import shutil\nshutil_rmtree = 'shutil.rmtree'\n"
        body = body.replace("import runpy, sys", "import runpy, sys, shutil")
    fw.write_text(body, encoding="utf-8")
    write_plist(pdir / "com.danta.codex-disk-reap.plist", "com.danta.codex-disk-reap",
                ["/usr/bin/python3", str(fw)])
    # 红：手工维护的化石副本
    fossil = home / ".codex-guard/snapshot.sh"
    fossil.parent.mkdir(parents=True, exist_ok=True)
    fossil.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    guard_target = (ver / "ops/guards/snapshot.sh") if fossil_versioned else fossil
    write_plist(pdir / "com.danta.codex-guard.plist", "com.danta.codex-guard",
                ["/bin/bash", str(guard_target)])
    # 不归我们：别人的任务不应参与判定
    write_plist(pdir / "com.google.keystone.agent.plist", "com.google.keystone.agent",
                ["/Library/Google/GoogleSoftwareUpdate/GoogleSoftwareUpdate.bundle/x"])

    reg = {
        "entries": [
            {"label": "com.codex-autopilot.daemon", "script": "ops/central_daemon.py",
             "siblings": [], "interval_s": 0, "class": "managed"},
            {"label": "com.danta.codex-disk-reap", "script": "ops/disk_reap.py",
             "siblings": [], "interval_s": 3600, "class": "repoint"},
            {"label": "com.danta.codex-guard", "script": "ops/guards/snapshot.sh",
             "siblings": [], "interval_s": 1800, "class": "repoint"},
        ],
        "owned_prefixes": ["~/plugins/codex-autopilot/", "~/.codex-autopilot/",
                           "~/.codex-guard/", "~/.kg-guard/", "~/.codex/hooks/",
                           "~/.codex-proxy/"],
        "registered_forwarders": [{"path": "~/.codex-autopilot/ops/disk_reap.py"}],
    }
    reg_path = home / "registry.json"
    reg_path.write_text(json.dumps(reg), encoding="utf-8")
    return reg_path


def main():
    # 1) 全绿现场：只有化石那条该红 —— 用它证明门禁不是「永远红」也不是「永远绿」。
    with tempfile.TemporaryDirectory(prefix="launchd-fresh-") as tmp:
        home = Path(tmp)
        reg = build(home)
        r = run_gate(home, reg)
        check("化石路径被点名", "com.danta.codex-guard" in r.stdout and r.returncode == 1,
              f"rc={r.returncode}")
        check("薄转发被认成黄档而不是红档",
              "com.danta.codex-disk-reap" in r.stdout and
              "[forwarder]" in r.stdout, r.stdout[-400:])
        check("版本目录那条被判绿", "[fresh    ] com.codex-autopilot.daemon" in r.stdout)
        check("别人的任务不进失败项", "keystone" not in r.stdout)

    # 2) 把化石也改指到版本目录 → 必须变绿（证明它能过）
    with tempfile.TemporaryDirectory(prefix="launchd-allgreen-") as tmp:
        home = Path(tmp)
        reg = build(home)
        ver = home / "plugins/codex-autopilot/releases/9.9.9"
        write_plist(home / "Library/LaunchAgents/com.danta.codex-guard.plist",
                    "com.danta.codex-guard",
                    ["/bin/bash", str(ver / "ops/guards/snapshot.sh")])
        r = run_gate(home, reg)
        check("全部改指后门禁通过", r.returncode == 0, r.stdout[-400:])

    # 3) 薄转发里被塞进实现 → 必须红。
    #    这一条必须造「其它全绿」的现场：否则它会因为隔壁那条化石红了而假通过，
    #    等到哪天化石被修好，这条断言就静默变成空转。（第一次写时正是这个毛病，
    #    反向验证拆掉判据后它照样 PASS，才发现的。）
    with tempfile.TemporaryDirectory(prefix="launchd-dirtyfw-") as tmp:
        home = Path(tmp)
        reg = build(home, forwarder_dirty=True, fossil_versioned=True)
        r = run_gate(home, reg)
        isolated = (r.returncode == 1
                    and "com.danta.codex-disk-reap" in r.stdout
                    and "com.danta.codex-guard" not in r.stdout.split("失败项")[-1])
        check("薄转发自带实现时单独判红（其它全绿）", isolated, r.stdout[-500:])
        check("失败原因点明「不是薄转发」", "薄转发" in r.stdout.split("失败项")[-1],
              r.stdout[-300:])

    # 4) 当前安装版本缺文件 → 必须红（这是「发版漏了守卫脚本」的现场）
    with tempfile.TemporaryDirectory(prefix="launchd-missing-") as tmp:
        home = Path(tmp)
        reg = build(home, missing_version_file=True)
        r = run_gate(home, reg)
        check("安装版本缺文件时判红", r.returncode == 1 and
              "缺文件" in r.stdout, r.stdout[-400:])

    # 5) registry 自身必须是有效的、每个 script 都真实存在于源码里
    real_reg = json.loads((OPS / "guards" / "registry.json").read_text(encoding="utf-8"))
    for entry in real_reg["entries"]:
        script = OPS.parent / entry["script"]
        check(f"registry 里的 {entry['script']} 真实存在于本仓", script.exists(), str(script))
        for sib in entry.get("siblings") or []:
            check(f"registry 里的 {sib} 真实存在于本仓", (OPS.parent / sib).exists())

    # 6) install.sh 必须真的把 guards 目录打进版本、并调用统一改指
    install_sh = (OPS.parent / "install.sh").read_text(encoding="utf-8")
    check("install.sh 会打包 ops/guards", 'cp -R "$REPO/ops/guards" "$STAGE/ops/"' in install_sh)
    check("install.sh 会统一改指定时任务",
          'repoint_launchd.py" --install' in install_sh)

    if failures:
        print("\n失败项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\nlaunchd entry freshness unit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
