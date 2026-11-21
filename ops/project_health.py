#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""project_health —— 为「整个项目是否健康」负责的只读聚合层（stdlib only）。

为什么单开这一层
----------------
状态管家只看 harness（压缩频率 / 峰值 input / 用量），它回答的是「这台机器累不累」，
不是「这个项目还健康吗」。用户要的是后者：产品主线有没有过期、约束有没有真的沉淀、
标成完成的东西有没有证据、执行面有没有真卡住、验收有没有积压。

这一层**不造第二套真相**：所有判据都读已有台账（mainline / ledger / acceptance /
constraints / decisions / judgments）和面板已经算好的运行面信号。它只做聚合和分级，
不写文件、不改状态、不触发任何动作。

三态与设计约束
--------------
  * `ok`       这一层有据可查且没有需要处理的事；
  * `attention` 有具体、可指出的问题；
  * `unknown`  取不到判据 —— 绝不降级成「正常」。

`overlap_hold` 是排队让路，不是病：它必须出现在信号里，但不得把整体健康抬成 attention，
否则正常调度会天天报红，用户会学会忽略警报。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

try:
    from ledger_state import latest_ledger_by_id
except Exception:  # 单测直接拷模块时也能跑
    def latest_ledger_by_id(rows):
        latest = {}
        for row in rows:
            if isinstance(row, dict) and row.get("id"):
                latest[row["id"]] = row
        return latest

OPEN_STATUSES = ("open", "doing", "todo", "wip", "blocked")
MAINLINE_STALE_DAYS = 30
HUMAN_QUEUE_BACKLOG = 3


def _now():
    return datetime.now(timezone.utc)


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_jsonl(path):
    out = []
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _read_jsonl_source(path):
    """读 JSONL 并区分「空台账」与「台账文件不存在」。

    空台账是事实，可以据此判断「预期没沉淀」；文件不存在是取不到，
    不能拿 0 条冒充。这个区别对用户很重要：前者是项目状态问题，
    后者是健康探针自己的数据缺失。
    """
    source = Path(path)
    return _read_jsonl(source), source.exists()


