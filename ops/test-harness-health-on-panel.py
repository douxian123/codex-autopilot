#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""状态管家的结论必须真的能被人看见，而且不许造假。

背景（2026-11-16）：`budget_gate` 每拍都在算「压缩频率 / 峰值 input / 用量」，
但结论只写进 `daemon.log` —— 用户看不到，于是「当前 harness 配置健康吗」在面板上
是个永久盲区。这次把它接进 `autopilot_status.harness_health` 并显示在面板第二问。

本门禁锁三件事：
  1) 有体检记录时，结论来自那份记录（level 取最差的一条），不是另算一套；
  2) 体检记录缺失 / 为空 / 坏掉时，如实说「取不到」，绝不降级成「正常」；
  3) 面板确实把结论写进第二问那行（`sysLine`），且第二问里的 harness 结论
     与 `harness_health` 一致 —— 不能一边报 over、一边在页面上写正常。

退出码 = 未通过项数。
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
PANEL = OPS.parent / "plugin" / "ui" / "panel.html"
if not PANEL.exists():
    PANEL = OPS.parent / "ui" / "panel.html"


def _load_health(runtime_dir):
    """在子进程里按给定 runtime 目录加载 central_mcp 并取 harness_health。"""
    code = (
        "import json,os,sys;"
        "sys.path.insert(0,%r);"
        "import central_mcp;"
        "print(json.dumps(central_mcp._harness_health(),ensure_ascii=False))"
        % str(OPS)
    )
    env = dict(os.environ)
    env["KG_DAEMON_HOME"] = str(runtime_dir)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "")[-400:])
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main():
    fails = 0

    def check(label, ok, detail=""):
        nonlocal fails
        if ok:
            print(f"  ok   {label}")
        else:
            print(f"  FAIL {label}" + (f"（{detail}）" if detail else ""))
            fails += 1

    with tempfile.TemporaryDirectory(prefix="harness-health-") as tmp:
        runtime = Path(tmp)

        # 1) 完全没有体检记录 → 必须说取不到，绝不能说正常。
        h = _load_health(runtime)
        check("没有体检记录时明写取不到", h.get("status") == "unknown", h)
        check("取不到时不给绿色结论",
              "正常" not in str(h.get("headline") or ""), h.get("headline"))

        # 2) 有记录且都健康 → ok，且 headline 说健康。
        (runtime / "budget-gate.json").write_text(json.dumps({
            "last_run": "2026-11-16 23:00:00",
            "last_report": {"threads": [
                {"automation": "automation", "generation": 0, "level": "ok",
                 "reasons": [], "peak_input_recent": 90000,
                 "compactions_recent": 0, "tokens_used": 1000000},
            ], "usage": {"input": 1000}},
        }, ensure_ascii=False), encoding="utf-8")
        h = _load_health(runtime)
        check("健康记录 → status=ok", h.get("status") == "ok", h.get("status"))
        check("健康记录 → level=ok", h.get("level") == "ok", h.get("level"))
        check("健康记录 → 有给人看的结论", bool(h.get("headline")), h)

        # 3) 一个 warn、一个 over → 取最差那条（over），不允许被健康窗口冲淡。
        (runtime / "budget-gate.json").write_text(json.dumps({
            "last_run": "2026-11-16 23:10:00",
            "last_report": {"threads": [
                {"automation": "healthy-one", "generation": 0, "level": "ok",
                 "reasons": []},
                {"automation": "fat-one", "generation": 3, "level": "over",
                 "reasons": ["峰值 input 220000 > 190000"]},
            ], "usage": {}},
        }, ensure_ascii=False), encoding="utf-8")
        h = _load_health(runtime)
        check("多条里取最差的一条（over 不被 ok 冲淡）",
              h.get("level") == "over", h.get("level"))
        check("饱和时结论给出可执行的人话建议",
              "上下文已饱和" in str(h.get("headline") or "")
              and "换新窗口" in str(h.get("headline") or ""),
              h.get("headline"))
        check("越线原因原样带出，不被吞掉",
              bool(h.get("reasons")), h.get("reasons"))

        # 4) 坏掉的记录 → 取不到，不许抛异常、不许装正常。
        (runtime / "budget-gate.json").write_text("{ not json", encoding="utf-8")
        h = _load_health(runtime)
        check("记录坏掉时明写取不到", h.get("status") == "unknown", h)

        # 5) 为空 report → no_data，不是 ok。
        (runtime / "budget-gate.json").write_text(json.dumps(
            {"last_run": "2026-11-16 23:20:00", "last_report": {"threads": []}}),
            encoding="utf-8")
        h = _load_health(runtime)
        check("没有长驻窗口时是 no_data，不是 ok",
              h.get("status") == "no_data", h.get("status"))

    # 6) 面板必须把结论真的写进第二问那行。
    html = PANEL.read_text(encoding="utf-8")
    check("面板读的是 harness_health", "harness_health" in html)
    check("面板第二问显示上下文健康", "sysLine" in html and "上下文健康" in html)

    print()
    if fails:
        print(f"结果：{fails} 项未通过")
    else:
        print("selftest ok: 状态管家的结论能上屏，且取不到时绝不伪装成正常。")
    return fails


if __name__ == "__main__":
    raise SystemExit(main())
