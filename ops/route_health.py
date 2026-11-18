#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""route_health —— 路线与效能健康的只读检测器（stdlib only）。

这个模块不是第八个管家。它把用户新提的两类关注变成可执行、可测试的判据：

  1. 路线是否仍围绕 mandate：工作项声明的 files / 目标是否还与实际信号一致；
  2. 效用比是否健康：是否长时间在无关卡点、重试或无进展上空转。

硬边界：**只读、只有提案**。它不杀 worker、不改配置、不改台账、不派活。
判据不足时返回 unknown，不能把「不知道」降级成「正常」。
"""

from __future__ import annotations

from datetime import datetime, timezone

DEFAULT_STALL_MINUTES = 45
DEFAULT_RETRY_LIMIT = 3


def _num(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _state(reasons, hard):
    if not reasons:
        return "ok", ["判据齐全，没有发现路线偏离或低效空转"]
    if hard:
        return "attention", reasons
    return "ok", reasons


def evaluate(runtime=None, now=None):
    """按现有运行信号评估路线健康与效用比。纯函数。

    runtime 约定字段：
      - mandate_scope_ok: bool|None  实际改动是否仍在 mandate 声明范围内；
      - off_scope_changes: int       超出 mandate scope 的改动数；
      - unrelated_stall_minutes: int 与验收/目标无关的卡点时长；
      - retry_count: int             同一卡点重试次数；
      - progress_events: int         观察窗口内的有效推进事件数。

    缺失字段按 unknown 处理，绝不脑补。bool 为 False 视为明确偏离，None 视为取不到。
    """
    runtime = runtime or {}
    now = now or datetime.now(timezone.utc)

    # 没有在跑的实现时，「路线偏掉」没有适用对象。这是明确的空闲事实，
    # 不是缺判据；仍然只允许只读结论，不生成任何动作。
    if runtime.get("idle") is True:
        return {
            "status": "ok",
            "level": "ok",
            "headline": "当前没有在跑的实现，路线与效能无需纠偏",
            "unknown": [],
            "reasons": [],
            "action": "none",
            "checked_at": now.isoformat(),
        }

    unknown = []
    reasons = []

    scope_ok = runtime.get("mandate_scope_ok")
    if scope_ok is None:
        unknown.append("mandate 范围判据取不到")
    elif scope_ok is False:
        reasons.append("实际工作已经偏离 mandate 声明的范围")

    off_scope = runtime.get("off_scope_changes")
    if off_scope is None:
        unknown.append("scope 外改动数取不到")
    elif _num(off_scope) > 0:
        reasons.append("%d 个改动落在 mandate scope 外" % _num(off_scope))

    stall = runtime.get("unrelated_stall_minutes")
    if stall is None:
        unknown.append("无关卡点时长取不到")
    elif _num(stall) >= DEFAULT_STALL_MINUTES:
        reasons.append("%d 分钟耗在与验收无关的卡点" % _num(stall))

    retries = runtime.get("retry_count")
    if retries is None:
        unknown.append("同卡点重试数取不到")
    elif _num(retries) > DEFAULT_RETRY_LIMIT:
        reasons.append("同一卡点重试 %d 次，效用比异常" % _num(retries))

    progress = runtime.get("progress_events")
    if progress is None:
        unknown.append("有效推进事件数取不到")
    elif _num(progress) == 0 and _num(retries) > 0:
        reasons.append("重试在增加但有效推进为 0")

    if unknown:
        return {
            "status": "ok",
            "level": "unknown",
            "headline": "路线与效能判据取不到：" + unknown[0],
            "unknown": unknown,
            "reasons": reasons,
            "action": "none",
            "checked_at": now.isoformat(),
        }

    level, final_reasons = _state(reasons, hard=bool(reasons))
    return {
        "status": "ok",
        "level": level,
        "headline": final_reasons[0],
        "unknown": [],
        "reasons": final_reasons,
        # 只生成提案。真正纠偏必须经过现有 route/PM/用户授权链。
        "action": "propose_review" if reasons else "none",
        "checked_at": now.isoformat(),
    }
