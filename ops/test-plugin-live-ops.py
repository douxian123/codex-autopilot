#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：插件升级之后，**已经在跑的 MCP 进程**不许变成哑巴。

为什么有这条（2026-11-16 实测事故）：
  Codex 把插件快照到 `~/.codex/plugins/cache/personal/codex-autopilot/<版本>/`，
  并以那个版本目录为 cwd 常驻拉起 `./ops/central_mcp.py`。发新版时 Codex 会拿新
  版本目录替换旧的，**却不会重启已经在跑的 MCP 进程**。

  后果：那些进程的 `Path(__file__).resolve().parent` 指向一个已被删除的目录，
  `OPS_DIR / "kgctl.py"` 根本不存在，每次工具调用都只拿到
  `can't open file .../ops/kgctl.py`。实测当时有 13 个这样的进程，cwd 全是已删除的
  `.82`，而 `.86` 早就装好了。

  这是本项目硬规则里最难发现的那类失效：**不抛异常、只是安静地失败**。宿主那边
  只表现为「工具坏了」，用户唯一的感受是「装了跟没装一样」。

这条门禁锁住三件事：
  1. 本目录还能跑时，就用本目录（不许平白无故跳到别的版本）
  2. 本目录已被删除时，`current_ops()` 必须跟随 install-state 找到当前安装版本
  3. 这种情况下 `stale_code_problem()` 必须报错，且 `binding_health()` 判 broken
     —— 绝不能退回「一切正常」的假绿灯
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


def make_plugin_version(base, version):
    """造一个「装好的版本目录」：ops/ 下带着够用的脚本。"""
    root = base / "codex-autopilot" / "releases" / version
    (root / "ops").mkdir(parents=True)
    for f in ("kgctl.py", "central_mcp.py", "truthctl.py", "central_daemon.py"):
        (root / "ops" / f).write_text("# stub\n", encoding="utf-8")
    # plugin_binding 要用真的那一份：探针只加载它，stub 会让结论失真。
    (root / "ops" / "plugin_binding.py").write_text(
        (OPS / "plugin_binding.py").read_text(encoding="utf-8"), encoding="utf-8")
    return root


def probe(ops_dir, home):
    """在子进程里以指定 HOME 探一次，避免 Path.home() 被本进程污染。

    只 import 传进去的那一份 plugin_binding.py，等价于那个进程启动时看到的样子。
    """
    code = (
        "import importlib.util, json, sys\n"
        "spec = importlib.util.spec_from_file_location('b', sys.argv[1])\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "print(json.dumps({'running': str(m.running_ops()),\n"
        "                  'current': str(m.current_ops()),\n"
        "                  'stale': m.stale_code_problem(),\n"
        "                  'health': m.binding_health()}, ensure_ascii=False))\n"
    )
    env = dict(os.environ)
    env["HOME"] = str(home)
    out = subprocess.run([sys.executable, "-c", code, str(ops_dir / "plugin_binding.py")],
                         capture_output=True, text=True, env=env)
    if out.returncode != 0:
        raise AssertionError("探针跑不起来：\n%s\n%s" % (out.stdout, out.stderr))
    return json.loads(out.stdout.strip().splitlines()[-1])


def main():
    base = Path(tempfile.mkdtemp(prefix="kg-live-ops-"))
    try:
        home = base / "home"
        home.mkdir()
        project = base / "state"
        (project / "ops" / "tasks").mkdir(parents=True)
        (project / "ops" / "truth").mkdir(parents=True)
        installed = make_plugin_version(home / "plugins", "0.6.0-preview.86")
        (installed / ".mcp.json").write_text(json.dumps({
            "mcpServers": {"codex-autopilot": {
                "env": {"KG_REPO": str(project),
                        "KG_TRUNK": str(base / "state-trunk")}}}}, ensure_ascii=False),
            encoding="utf-8")

        print("1) 本目录还能跑 → 就用本目录，不跳版本")
        got = probe(installed / "ops", home)
        check("current_ops 落在装好的那一版",
              Path(got["current"]).resolve() == (installed / "ops").resolve(),
              got["current"])
        check("本目录健在时 stale_code_problem 为 None（不许无病呻吟）",
              got["stale"] is None, got["stale"])

        print("2) 本目录已被删除 → 必须跟随 install-state 找到当前版本")
        stale_ops = home / "cache" / "codex-autopilot" / "0.6.0-preview.82" / "ops"
        stale_ops.mkdir(parents=True)
        (stale_ops / "plugin_binding.py").write_text(
            (OPS / "plugin_binding.py").read_text(encoding="utf-8"), encoding="utf-8")
        check("模拟现场：陈旧目录里确实没有 kgctl.py",
              not (stale_ops / "kgctl.py").exists())
        (home / "plugins" / "codex-autopilot").mkdir(parents=True, exist_ok=True)
        (home / "plugins" / "codex-autopilot" / "install-state.json").write_text(
            json.dumps({"version": "0.6.0-preview.86",
                        "source": "./plugins/codex-autopilot/releases/0.6.0-preview.86"}),
            encoding="utf-8")
        got = probe(stale_ops, home)
        check("陈旧进程的 current_ops 跟随 install-state 落到当前版本",
              Path(got["current"]).resolve() == (installed / "ops").resolve(),
              got["current"])
        check("陈旧进程必须报出「正在跑的代码已被删除」",
              bool(got["stale"]) and "已被删除" in got["stale"], got["stale"])
        check("陈旧进程的绑定体检必须是 broken，不许假绿灯",
              got["health"].get("status") == "broken", got["health"].get("status"))
        check("broken 的原因里带上修复指引（重启 Codex）",
              any("重启 Codex" in p for p in got["health"].get("problems") or []),
              got["health"].get("problems"))

        print("3) 当前版本也读不到时，至少指回本进程真实所在位置")
        (home / "plugins" / "codex-autopilot" / "install-state.json").write_text(
            json.dumps({"version": "0.0.0",
                        "source": "./plugins/codex-autopilot/releases/gone"}),
            encoding="utf-8")
        got = probe(stale_ops, home)
        check("无处可去时 current_ops 报自己，不编一条假路径",
              Path(got["current"]).resolve() == stale_ops.resolve(), got["current"])
        check("这种情况下仍是 broken",
              got["health"].get("status") == "broken", got["health"].get("status"))
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print()
    if failures:
        print(f"FAILED {len(failures)} 项：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("selftest ok: 插件升级后不再让在跑的 MCP 变成哑巴，且失效必被报成 broken。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
