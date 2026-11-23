#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：绑定失效时面板给出的恢复动作必须按故障类型分流。

2026-11-23 现场：Codex 升级后旧 MCP 进程跑在已被删除的 `.136` 目录里，
`need_action` 却统一写「重跑 install.sh」。用户照做也不会替换已经在跑的进程。
本测试锁住：陈旧进程必须提示重启/重新打开 Codex；真正的绑定丢失才提示重装。
"""

import importlib.util

from pathlib import Path

OPS = Path(__file__).resolve().parent
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


def load_binding_module():
    spec = importlib.util.spec_from_file_location(
        "plugin_binding_under_test", OPS / "plugin_binding.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    binding = load_binding_module()

    print("1) 陈旧 MCP 进程：必须提示重启宿主，不能先让人重装")
    stale = binding.binding_recovery_action({
        "status": "broken",
        "stale": "正在运行的插件代码目录已被删除",
        "versions": {"install_state": "0.6.0-preview.139",
                     "marketplace": "0.6.0-preview.139"},
    })
    check("返回 stale_running_code 类型", stale["kind"] == "stale_running_code", stale)
    check("主文案不再无辜称状态根不可用",
          "状态根不是一个可用的项目账本" not in stale["doing"], stale)
    check("主文案点明旧 MCP 进程",
          "旧 MCP 进程" in stale["doing"], stale)
    check("近期说明点明当前会话仍由旧进程服务",
          "当前会话仍由旧进程服务" in stale["recent"], stale)
    check("明确要求重新打开 Codex", "重新打开 Codex" in stale["need_action"], stale)
    check("带上当前安装版本", "0.6.0-preview.139" in stale["need_action"], stale)
    check("主指令不再先让人重跑 install.sh",
          "重跑 ./install.sh" not in stale["need_action"], stale)
    check("详情说明重装不会替换已启动进程",
          "重跑 install.sh 不会替换已经启动的进程" in stale["need_action_detail"], stale)
    check("详情给出重启后仍坏再重装的次序",
          "若仍失效再重跑" in stale["need_action_detail"], stale)

    print("2) 安装版本缺失时从 marketplace 版本给指引")
    stale_from_market = binding.binding_recovery_action({
        "status": "broken",
        "stale": "正在运行的插件代码目录已被删除",
        "versions": {"marketplace": "0.6.0-preview.138"},
    })
    check("使用 marketplace 版本", "0.6.0-preview.138" in stale_from_market["need_action"],
          stale_from_market)

    print("3) 真绑定丢失：仍提示重跑 install.sh")
    invalid = binding.binding_recovery_action({
        "status": "broken", "stale": None,
        "versions": {"install_state": "0.6.0-preview.139"},
    })
    check("返回 binding_invalid 类型", invalid["kind"] == "binding_invalid", invalid)
    check("保留原状态根说明", "状态根不是一个可用的项目账本" in invalid["doing"], invalid)
    check("保留重装指引", "重跑 ./install.sh" in invalid["need_action"], invalid)

    print("4) 老调用方没带 stale 字段时按绑定丢失处理")
    legacy = binding.binding_recovery_action({"status": "broken", "versions": {}})
    check("兼容旧 health 结构", legacy["kind"] == "binding_invalid", legacy)

    print()
    if failures:
        print(f"FAILED {len(failures)} 项：")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("binding recovery action ok: 陈旧进程与绑定失效分别给出正确恢复动作。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
