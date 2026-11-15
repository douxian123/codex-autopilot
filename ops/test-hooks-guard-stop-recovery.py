#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression: hooks-guard 必须能发现「Stop 熔断被摘掉」的 hooks.json。

为什么这条单测本身必要
----------------------
看门狗最容易的腐化方式是「永远绿」。本脚本守护的正是已经发生过 5 次的病：
某次会话把 hooks.json 写成丢了 Stop 的版本，于是所有对话裸奔空转。

2026-11-15 实测到的结构性盲区：旧判定只要求「hooks 非空」，但它会**先**跑
install-resource-hooks.py；那个 installer 只要 hooks 是 dict 就往里追加
resource 条目 → hooks 立刻变非空 → 旧判定直接 PASS → Stop 永远不会被恢复。

所以这里用假 HOME + 假插件根，逐个断言：
  1. 完整 hooks（Stop 挂在 loop-guard.py stop）  → 不恢复
  2. 只有 PostToolUse、丢了 Stop                → 必须恢复
  3. hooks 为空                                  → 必须恢复
  4. Stop 存在但指向别的脚本                      → 必须恢复
  5. 权威备份缺失 + 丢了 Stop                    → fail-open，绝不猜

判定「是否恢复」看的是守卫留下的备份文件 hooks.json.bak-<TS>-guard-restore，
而不是 hooks.json 的内容 —— 因为 installer 每次都会追加 resource 条目，
内容本来就一定会变，拿内容当判据会把「误恢复」和「正常追加」混为一谈。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
GUARDS = OPS / "guards"
GUARD = GUARDS / "hooks-guard.sh"
CANON = Path.home() / ".codex" / "hooks.json.bak-20261009-impl"

failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


STOP_OK = {
    "Stop": [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "/usr/bin/env python3 loop-guard.py stop",
                    "timeout": 3,
                }
            ]
        }
    ]
}


def scenario(name, hooks, *, canon=True, canon_hooks=None, canon_raw=None,
             expect_restore=True, expect_log=None, forbid_log=None):
    """在假 HOME + 假插件根里跑一次守卫，断言它是否做了恢复。"""
    with tempfile.TemporaryDirectory(prefix="hooks-guard-test-") as tmp:
        root = Path(tmp)
        home = root / "home"
        plugin = root / "plugin"
        # 守卫用 BASH_SOURCE 定位 install-resource-hooks.py，
        # installer 又用 parents[2] 定位 resource-admission.py / resource-hooks.json，
        # 所以这里必须摊出一个真的插件根，而不是只拷一个脚本。
        gdir = plugin / "ops" / "guards"
        gdir.mkdir(parents=True)
        for src in ("hooks-guard.sh", "install-resource-hooks.py",
                    "resource-admission.py", "resource-hooks.json"):
            shutil.copy2(GUARDS / src, gdir / src)
        (plugin / "ops" / "guards" / "hooks-guard.sh").chmod(0o755)

        hooks_dir = home / ".codex" / "hooks"
        hooks_dir.mkdir(parents=True)
        hooks_json = home / ".codex" / "hooks.json"
        hooks_json.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
        if canon:
            (home / ".codex" / "hooks.json.bak-20261009-impl").write_text(
                canon_raw if canon_raw is not None
                else json.dumps({"hooks": STOP_OK if canon_hooks is None else canon_hooks}),
                encoding="utf-8")

        env = {**os.environ, "HOME": str(home)}
        proc = subprocess.run(["/bin/bash", str(gdir / "hooks-guard.sh")],
                              capture_output=True, text=True, env=env)
        restored = list((home / ".codex").glob("hooks.json.bak-*-guard-restore"))
        log_path = home / ".codex" / "hooks" / "hooks-guard.log"
        log_text = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""

        check(f"{name}: 守卫正常退出", proc.returncode == 0,
              f"rc={proc.returncode} stderr={proc.stderr[:200]}")
        if expect_restore:
            check(f"{name}: 判定为需要恢复", bool(restored))
        else:
            check(f"{name}: 未误恢复", not restored)
        if expect_log is not None:
            check(f"{name}: 日志含 {expect_log!r}", expect_log in log_text)
        if forbid_log is not None:
            check(f"{name}: 日志不含 {forbid_log!r}", forbid_log not in log_text)
        if expect_restore and restored:
            after = json.loads(hooks_json.read_text(encoding="utf-8"))
            stop = json.dumps(after.get("hooks", {}).get("Stop", []))
            check(f"{name}: 恢复后 Stop 挂在 loop-guard.py", "loop-guard.py" in stop)


