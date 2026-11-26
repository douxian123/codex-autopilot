#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""闭环施工包门禁：草案、补齐、确认、执行闸和关闭都要 fail-closed。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

OPS = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("closure_package_under_test",
                                              OPS / "closure_package.py")
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"  ok   {label}")


def valid_package(**overrides):
    package = {
        "status": mod.STATUS_READY,
        "service_result": "用户在面板看到可复核的施工包状态",
        "entrypoint": "打开 Codex 面板高级区 -> PM 简报",
        "activation": "控制层返回施工包投影，面板渲染对应文字",
        "machine_acceptance": [{
            "name": "panel-logic",
            "cmd": "python3 ops/panel_logic_check.py",
            "cwd": ".",
            "pass_condition": "exit 0",
        }],
        "real_usage_acceptance": [{
            "step": "打开真实面板高级区确认文字可见",
            "pass_condition": "文字可见且状态可解释",
            "status": "pending",
        }],
        "human_boundary": {"status": "not_required", "why": "机器与真实入口已覆盖"},
        "rollback": "保留现场，失败时回到 planning_incomplete，不覆盖他人文件",
        "writeback": [{"target": "ops/truth/progress.jsonl", "status": "pending"}],
        "cost_strategy": {
            "mode": "summary_first",
            "cache": "scope + 文件指纹命中缓存",
            "on_over_budget": "block_and_report",
            "note": "不删必留上下文、不跳验收；超预算停开工并报告",
        },
    }
    package.update(overrides)
    return package


def main():
    check("九个字段都是必填",
          list(mod.FIELD_LABELS) == [
              "service_result", "entrypoint", "activation", "machine_acceptance",
              "real_usage_acceptance", "human_boundary", "rollback", "writeback",
              "cost_strategy"],
          list(mod.FIELD_LABELS))

    empty = mod.validate({})
    check("空包停在 planning_incomplete",
          empty["status"] == mod.STATUS_PLANNING_INCOMPLETE
          and len(empty["missing"]) == 9, empty)

    placeholder_machine = valid_package(machine_acceptance=[{
        "name": "todo", "cmd": "", "cwd": ".", "pass_condition": "以后再写"}])
    check("机器验收不能是空命令占位",
          "machine_acceptance" in mod.missing_fields(placeholder_machine),
          mod.validate(placeholder_machine))

    bad_cost = valid_package(cost_strategy={
        "mode": "whatever", "cache": "", "on_over_budget": "ignore", "note": ""})
    check("成本策略不能写空口号",
          "cost_strategy" in mod.missing_fields(bad_cost), mod.validate(bad_cost))

    draft = mod.for_ledger(
        {"id": "L-0001", "title": "测试施工包", "detail": "补齐闭环", "files": ["ops/x.py"]},
        text="测试施工包", kind="gate", lane="gate",
        acceptance_plan={"machine_checks": [], "human_checks": []})
    check("路由草案永远是 planning_incomplete",
          draft["status"] == mod.STATUS_PLANNING_INCOMPLETE
          and "cost_strategy" in draft and draft["missing"], draft)
    check("草案不能过执行闸",
          mod.execution_gate(draft)["status"] == "blocked", mod.execution_gate(draft))

    unfilled_confirm = mod.confirm(draft, note="PM 已看", evidence="docs/PM.md:12")
    check("字段没补齐时 confirm 不能绕过",
          (unfilled_confirm.get("confirmation") or {}).get("status") == "rejected"
          and unfilled_confirm["status"] == mod.STATUS_PLANNING_INCOMPLETE,
          unfilled_confirm)

    filled = mod.fill(draft, {
        "machine_acceptance": [{
            "name": "closure-test", "cmd": "python3 ops/test-closure-package.py",
            "cwd": ".", "pass_condition": "exit 0"}],
        "real_usage_acceptance": [{
            "step": "打开真实面板确认可见", "pass_condition": "可见"}],
        "human_boundary": {"status": "not_required", "why": "可机器判定"},
    })
    check("fill 后仍是规划未完成",
          filled["status"] == mod.STATUS_PLANNING_INCOMPLETE
          and not filled["missing"]
          and "confirmation" not in filled, filled)
    check("完整但未确认仍挡 execute",
          mod.execution_gate(filled)["status"] == "blocked", mod.execution_gate(filled))

    ready = mod.confirm(filled, note="PM 确认范围和证据完整",
                        evidence="docs/PM.md:12 已核对入口和门禁")
    check("confirm 后才 ready",
          ready["status"] == mod.STATUS_READY
          and (ready["confirmation"] or {}).get("status") == "confirmed", ready)
    check("ready 才过执行闸",
          mod.execution_gate(ready)["status"] == "pass", mod.execution_gate(ready))

    bad_close = mod.close(ready, note="跑过命令", evidence="cmd exit 0")
    check("ready 但缺真实入口证据不能关闭",
          (bad_close.get("closure") or {}).get("status") == "needs_human"
          and bad_close["status"] == mod.STATUS_PLANNING_INCOMPLETE, bad_close)

    closed = mod.close(
        ready, note="机器门禁和真实入口都确认",
        evidence="python3 ops/test-closure-package.py cwd=. exit 0",
        usage_evidence="真实面板高级区 PM 简报可见 L-0001，截图 docs/evidence.png")
    check("带真实入口证据才 closed",
          closed["status"] == mod.STATUS_CLOSED
          and (closed["closure"] or {}).get("status") == "closed"
          and mod.is_closed(closed), closed)

    pending_human = valid_package(human_boundary={"status": "pending"})
    blocked_human = mod.close(
        pending_human, note="跑过命令", evidence="cmd exit 0",
        usage_evidence="真实入口看过")
    check("人工边界未确认不能被机器代签",
          (blocked_human.get("closure") or {}).get("status") == "needs_human",
          blocked_human)
    human_closed = mod.close(
        pending_human, note="用户确认视觉效果", evidence="用户确认：看过，可以",
        human_confirmed=True, usage_evidence="截图 docs/visual.png")
    check("只有显式用户确认才翻人工边界",
          mod.is_closed(human_closed), human_closed)

    try:
        mod.confirm(filled, note="", evidence="docs/PM.md:12")
    except ValueError:
        check("confirm 缺 PM 判断会红", True)
    else:
        raise AssertionError("confirm 缺 PM 判断没有被拒绝")

    print("closure-package: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
