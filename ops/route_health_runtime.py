#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""route_health_runtime —— 把路线/效能检测接到真实运行现场的只读装配层。

`route_health.evaluate()` 是纯函数；本模块负责从现有 daemon / MCP 已有的
运行面数据里取判据，交给纯函数计算，再把结果收敛成固定形状。它不新增
第二套健康定义，也不写项目真相台账。

硬边界：
  * 没有在跑的实现 -> 明确的 idle ok；
  * 判据取不到 -> unknown，绝不写「正常」或 0；
  * 输出动作只有 `none` / `propose_review`；
  * 运行账本是 append-only，只允许 daemon 在状态签名变化时追加一行，
    不派工单、不改 worker、不改配置、不改代码。
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import route_health

ALLOWED_ACTIONS = {"none", "propose_review"}
LEDGER_NAME = "route_health.jsonl"
# 同一 attention 状态下，卡点分钟数/改动数/重试数继续增长只是同一件事的进度，
# 不是新的路线结论。签名里把这些数字归一，避免每拍 append + 通知打扰人。
_GROWTH_RE = re.compile(r"\d+\s*(?:分钟|个改动|次|个)")


def _now():
    return datetime.now(timezone.utc)


def _scope_path(value):
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def _path_in_scope(path, declared):
    candidate = _scope_path(path)
    if not candidate:
        return False
    for raw in declared:
        scope = _scope_path(raw)
        if not scope:
            continue
        if candidate == scope or candidate.startswith(scope.rstrip("/") + "/"):
            return True
    return False


def _exec_changed_paths(worktree):
    """只读取当前实现工作区的已跟踪改动；取不到就返回 None，不猜空。"""
    text = str(worktree or "").strip()
    if not text:
        return None
    wt = Path(text)
    if not wt.is_dir():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(wt), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    paths = []
    for line in (proc.stdout or "").splitlines():
        if len(line) < 4:
            continue
        raw = line[3:].strip()
        if " -> " in raw:
            raw = raw.split(" -> ", 1)[1].strip()
        raw = raw.strip('"')
        if raw:
            paths.append(raw)
    return paths


def _stall_minutes_from_blocked(blocked, rid, now=None):
    """从已知卡点算无关卡点分钟数；调度性等待不算偏航。"""
    records = (blocked or {}).get("exec") or []
    rec = next((r for r in records if isinstance(r, dict)
                and str(r.get("id") or "") == str(rid)), None)
    reason = str((rec or {}).get("reason") or "")
    benign = {"overlap_hold", "land_locked", "channel_unavailable", "retry_after"}
    if not reason or reason in benign:
        return 0
    ts = (rec or {}).get("ts")
    try:
        if isinstance(ts, (int, float)):
            start = datetime.fromtimestamp(float(ts), timezone.utc)
        else:
            start = datetime.fromisoformat(str(ts or "").replace("Z", "+00:00"))
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    delta = ((now or _now()) - start).total_seconds() / 60
    return max(0, int(delta))


def _sanitize(result):
    """只暴露只读结论；任何越权字段都在这里被剥掉。"""
    result = result or {}
    action = result.get("action")
    if action not in ALLOWED_ACTIONS:
        action = "none"
    return {
        "status": "ok",
        "level": str(result.get("level") or "unknown"),
        "headline": str(result.get("headline") or "路线与效能结论取不到"),
        "unknown": list(result.get("unknown") or []),
        "reasons": list(result.get("reasons") or []),
        "action": action,
        "checked_at": result.get("checked_at"),
    }