def loop_guard_scenarios():
    """Stop 执行体 loop-guard.py 自己的存活：
    hooks.json 完好但执行体被删/截断，看门狗必须把它补回来，且绝不覆盖好文件。

    恢复源三层(2026-11-15 18:4x 补齐第三层):现场 → last-good → 版本目录(随发版)。
    第三层是为了堵「现场和 last-good 同时坏掉就再没有恢复源」这条复发通道。
    """
    def build(root, *, version_source=True):
        """摊一个假 HOME + 假插件根;version_source 控制版本目录里有没有待用源。"""
        home = root / "home"
        plugin = root / "plugin"
        gdir = plugin / "ops" / "guards"
        gdir.mkdir(parents=True)
        for src in ("hooks-guard.sh", "install-resource-hooks.py",
                    "resource-admission.py", "resource-hooks.json"):
            shutil.copy2(GUARDS / src, gdir / src)
        if version_source:
            shutil.copy2(GUARDS / "loop-guard.py", gdir / "loop-guard.py")
        (gdir / "hooks-guard.sh").chmod(0o755)

        hooks_dir = home / ".codex" / "hooks"
        hooks_dir.mkdir(parents=True)
        (home / ".codex" / "hooks.json").write_text(
            json.dumps({"hooks": STOP_OK}), encoding="utf-8")
        (home / ".codex" / "hooks.json.bak-20261009-impl").write_text(
            json.dumps({"hooks": STOP_OK}), encoding="utf-8")
        return home, gdir

    def run(gdir, home):
        return subprocess.run(["/bin/bash", str(gdir / "hooks-guard.sh")],
                              capture_output=True, text=True,
                              env={**os.environ, "HOME": str(home)})

    # ---- 第一组:现场完好 → 留 last-good,不误恢复 ----
    with tempfile.TemporaryDirectory(prefix="loop-guard-good-") as tmp:
        home, gdir = build(Path(tmp))
        target = home / ".codex" / "hooks" / "loop-guard.py"
        lastgood = home / ".codex" / "hooks" / "loop-guard.py.last-good"
        target.write_text("#! /usr/bin/env python3\n# stop marker\n", encoding="utf-8")
        run(gdir, home)
        check("执行体: 正常态生成 last-good", lastgood.is_file())
        check("执行体: 正常态不误恢复", not list((home / ".codex").glob(
            "hooks.json.bak-*-guard-restore")))

        # 现场被移走 → 从 last-good 补回,内容一致
        good = target.read_text(encoding="utf-8")
        target.unlink()
        run(gdir, home)
        check("执行体: 缺失后从 last-good 补回", target.is_file())
        check("执行体: 补回内容与 last-good 一致",
              target.is_file() and target.read_text(encoding="utf-8") == good)

        # 现场被截断成空 → 同样补回
        target.write_text("", encoding="utf-8")
        run(gdir, home)
        check("执行体: 截断成空后补回", target.is_file() and target.stat().st_size > 0)

    # ---- 第二组:现场 + last-good 都没了,但版本目录有源 → 第三层必须顶上 ----
    with tempfile.TemporaryDirectory(prefix="loop-guard-src-") as tmp:
        home, gdir = build(Path(tmp), version_source=True)
        target = home / ".codex" / "hooks" / "loop-guard.py"
        # 刻意不放现场,也不放 last-good:只有版本目录一份。
        proc = run(gdir, home)
        log_text = (home / ".codex" / "hooks" / "hooks-guard.log").read_text(
            encoding="utf-8") if (home / ".codex" / "hooks" / "hooks-guard.log").is_file() else ""
        check("执行体: 无 last-good 时从版本目录补回", target.is_file())
        check("执行体: 版本目录补回后守卫正常退出", proc.returncode == 0,
              f"rc={proc.returncode}")
        check("执行体: 日志记下了补回来源", "补回 loop-guard.py 执行体" in log_text)
        if target.is_file():
            src = (GUARDS / "loop-guard.py").read_text(encoding="utf-8")
            check("执行体: 版本目录补回内容与源一致",
                  target.read_text(encoding="utf-8") == src)

    # ---- 第三组:三个位置全空 → fail-open,绝不凭空造内容 ----
    with tempfile.TemporaryDirectory(prefix="loop-guard-none-") as tmp:
        home, gdir = build(Path(tmp), version_source=False)
        proc = run(gdir, home)
        created = home / ".codex" / "hooks" / "loop-guard.py"
        check("执行体: 无任何恢复源时不凭空造文件", not created.exists())
        check("执行体: 无任何恢复源时守卫仍正常退出", proc.returncode == 0,
              f"rc={proc.returncode}")
        log_text = (home / ".codex" / "hooks" / "hooks-guard.log").read_text(
            encoding="utf-8") if (home / ".codex" / "hooks" / "hooks-guard.log").is_file() else ""
        check("执行体: 无源时明确记日志", "无可用恢复源" in log_text)


def main():
    # 0) 守卫源码本身必须带 Stop 判据。否则下面几条可能因为整段逻辑被删而"通过"。
    body = GUARD.read_text(encoding="utf-8")
    check("守卫源码含 Stop 判据", 'h.get("Stop")' in body)
    check("守卫源码认 loop-guard.py + stop",
          '"loop-guard.py" in cmd' in body and '"stop" in cmd' in body)
    check("守卫源码守执行体", "last-good" in body)
    check("守卫源码验 CANON 自身含 Stop",
          "权威备份本身不含 Stop 熔断" in body)

    scenario("完整 hooks", {**STOP_OK,
                          "PostToolUse": [{"matcher": ".*", "hooks": []}]},
             expect_restore=False)
    scenario("丢了 Stop", {"PostToolUse": [{"matcher": ".*", "hooks": []}]})
    scenario("hooks 为空", {})
    scenario("Stop 指向别的脚本",
             {"Stop": [{"hooks": [{"type": "command", "command": "python3 other.py stop"}]}]})
    scenario("权威备份缺失", {"PostToolUse": [{"matcher": ".*", "hooks": []}]},
             canon=False, expect_restore=False)
    # 2026-11-15 补:权威备份自己坏了 -> 拒绝照坏件恢复,且绝不误记「已恢复」。
    scenario("权威备份丢了 Stop",
             {"PostToolUse": [{"matcher": ".*", "hooks": []}]},
             canon_hooks={"PostToolUse": [{"matcher": ".*", "hooks": []}]},
             expect_restore=False,
             expect_log="权威备份本身不含 Stop 熔断",
             forbid_log="已恢复 hooks.json 挂载")
    scenario("权威备份不是合法 JSON",
             {"PostToolUse": [{"matcher": ".*", "hooks": []}]},
             canon_raw="{ this is not json",
             expect_restore=False,
             expect_log="权威备份本身不含 Stop 熔断",
             forbid_log="已恢复 hooks.json 挂载")

    loop_guard_scenarios()

    if failures:
        print("\n失败项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\nhooks-guard stop recovery unit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