def _parse_ts(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def truth_snapshot(repo):
    """读一遍项目真相，返回健康判定需要的计数与时间。只读。"""
    base = Path(repo).expanduser()
    truth = base / "ops" / "truth"
    mainline = _read_json(truth / "mainline.json")
    ledger_rows, ledger_present = _read_jsonl_source(truth / "ledger.jsonl")
    ledger = latest_ledger_by_id(ledger_rows)
    open_items = ([r for r in ledger.values() if r.get("status") in OPEN_STATUSES]
                  if ledger_present else None)
    done_no_evidence = ([
        r for r in ledger.values()
        if r.get("status") == "done" and not str(r.get("evidence") or "").strip()
    ] if ledger_present else None)

    constraints_rows, constraints_present = _read_jsonl_source(truth / "constraints.jsonl")
    acceptance_rows, acceptance_present = _read_jsonl_source(truth / "acceptance.jsonl")
    acc = latest_ledger_by_id(acceptance_rows)
    acc_rows = list(acc.values())
    human_pending = [
        r for r in acc_rows
        if r.get("needs_human") and r.get("status") in ("todo", "wip", "blocked")
    ]
    human_ready = [r for r in human_pending if str(r.get("evidence") or "").strip()]
    self_check = [r for r in human_pending if not str(r.get("evidence") or "").strip()]
    return {
        "path": str(base),
        "mainline_present": mainline is not None,
        "mainline_updated": (mainline or {}).get("updated"),
        "mainline_stage": (mainline or {}).get("stage"),
        "open_items": len(open_items) if ledger_present else None,
        "done_no_evidence": len(done_no_evidence) if ledger_present else None,
        "constraints": len(constraints_rows) if constraints_present else None,
        "constraints_present": constraints_present,
        "ledger_present": ledger_present,
        "decisions": len(_read_jsonl(truth / "decisions.jsonl")),
        "judgments": len(_read_jsonl(truth / "judgments.jsonl")),
        "acceptance_total": len(acc_rows),
        "acceptance_present": acceptance_present,
        "human_ready": len(human_ready) if acceptance_present else None,
        "self_check": len(self_check) if acceptance_present else None,
        "verified": (len([r for r in acc_rows if r.get("status") == "verified"])
                     if acceptance_present else None),
    }


def _layer(key, label, state, reasons):
    return {"key": key, "label": label, "state": state, "reasons": reasons}


def _truth_layer(truth, now):
    reasons = []
    state = "ok"

    updated = _parse_ts(truth.get("mainline_updated"))
    if not truth.get("mainline_present"):
        return _layer("truth", "项目真相", "unknown", ["mainline.json 读不到"])
    if updated is None:
        state = "unknown"
        reasons.append("产品主线更新时间读不到")
    else:
        age = max(0, (now - updated).days)
        if age > MAINLINE_STALE_DAYS:
            state = "attention"
            reasons.append("产品主线 %d 天没更新（%s）"
                           % (age, truth.get("mainline_updated")))

    if not truth.get("constraints_present", True):
        state = "unknown"
        reasons.append("约束台账读不到")
    elif truth.get("constraints", 0) == 0:
        state = "attention"
        reasons.append("约束台账 0 条：用户预期还没沉淀成可调用的上下文")
    if not truth.get("ledger_present", True):
        state = "unknown"
        reasons.append("问题台账读不到，完成项证据检查取不到")
    if truth.get("done_no_evidence", 0):
        state = "attention"
        reasons.append("%d 条标了完成但没有证据" % truth["done_no_evidence"])

    if state == "ok":
        reasons.append("主线新鲜、约束 %d 条、完成项都有证据" % truth.get("constraints", 0))
    return _layer("truth", "项目真相", state, reasons)


def _flow_layer(runtime):
    if runtime is None:
        return _layer("flow", "执行流动", "unknown", ["运行面信号没接上"])
    reasons = []
    state = "ok"

    stuck = int(runtime.get("exec_stuck") or 0)
    failures = int(runtime.get("land_failures") or 0)
    resolve = int(runtime.get("resolve") or 0)
    overlap = int(runtime.get("overlap_waiting") or 0)

    if stuck:
        state = "attention"
        reasons.append("%d 条实现卡住" % stuck)
    if failures:
        state = "attention"
        reasons.append("%d 条落盘门禁失败" % failures)
    if resolve:
        # 机器自己在拆冲突，不是用户要处理的事；但健康层必须看得见。
        reasons.append("%d 条冲突在自动拆解" % resolve)
    if overlap:
        # 排队让路是调度，不是病。只报信号，不抬级别。
        reasons.append("%d 条在排队让路（不是卡住）" % overlap)
    if state == "ok" and not reasons:
        reasons.append("没有卡住的实现或落盘")
    return _layer("flow", "执行流动", state, reasons)


def _acceptance_layer(truth, runtime):
    reasons = []
    state = "ok"
    if not truth.get("acceptance_present", True):
        return _layer("accept", "验收", "unknown", ["验收台账读不到"])
    ready = int(truth.get("human_ready") or 0)
    self_check = int(truth.get("self_check") or 0)
    auto_exec = bool((runtime or {}).get("auto_exec"))

    if self_check:
        reasons.append("%d 条还没自验" % self_check)
        if runtime is not None and not auto_exec:
            state = "attention"
            reasons.append("自动实现关着，没人会去跑")
    if ready >= HUMAN_QUEUE_BACKLOG:
        state = "attention"
        reasons.append("%d 条已自验在等你，验收积压" % ready)
    elif ready:
        reasons.append("%d 条等你验收" % ready)
    if state == "ok" and not reasons:
        reasons.append("没有待自验或待验收的项")
    return _layer("accept", "验收", state, reasons)


def evaluate(truth, runtime=None, now=None):
    """把真相快照 + 运行面信号收敛成一份分层健康结论。纯函数。"""
    now = now or _now()
    if truth is None:
        return {
            "status": "unknown", "level": "unknown",
            "headline": "项目健康取不到：真相目录读不到",
            "layers": [], "signals": {},
        }
    layers = [
        _truth_layer(truth, now),
        _flow_layer(runtime),
        _acceptance_layer(truth, runtime),
    ]
    order = {"attention": 0, "unknown": 1, "ok": 2}
    worst = sorted(layers, key=lambda r: order.get(r["state"], 9))[0]
    level = worst["state"]
    # 标题只挂第一条真正需要处理的理由，避免把整段台账搬上首屏。
    reasons = [x for layer in layers if layer["state"] == level for x in layer["reasons"]]
    headline = reasons[0] if reasons else "项目健康"
    return {
        "status": "ok",
        "level": level,
        "headline": headline,
        "layers": layers,
        "signals": {
            "open_items": truth.get("open_items"),
            "constraints": truth.get("constraints"),
            "self_check": truth.get("self_check"),
            "human_ready": truth.get("human_ready"),
            "exec_stuck": (runtime or {}).get("exec_stuck"),
            "overlap_waiting": (runtime or {}).get("overlap_waiting"),
        },
    }


def compute(repo, runtime=None, now=None):
    """读台账并给出健康结论；任何读失败都如实返回 unknown。"""
    return evaluate(truth_snapshot(repo), runtime=runtime, now=now)