def summarize(payload, state=None, runtime=None, now=None):
    """从现有运行面取判据并算出只读摘要。MCP 与 daemon 共用这一份。"""
    now = now or _now()
    payload = payload or {}
    state = state or {}
    runtime_root = Path(runtime) if runtime else Path.home() / ".codex-autopilot"

    en = payload.get("exec_now")
    if not en:
        return _sanitize(route_health.evaluate({"idle": True}, now=now))

    rid = str(en.get("id") or "")
    runtime_signals = {}
    declared = None
    try:
        import exec_lifecycle
        jobs = ((state or {}).get("exec_workers") or {}).get("exec_jobs") or {}
        job = jobs.get(rid) or {}
        worker_id = str(job.get("lifecycle_worker_id")
                        or exec_lifecycle.safe_worker_id(rid))
        projection = exec_lifecycle._service(runtime_root).status(worker_id)
        spec = projection.get("spec") or {}
        declared = [
            str(x) for x in (spec.get("allowed_resources") or spec.get("resources") or [])
            if str(x).strip()
        ]
        runtime_signals["retry_count"] = max(
            0, int(projection.get("generation") or 1) - 1)
        ledger = exec_lifecycle.lifecycle_root(runtime_root) / f"worker-{worker_id}.jsonl"
        progress = 0
        for line in ledger.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if str((row or {}).get("type") or "") == "event.progress":
                progress += 1
        runtime_signals["progress_events"] = progress
    except Exception:
        # 生命周期账本取不到，scope / 重试 / 推进都保持缺项，检测器会诚实报 unknown。
        declared = None

    if declared:
        changed = _exec_changed_paths(en.get("worktree"))
        if changed is None:
            runtime_signals["mandate_scope_ok"] = None
            runtime_signals["off_scope_changes"] = None
        else:
            offenders = [p for p in changed if not _path_in_scope(p, declared)]
            runtime_signals["mandate_scope_ok"] = not offenders
            runtime_signals["off_scope_changes"] = len(offenders)
    else:
        runtime_signals["mandate_scope_ok"] = None
        runtime_signals["off_scope_changes"] = None

    # 面板投影会丢掉 exec_blocked 的 ts。这里回读 daemon 原始记录里的同一条，
    # 否则「已经卡了多久」永远取不到，检测器只能一直报 unknown。
    blocked_for_route = dict(payload.get("blocked") or {})
    raw_rec = (((state or {}).get("exec_blocked") or {}).get(rid))
    if isinstance(raw_rec, dict):
        blocked_for_route["exec"] = [{
            "id": rid,
            "reason": raw_rec.get("reason"),
            "ts": raw_rec.get("ts"),
        }]
    runtime_signals["unrelated_stall_minutes"] = _stall_minutes_from_blocked(
        blocked_for_route, rid, now=now)

    try:
        return _sanitize(route_health.evaluate(runtime_signals, now=now))
    except Exception as exc:
        return _sanitize({
            "level": "unknown",
            "headline": "路线与效能判据取不到：计算失败（%s）" % exc,
            "unknown": ["计算失败"],
            "reasons": [],
            "action": "none",
            "checked_at": now.isoformat(),
        })


def summarize_many(exec_payloads, state=None, runtime=None, now=None):
    """聚合多个在跑施工项的路线摘要；任一 attention 就抬 attention。

    多个 worker 时不能只检最后一个：那会把前面 worker 的 scope 偏离漏掉。
    仍然只输出只读结论，不产生任何动作字段。
    """
    now = now or _now()
    payloads = list(exec_payloads or [])
    if not payloads:
        return summarize({"exec_now": None}, state=state, runtime=runtime, now=now)

    results = [summarize(p, state=state, runtime=runtime, now=now) for p in payloads]
    attention = [r for r in results if r.get("level") == "attention"]
    unknown = [r for r in results if r.get("level") == "unknown"]
    chosen = attention[0] if attention else (unknown[0] if unknown else results[0])
    reasons = [reason for r in results for reason in r.get("reasons") or []]
    unknowns = [item for r in results for item in r.get("unknown") or []]
    checked = [r.get("checked_at") for r in results if r.get("checked_at")]
    return _sanitize({
        "level": chosen.get("level") or "unknown",
        "headline": chosen.get("headline") or "路线与效能结论取不到",
        "unknown": unknowns,
        "reasons": reasons,
        "action": "propose_review" if attention else "none",
        "checked_at": max(checked) if checked else now.isoformat(),
    })


def ledger_path(runtime):
    return Path(runtime) / LEDGER_NAME


def last_summary(runtime):
    """读最后一条巡检记录；没有账本或账本损坏时返回空字典。"""
    try:
        lines = ledger_path(runtime).read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[-1])
        return row if isinstance(row, dict) else {}
    except (OSError, ValueError, IndexError, AttributeError):
        return {}


def _signature_text(value):
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, sort_keys=True)
    return _GROWTH_RE.sub(lambda m: re.sub(r"\d+", "N", m.group(0)), text)


def signature(summary, exec_id=None):
    summary = summary or {}
    return "|".join([
        str(summary.get("level") or ""),
        str(summary.get("action") or ""),
        _signature_text(summary.get("headline") or ""),
        _signature_text(summary.get("unknown") or []),
        _signature_text(summary.get("reasons") or []),
        str(exec_id or ""),
    ])


def append_if_changed(runtime, summary, prior=None, exec_id=None, now=None):
    """状态签名变化才追加一行；同状态的下一拍只更新摘要，不刷屏。"""
    summary = _sanitize(summary)
    now = now or _now()
    sig = signature(summary, exec_id)
    prior_sig = None
    if isinstance(prior, dict):
        prior_sig = prior.get("signature")
    if not prior_sig:
        prior_sig = last_summary(runtime).get("signature")
    if prior_sig == sig:
        return {"written": False, "signature": sig}

    row = {
        "ts": now.isoformat(),
        "level": summary.get("level"),
        "headline": summary.get("headline"),
        "unknown": summary.get("unknown"),
        "reasons": summary.get("reasons"),
        "action": summary.get("action"),
        "exec_id": exec_id,
        "checked_at": summary.get("checked_at"),
        "signature": sig,
    }
    path = ledger_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {"written": True, "signature": sig}
