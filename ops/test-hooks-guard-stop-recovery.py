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


def scenario(name, hooks, *, canon=True, expect_restore=True):
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
                json.dumps({"hooks": STOP_OK}), encoding="utf-8")

        env = {**os.environ, "HOME": str(home)}
        proc = subprocess.run(["/bin/bash", str(gdir / "hooks-guard.sh")],
                              capture_output=True, text=True, env=env)
        restored = list((home / ".codex").glob("hooks.json.bak-*-guard-restore"))

        check(f"{name}: 守卫正常退出", proc.returncode == 0,
              f"rc={proc.returncode} stderr={proc.stderr[:200]}")
        if expect_restore:
            check(f"{name}: 判定为需要恢复", bool(restored))
        else:
            check(f"{name}: 未误恢复", not restored)
        if expect_restore and restored:
            after = json.loads(hooks_json.read_text(encoding="utf-8"))
            stop = json.dumps(after.get("hooks", {}).get("Stop", []))
            check(f"{name}: 恢复后 Stop 挂在 loop-guard.py", "loop-guard.py" in stop)


def main():
    # 0) 守卫源码本身必须带 Stop 判据。否则下面几条可能因为整段逻辑被删而"通过"。
    body = GUARD.read_text(encoding="utf-8")
    check("守卫源码含 Stop 判据", 'h.get("Stop")' in body)
    check("守卫源码认 loop-guard.py + stop",
          '"loop-guard.py" in cmd' in body and '"stop" in cmd' in body)

    scenario("完整 hooks", {**STOP_OK,
                          "PostToolUse": [{"matcher": ".*", "hooks": []}]},
             expect_restore=False)
    scenario("丢了 Stop", {"PostToolUse": [{"matcher": ".*", "hooks": []}]})
    scenario("hooks 为空", {})
    scenario("Stop 指向别的脚本",
             {"Stop": [{"hooks": [{"type": "command", "command": "python3 other.py stop"}]}]})
    scenario("权威备份缺失", {"PostToolUse": [{"matcher": ".*", "hooks": []}]},
             canon=False, expect_restore=False)

    if failures:
        print("\n失败项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\nhooks-guard stop recovery unit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
