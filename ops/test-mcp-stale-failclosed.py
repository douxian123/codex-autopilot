#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：陈旧 MCP 进程不许再返回任何项目态。

2026-11-24 现场实测：Codex 升级后，当前会话的 MCP 进程仍跑在已被删除的
`.145` 缓存目录里。`autopilot_status` 已经会报 binding_broken，但其它工具仍在
从旧进程残留的 KG_REPO/KG_TRUNK 读旧账本：`resolve_list` 返回 4 条陈旧 resolve，
显式 KG_REPO 的 CLI 却为空。用户看到的是两套互相打架的真相，这就是「能用明白」
的闭环缺口。

本门禁锁住：
  1. stale running_code 下，普通读写工具全部 fail-closed，不碰账本；
  2. autopilot_status / render_autopilot_panel 仍返回诊断结果和恢复动作；
  3. 非 stale 的 ok / broken binding 不被这条 guard 误拦。
"""

import sys
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))

import central_mcp  # noqa: E402


failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label} {detail}")


def asks_restart(text):
    """文案可以写成「重启」或「完全退出并重新打开」，语义都是重载宿主。"""
    text = str(text)
    return "重启 Codex" in text or "重新打开 Codex" in text


def stale_health():
    return {
        "status": "broken",
        "repo": "/tmp/old-stale-state-root",
        "trunk": "/tmp/old-stale-trunk",
        "problems": ["正在运行的插件代码目录已被删除：/tmp/old-mcp-cache/ops"],
        "warnings": [],
        "versions": {"install_state": "0.6.0-preview.140"},
        "stale": "正在运行的插件代码目录已被删除：/tmp/old-mcp-cache/ops",
    }


def run_with_health(health, fn):
    old_health = central_mcp.binding_health
    old_kgctl = central_mcp.kgctl
    old_truthctl = central_mcp.truthctl
    calls = []
    central_mcp.binding_health = lambda: health
    central_mcp.kgctl = lambda *a, **kw: (calls.append(("kgctl", a, kw)) or ("PASSED_THROUGH", 0))
    central_mcp.truthctl = lambda *a, **kw: (calls.append(("truthctl", a, kw)) or ("PASSED_THROUGH", 0))
    try:
        return fn(), calls
    finally:
        central_mcp.binding_health = old_health
        central_mcp.kgctl = old_kgctl
        central_mcp.truthctl = old_truthctl


def main():
    health = stale_health()

    print("1) stale running_code：普通工具一律 fail-closed")
    cases = [
        ("resolve_list", {}),
        ("project_status", {}),
        ("capture_request", {"text": "这是陈旧进程不该落账的需求"}),
        ("sync_control_plane", {}),
        ("project_autonomy_status", {}),
        ("constraints_add", {"statement": "陈旧进程不该写约束"}),
        ("not_a_real_tool", {}),
    ]
    for name, args in cases:
        (out, code), calls = run_with_health(
            health, lambda name=name, args=args: central_mcp.call_tool(name, args))
        blocked = (
            code == 2
            and isinstance(out, dict)
            and out.get("code") == "AUTOPILOT_BINDING_BROKEN"
            and out.get("state") == "binding_broken"
            and out.get("tool") == name
            and asks_restart(out.get("need_action"))
            and not calls
        )
        check(f"{name} 被 stale guard 拦住且没有触碰账本", blocked, (out, code, calls))

    print("2) 诊断出口仍可用，且明确给出恢复动作")
    (status, code), calls = run_with_health(
        health, lambda: central_mcp.call_tool("autopilot_status", {}))
    check("autopilot_status 仍返回诊断", code == 0, (status, code))
    check("状态里明写 binding_broken", isinstance(status, dict)
          and status.get("state") == "binding_broken", status)
    check("恢复动作指向重启或重新打开 Codex",
          asks_restart((status or {}).get("need_action")),
          (status or {}).get("need_action"))
    check("诊断出口没有顺手写账本", not calls, calls)

    (panel, panel_code), panel_calls = run_with_health(
        health, lambda: central_mcp.call_tool("render_autopilot_panel", {}))
    check("原生面板入口仍可打开", panel_code == 0, (panel, panel_code))
    check("面板状态同样报 binding_broken", isinstance(panel, dict)
          and panel.get("state") == "binding_broken", panel)
    check("面板出口没有顺手写账本", not panel_calls, panel_calls)

    print("3) 非 stale 的 ok / broken 不被误拦")
    variants = [
        ("ok", {"status": "ok", "problems": [], "stale": None,
                "versions": {"install_state": "0.6.0-preview.140"}}),
        ("broken", {"status": "broken", "problems": ["缺 KG_REPO"], "stale": None,
                    "versions": {"install_state": "0.6.0-preview.140"}}),
    ]
    for label, variant in variants:
        for name in ("project_status", "resolve_list"):
            (out, code), calls = run_with_health(
                variant, lambda name=name: central_mcp.call_tool(name, {}))
            check(f"{label} binding 下 {name} 不被 stale guard 误拦",
                  code == 0 and out == "PASSED_THROUGH" and len(calls) == 1,
                  (out, code, calls))

    print()
    if failures:
        print(f"FAILED {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
    else:
        print("selftest ok: 陈旧 MCP 进程只留诊断出口，不再返回旧的项目的状态。")
    return len(failures)


if __name__ == "__main__":
    raise SystemExit(main())
