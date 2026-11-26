#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""闭环施工包契约。

PRD `docs/PRODUCT_CONTRACT.md` 5.0 要求每个进入施工队列的工程项先形成
闭环施工包。本模块只做纯校验，不碰账本、不启动进程：缺任何一项都必须
返回 ``planning_incomplete``，由调用方 fail-closed。
"""

from __future__ import annotations

import time
from typing import Any

STATUS_READY = "ready"
STATUS_PLANNING_INCOMPLETE = "planning_incomplete"
STATUS_CLOSED = "closed"

FIELD_LABELS = {
    "service_result": "服务结果",
    "entrypoint": "使用入口",
    "activation": "生效条件",
    "machine_acceptance": "机器验收",
    "real_usage_acceptance": "真实使用验收",
    "human_boundary": "人工边界",
    "rollback": "回滚与恢复",
    "writeback": "最终回写",
    "cost_strategy": "成本策略",
}


def _has_content(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return any(_has_content(item) for item in value)
    if isinstance(value, dict):
        return any(_has_content(item) for item in value.values())
    return value is not None and bool(value)


def _meaningful_machine_checks(value: Any) -> bool:
    """机器验收必须含可执行命令，不能拿占位文字冒充门禁。"""
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return False
    for item in value:
        if not isinstance(item, dict):
            continue
        cmd = str(item.get("cmd") or "").strip()
        cwd = str(item.get("cwd") or "").strip()
        condition = str(item.get("pass_condition") or "").strip()
        if cmd and cwd and condition:
            return True
    return False


def _meaningful_steps(value: Any, key: str) -> bool:
    """真实使用/最终回写至少要有一条具体步骤或目标。"""
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return False
    for item in value:
        if isinstance(item, dict) and str(item.get(key) or "").strip():
            return True
        if isinstance(item, str) and item.strip():
            return True
    return False


def _meaningful_cost_strategy(value: Any) -> bool:
    """成本策略必须能机器判定，不能只写「省一点」。"""
    if not isinstance(value, dict):
        return False
    mode = str(value.get("mode") or "").strip()
    cache = str(value.get("cache") or "").strip()
    over = str(value.get("on_over_budget") or "").strip()
    note = str(value.get("note") or "").strip()
    return (mode in {"summary_first", "minimal_scope"}
            and bool(cache)
            and over in {"block_and_report", "stop_and_report"}
            and bool(note))


def missing_fields(package: Any) -> list[str]:
    if not isinstance(package, dict):
        return list(FIELD_LABELS)
    missing = []
    for key in FIELD_LABELS:
        value = package.get(key)
        if not _has_content(value):
            missing.append(key)
            continue
        if key == "machine_acceptance" and not _meaningful_machine_checks(value):
            missing.append(key)
        elif key == "real_usage_acceptance" and not _meaningful_steps(value, "step"):
            missing.append(key)
        elif key == "writeback" and not _meaningful_steps(value, "target"):
            missing.append(key)
        elif key == "cost_strategy" and not _meaningful_cost_strategy(value):
            missing.append(key)
    return missing


def reason_for(missing: list[str]) -> str:
    if not missing:
        return "闭环施工包完整，可以进入施工"
    labels = "、".join(FIELD_LABELS.get(key, key) for key in missing)
    return f"闭环施工包缺少{labels}，停在 planning_incomplete，不能进入 execute"


def validate(package: Any) -> dict:
    missing = missing_fields(package)
    return {
        "status": STATUS_READY if not missing else STATUS_PLANNING_INCOMPLETE,
        "ok": not missing,
        "missing": missing,
        "reason": reason_for(missing),
        "fields": list(FIELD_LABELS),
    }


def gate(package: Any) -> tuple[bool, str]:
    result = validate(package)
    return result["ok"], result["reason"]


def can_execute(package: Any) -> bool:
    return missing_fields(package) == []


def is_closed(package: Any) -> bool:
    """完成口径：施工包完整，且机器、真实入口、人工边界、回写都有收敛证据。"""
    if not isinstance(package, dict) or not can_execute(package):
        return False
    if package.get("status") != STATUS_CLOSED:
        return False
    machine = package.get("machine_acceptance")
    usage = package.get("real_usage_acceptance")
    writeback = package.get("writeback")
    human_boundary = package.get("human_boundary")
    machine_done = isinstance(machine, dict) and machine.get("status") in {"pass", "passed", "verified"}
    if not machine_done and isinstance(machine, list):
        machine_done = all(
            isinstance(item, dict) and item.get("status") in {"pass", "passed", "verified"}
            for item in machine
        )
    usage_done = isinstance(usage, dict) and usage.get("status") in {"pass", "passed", "verified"}
    if not usage_done and isinstance(usage, list):
        usage_done = all(
            isinstance(item, dict) and item.get("status") in {"pass", "passed", "verified"}
            for item in usage
        )
    human_ready = True
    if isinstance(human_boundary, dict):
        human_ready = human_boundary.get("status") not in {"pending", "needs_human", "blocked"}
    elif isinstance(human_boundary, list):
        human_ready = all(
            not (isinstance(item, dict) and item.get("status") in {"pending", "needs_human", "blocked"})
            for item in human_boundary
        )
    writeback_done = isinstance(writeback, dict) and writeback.get("status") in {"done", "verified"}
    if not writeback_done and isinstance(writeback, list):
        writeback_done = all(
            isinstance(item, dict) and item.get("status") in {"done", "verified"}
            for item in writeback
        )
    return machine_done and usage_done and human_ready and writeback_done


def execution_gate(package: Any) -> dict:
    """执行前契约门： planning_incomplete / 非 ready 一律 fail-closed。"""
    result = validate(package)
    if result["ok"] and (not isinstance(package, dict)
                         or package.get("status") in (None, STATUS_READY)):
        return {"status": "pass", "reason": result["reason"], "validation": result}
    if result["ok"]:
        return {
            "status": "blocked",
            "reason": "闭环施工包标记为 %s，不能进入 execute" % package.get("status"),
            "validation": result,
        }
    return {"status": "blocked", "reason": result["reason"], "validation": result}


def for_ledger(rec: Any, *, text: str = "", kind: str = "", lane: str = "",
               files: Any = None, acceptance_plan: Any = None) -> dict:
    """从一条已路由 ledger 推导首版闭环施工包。

    这是规划产物，不是完成证据：服务结果、入口、生效条件、回滚和回写只能由
    路由/施工规划填出可执行描述；填不出就留空并让 `validate` 判
    `planning_incomplete`。绝不把「改代码」或「测试通过」冒充施工包。
    """
    rec = rec if isinstance(rec, dict) else {}
    goal = " ".join(str(rec.get("title") or text or "").split())[:160]
    detail = " ".join(str(rec.get("detail") or text or "").split())[:400]
    file_list = [str(x) for x in (rec.get("files") or files or []) if str(x).strip()]
    file_hint = "、".join(file_list[:8])
    service_result = goal
    entrypoint = (
        "从 Codex 原生输入框用原话触发本条需求，并在面板对应详情/验收入口查看结果" if goal else ""
    )
    activation = "；".join(filter(None, [
        f"相关文件：{file_hint}" if file_hint else "",
        "完成实现并通过本条验收清单",
        "真实入口可见且证据回写 ledger/progress",
    ]))
    machine = acceptance_plan if isinstance(acceptance_plan, dict) else (
        rec.get("acceptance_plan") if isinstance(rec.get("acceptance_plan"), dict) else {}
    )
    machine_checks = machine.get("machine_checks") or []
    real_usage = machine.get("human_checks") or []
    # 每个施工包都必须有真实入口验收，不能因为条目不是 visual/ux 就空着。
    # 视觉/交互项保留编译器给出的细步骤；其余项至少要有「打开真实入口看结果」
    # 这一步，状态明确 pending，由确认人补证据。
    real_usage = [
        dict(item, status="pending") if isinstance(item, dict) else
        {"step": str(item), "status": "pending"}
        for item in real_usage
    ]
    real_usage.append({
        "step": "打开真实 Codex 宿主/面板，按使用入口确认结果可见且与诉求一致",
        "pass_condition": "结果可见，状态可解释，用户知道下一步",
        "status": "pending",
        "why": "命令退出码不能代替真实入口验收",
    })
    human = []
    # 真实使用验收是每条施工包的必备证据，但它不等于「用户必须签收」。
    # 只有视觉/交互/PM 明确需要判断的项才停在人工边界；普通 bug/gate/arch
    # 仍要给真实入口证据，但不能因此永远卡住自动闭环。
    needs_human = bool(machine.get("needs_human")) or str(kind) in {"visual", "ux"}
    if needs_human:
        human.append({
            "status": "pending",
            "steps": real_usage or ["按真实入口验收步骤人工确认"],
            "why": "视觉/交互/产品方向不能由命令退出码代替人眼确认",
        })
    else:
        human.append({
            "status": "not_required",
            "why": "当前条目没有审美、方向、删除或发布类人工判断；机器和真实入口验收已覆盖",
        })
    rollback = (
        "保留当前 worktree 和未提交改动；失败时停在 planning_incomplete 或 blocked，"
        "不覆盖他人工作区，不删除历史证据；按文件占地重开隔离分支或恢复原始状态"
    )
    writeback = [
        {"target": "ops/truth/ledger.jsonl", "status": "pending", "detail": "回写真实状态和结论"},
        {"target": "ops/truth/progress.jsonl", "status": "pending", "detail": "回写命令、cwd、退出码和结果"},
        {"target": "面板详情", "status": "pending", "detail": "用户可在面板看到结果和证据"},
    ]
    package = {
        "status": "pending",
        "service_result": service_result,
        "entrypoint": entrypoint,
        "activation": activation,
        "machine_acceptance": machine_checks,
        "real_usage_acceptance": real_usage,
        "human_boundary": human,
        "rollback": rollback,
        "writeback": writeback,
        "cost_strategy": {
            "mode": "summary_first",
            "cache": "只读缓存按 scope + 文件指纹命中；分支/文件未变不重复读账本或重扫宿主",
            "on_over_budget": "block_and_report",
            "note": "不删必留上下文、不跳验收；只把可选材料摘要化，预算不足停开工并报告缺口",
        },
        "detail": detail,
        "lane": rec.get("lane") or lane,
        "kind": rec.get("kind") or kind,
        "source": "closure-package-planner",
    }
    if not machine_checks:
        package["machine_acceptance"] = [{
            "name": "missing-machine-acceptance",
            "cmd": "",
            "cwd": ".",
            "pass_condition": "施工前必须补真命令和退出码",
            "why": "没有可判定机器门禁时，不能把路由草案当成可施工方案",
        }]
    verdict = validate(package)
    package["status"] = verdict["status"]
    package["reason"] = verdict["reason"]
    package["missing"] = verdict["missing"]
    # 路由草案永远不是可执行施工方案。字段齐只代表可进入 PM 收敛，不代表
    # 入口、门禁、真实使用验收和回写已经被验证；必须显式确认后才能 ready。
    package["status"] = STATUS_PLANNING_INCOMPLETE
    package["reason"] = (
        "闭环施工包草案已生成；PM 必须补齐并确认入口、门禁、真实使用验收、回写和成本策略后才能进入 execute"
    )
    return package


def confirm(package: Any, *, note: str, evidence: str) -> dict:
    """把完整施工包从草案确认成可执行的 ready。

    确认必须带 PM 的说明和可复核证据。缺字段、空命令、空步骤或空回写目标
    都不能靠一句“确认”绕过；否则 planning_incomplete 就只是文案。
    """
    note = str(note or "").strip()
    evidence = str(evidence or "").strip()
    if not note:
        raise ValueError("确认闭环施工包必须写明 PM 判断")
    if not evidence:
        raise ValueError("确认闭环施工包必须带可复核证据")
    verdict = validate(package)
    if not verdict["ok"]:
        result = dict(package) if isinstance(package, dict) else {}
        result["status"] = STATUS_PLANNING_INCOMPLETE
        result["validation"] = verdict
        result["confirmation"] = {"status": "rejected", "note": note,
                                  "reason": verdict["reason"]}
        return result
    result = dict(package)
    result["status"] = STATUS_READY
    result["validation"] = verdict
    result["confirmation"] = {
        "status": "confirmed",
        "note": note,
        "evidence": evidence,
        "confirmed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return result


def fill(package: Any, updates: Any) -> dict:
    """把路由草案补成可执行施工包，但不替 PM 确认。

    PM 的研究结果和工程判断需要落进施工包；这一步只合并字段并重新计算校验结果，
    状态仍停在 `planning_incomplete`。只有 `confirm` 才能把它推进到 `ready`。
    """
    if not isinstance(package, dict):
        raise ValueError("闭环施工包不存在，不能补字段")
    if not isinstance(updates, dict):
        raise ValueError("闭环施工包补丁必须是对象")
    result = dict(package)
    for key in FIELD_LABELS:
        if key in updates:
            result[key] = updates[key]
    verdict = validate(result)
    result["validation"] = verdict
    result["missing"] = verdict["missing"]
    result["reason"] = verdict["reason"]
    result["status"] = STATUS_PLANNING_INCOMPLETE
    result.pop("confirmation", None)
    result.pop("closure", None)
    return result


def close(package: Any, *, note: str, evidence: str,
          human_confirmed: bool = False, usage_evidence: str = "") -> dict:
    """把已确认施工包收敛成 closed。

    这里不执行用户命令，只校验收口证据的形状。命令、cwd、退出码仍以传入的
    evidence 原文保存，调用方必须把它同时写进 ledger/progress。真实使用验收
    默认就是 pending：机器命令过绿不能替代「打开真实入口看过结果」。施工包
    必须先由 PM 显式确认到 ready；close 不再自动确认草案。
    """
    note = str(note or "").strip()
    evidence = str(evidence or "").strip()
    usage_evidence = str(usage_evidence or "").strip()
    if not note:
        raise ValueError("关闭闭环施工包必须写明结果判断")
    if not evidence:
        raise ValueError("关闭闭环施工包必须带真实命令/cwd/退出码证据")
    human_evidence = human_confirmed and "用户确认" in evidence
    if (not human_evidence
            and "exit 0" not in evidence.lower()
            and "commit " not in evidence.lower()):
        raise ValueError("闭环施工包证据必须含 exit 0 或 commit sha，不能只写结论")

    result = dict(package) if isinstance(package, dict) else {}
    status = str(result.get("status") or "")
    if status != STATUS_READY:
        verdict = validate(result)
        result["status"] = STATUS_PLANNING_INCOMPLETE
        result["validation"] = verdict
        result["closure"] = {
            "status": "needs_planning",
            "reason": "施工包未由 PM 确认到 ready，不能关闭；先补齐并确认闭环施工包",
        }
        return result

    human = result.get("human_boundary")
    usage = result.get("real_usage_acceptance")
    pending_human = False
    pending_usage = False
    if isinstance(human, dict):
        pending_human = human.get("status") in {"pending", "needs_human", "blocked"}
    elif isinstance(human, list):
        pending_human = any(
            isinstance(item, dict)
            and item.get("status") in {"pending", "needs_human", "blocked"}
            for item in human
        )
    if isinstance(usage, dict):
        pending_usage = usage.get("status") not in {"pass", "passed", "verified"}
    elif isinstance(usage, list):
        pending_usage = any(
            isinstance(item, dict)
            and item.get("status") not in {"pass", "passed", "verified"}
            for item in usage
        )
    if pending_usage and usage_evidence:
        result["real_usage_acceptance"] = _mark_status(
            usage, "verified", evidence=usage_evidence)
        pending_usage = False
    elif pending_usage and human_confirmed:
        result["real_usage_acceptance"] = _mark_status(
            usage, "verified", evidence="用户已按真实入口确认")
        pending_usage = False
    if pending_human and human_confirmed:
        result["human_boundary"] = _mark_status(
            result.get("human_boundary"), "verified", evidence="用户已确认")
        pending_human = False
    if (pending_human or pending_usage) and not human_confirmed:
        result["status"] = STATUS_PLANNING_INCOMPLETE
        result["closure"] = {
            "status": "needs_human",
            "reason": "真实使用验收缺入口证据，或人工边界未确认；机器不能替用户签收",
        }
        return result

    result["machine_acceptance"] = _mark_status(result.get("machine_acceptance"), "verified")
    result["writeback"] = _mark_status(result.get("writeback"), "done")
    result["status"] = STATUS_CLOSED
    result["closure"] = {
        "status": "closed",
        "note": note,
        "evidence": evidence,
        "closed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return result


def _mark_status(value: Any, status: str, evidence: str = "") -> Any:
    if isinstance(value, dict):
        result = dict(value)
        result["status"] = status
        if evidence:
            result["evidence"] = evidence
        return result
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, dict):
                copied = dict(item)
                copied["status"] = status
                if evidence:
                    copied["evidence"] = evidence
                out.append(copied)
            else:
                out.append(item)
        return out
    return value
