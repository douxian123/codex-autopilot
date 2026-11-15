#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression: hooks-guard 必须保住 hooks.json 的「非空合法」底线。

2026-11-16 政策变更（本次重写的原因）
------------------------------------
Stop 事件已**主动摘除**：它的 decision:block 被 Codex 客户端落成 role=user 的
HookPrompt（桌面端渲染成「钩子反馈」卡片），把对话刷爆；`suppressOutput` 只压
stdout，挡不住这条落盘。且在 Plan Mode 会话里实测到该注入会让模型回「我不能改」
直接停住，等于制造「跑两句就停」。

于是看门狗的职责从「Stop 必须在」收窄成「hooks.json 存在 + 合法 JSON + hooks
非空」。这样既修掉满屏钩子，又保留「被清成 {"hooks":{}} 就自动补回」这层保护
（历史上有过 5 次这种事故）。

所以这里用假 HOME + 假插件根，逐个断言：
  1. 完整 hooks（PreToolUse 等，无 Stop）         → 不恢复
  2. 只有 PostToolUse                             → 不恢复
  3. hooks 为空                                   → 必须恢复
  4. Stop 残留但不属于本脚本（非空）              → 不恢复
  5. 权威备份缺失 + hooks 为空                    → fail-open，绝不猜
  6. 权威备份不是合法 JSON                        → 拒绝照坏件恢复
  7. 权威备份合法且非空（无 Stop）+ hooks 为空    → 必须恢复

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

# 新政下的「正常挂载」样本：loop-guard 三条挂载齐、且**不含** Stop。
# 注意必须写成真实的 loop-guard 命令 —— 判据要求 command 里有 loop-guard.py,
# 用空 hooks 数组当样本会被（正确地）判成需要恢复。
def _lg(alias):
    return [{"hooks": [{"type": "command",
                        "command": f"/usr/bin/env python3 loop-guard.py {alias}",
                        "timeout": 3}]}]


HOOKS_OK = {
    "PreToolUse": _lg("pre-tool-use"),
    "PostToolUse": _lg("post-tool-use"),
    "UserPromptSubmit": _lg("user-prompt-submit"),
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
            # 默认 CANON 用新政下的正常挂载（三条 loop-guard、无 Stop）。
            # 用旧的「只有 Stop」当默认会让 CANON 校验先失败，
            # 把「该恢复」的场景误判成「拒绝恢复」，掩盖真实判据。
            (home / ".codex" / "hooks.json.bak-20261009-impl").write_text(
                canon_raw if canon_raw is not None
                else json.dumps({"hooks": HOOKS_OK if canon_hooks is None else canon_hooks}),
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
            restored_hooks = after.get("hooks", {})
            check(f"{name}: 恢复后 hooks 非空", bool(restored_hooks))
            check(f"{name}: 恢复后也未把 Stop 装回来",
                  "Stop" not in restored_hooks)


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
            json.dumps({"hooks": HOOKS_OK}), encoding="utf-8")
        (home / ".codex" / "hooks.json.bak-20261009-impl").write_text(
            json.dumps({"hooks": HOOKS_OK}), encoding="utf-8")
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
    # 0) 守卫源码本身必须证明「Stop 已不再被要求」。否则下面几条可能因为整段
    #    逻辑被删而"通过"，就无法区分「按新政摘除」和「误删保护」。
    body = GUARD.read_text(encoding="utf-8")
    check("守卫源码不再要求 Stop 判据", 'h.get("Stop")' not in body)
    check("守卫源码保留 hooks 非空判据", "isinstance(h, dict) and h" in body)
    check("守卫源码守执行体", "last-good" in body)
    check("守卫源码改为要求三条 loop-guard 挂载",
          'for event in ("PreToolUse", "PostToolUse", "UserPromptSubmit")' in body)
    check("守卫源码仍拒收坏 CANON",
          "权威备份缺 loop-guard 挂载或不是合法 JSON" in body)

    # 1) 正常态:三条 loop-guard 挂载齐、无 Stop → 不恢复
    scenario("完整 hooks（无 Stop）", HOOKS_OK, expect_restore=False)
    # 2) 缺 PreToolUse 一条 → 必须恢复（Stop 摘除后覆盖面反而更广）
    scenario("缺 PreToolUse 一条",
             {"PostToolUse": HOOKS_OK["PostToolUse"],
              "UserPromptSubmit": HOOKS_OK["UserPromptSubmit"]},
             expect_restore=True,
             expect_log="已恢复 hooks.json 挂载")
    # 3) 被清成空 → 必须恢复
    scenario("hooks 为空", {}, expect_restore=True,
             expect_log="已恢复 hooks.json 挂载")
    # 4) Stop 残留但指向别的脚本（三条挂载齐）→ 不恢复，也不把 Stop 当回事
    scenario("Stop 指向别的脚本（非空）",
             {**HOOKS_OK,
              "Stop": [{"hooks": [{"type": "command", "command": "python3 other.py stop"}]}]},
             expect_restore=False)
    # 5) 权威备份缺失 + hooks 为空 → fail-open，绝不猜
    scenario("权威备份缺失 + hooks 为空", {}, canon=False, expect_restore=False,
             expect_log="权威备份缺失")
    # 6) 权威备份不是合法 JSON → 拒绝照坏件恢复，且绝不误记「已恢复」
    scenario("权威备份不是合法 JSON", {},
             canon_raw="{ this is not json",
             expect_restore=False,
             expect_log="权威备份缺 loop-guard 挂载或不是合法 JSON",
             forbid_log="已恢复 hooks.json 挂载")
    # 7) 权威备份合法且三条挂载齐（无 Stop）+ hooks 为空 → 必须恢复
    scenario("权威备份合法（无 Stop）+ hooks 为空", {},
             canon_hooks=HOOKS_OK,
             expect_restore=True,
             expect_log="已恢复 hooks.json 挂载")

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
