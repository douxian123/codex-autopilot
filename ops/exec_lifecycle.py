#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exec_lifecycle —— 把 central_daemon 的真实派发接到 worker 生命周期账本。

这里刻意只是一层薄适配：状态机、授权闸、顺序和 fail-closed 全在
`worker_lifecycle.py`，本模块只负责三件事：

  1. 从 daemon 的 job 形状生成合法 worker spec；
  2. 在真正启动 codex 之前完成 start/resume 声明，账本拒绝就不许启动；
  3. 在回收结果处上报 worker.completed / worker.failed / handback / progress。

不改 daemon 的并发上限、磁盘准入、重试次数或串行 land 语义。
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Callable

import worker_lifecycle as wl
from pass_registry import EFFECT_REVERSIBLE

_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]+")


def lifecycle_root(runtime: str | os.PathLike[str] | None = None) -> Path:
    """账本根：显式环境变量优先，默认落在 daemon 状态根下。"""
    raw = os.environ.get("KG_WORKER_LIFECYCLE_ROOT")
    if raw and str(raw).strip():
        return Path(raw).expanduser().resolve()
    base = Path(runtime) if runtime else Path.home() / ".codex-autopilot"
    return base.expanduser().resolve() / "worker-lifecycle"


def _service(runtime: str | os.PathLike[str] | None = None) -> wl.WorkerLifecycle:
    return wl.WorkerLifecycle(lifecycle_root(runtime))


def safe_worker_id(ticket_id: Any) -> str:
    raw = "exec-" + str(ticket_id or "unknown")
    cleaned = _UNSAFE.sub("-", raw).strip("-.")
    return (cleaned or "exec-unknown")[:128]


def spec_for_job(job: dict[str, Any]) -> dict[str, Any]:
    """从 daemon job 生成固定形状的 spec，不带任何隐藏授权。"""
    item = job.get("item") or {}
    rid = str(job.get("id") or item.get("id") or "unknown")
    codex = [str(x) for x in (job.get("codex") or ["codex"])]
    command = [*codex, "exec", "-C", str(job.get("worktree") or "")]

    resources = item.get("files") or item.get("resources") or []
    if not isinstance(resources, (list, tuple)):
        resources = [str(resources)]
    tools = item.get("tools") or ["codex_exec"]
    if not isinstance(tools, (list, tuple)):
        tools = [str(tools)]

    return {
        "worker_id": str(job.get("lifecycle_worker_id") or safe_worker_id(rid)),
        "ticket_id": rid,
        "lane": str(item.get("lane") or item.get("kind") or "exec"),
        "worktree": str(job.get("worktree") or ""),
        "command": command,
        "resources": [str(x) for x in resources if str(x).strip()],
        "tools": [str(x) for x in tools if str(x).strip()],
        # auto_exec 只有在用户显式打开控制开关后才到得了这里；隔离 worktree
        # 加 kgctl land 门禁是可逆的，不需要也不允许 inferred + destructive。
        "authorization": wl.AUTH_EXPLICIT,
        "effect": EFFECT_REVERSIBLE,
        "authorization_reason": "用户已打开 auto_exec；隔离 worktree 与 land 门禁",
        "expected_evidence": "kgctl land exit 0",
    }


def _fresh_worker_id(base: str) -> str:
    suffix = f"-{time.time_ns() % 1_000_000_000:09d}"
    return base[:128 - len(suffix)] + suffix


def _remember(job: dict[str, Any], worker_id: str,
              projection: dict[str, Any]) -> None:
    job["lifecycle_worker_id"] = worker_id
    job["lifecycle"] = {
        "worker_id": worker_id,
        "state": projection.get("state"),
        "generation": projection.get("generation"),
        "external_ref": projection.get("external_ref"),
        "last_seq": projection.get("last_seq"),
    }


def start_or_resume(
    job: dict[str, Any],
    launch: Callable[[], Any],
    runtime: str | os.PathLike[str] | None = None,
    reason: str = "auto_exec 派发",
) -> tuple[dict[str, Any] | None, str | None]:
    """声明一个 worker 并启动它；任何账本/授权失败都 fail-closed。"""
    service = _service(runtime)
    try:
        spec = wl.normalize_spec(spec_for_job(job))
        base_id = str(job.get("lifecycle_worker_id") or spec["worker_id"])
        current = service.status(base_id)
        state = current["state"]

        if state == "absent":
            worker_id = spec["worker_id"]
            projection = service.start(spec, launch=launch)
        elif state in wl.RESUMABLE_STATES:
            worker_id = base_id
            projection = service.resume(base_id, reason, launch=launch)
        elif state in wl.ACTIVE_STATES:
            # 调用方明确要求新开一次会话，旧账本却还在 active：先把旧代标成
            # interrupted，再用 resume 升 generation，杜绝无账本的裸 Popen。
            worker_id = base_id
            service.append(base_id, wl.WORKER_INTERRUPTED, {"reason": reason})
            projection = service.resume(base_id, reason, launch=launch)
        else:
            # handback / completed / failed 都是终态，route 必须开新 worker。
            worker_id = _fresh_worker_id(base_id)
            projection = service.start(dict(spec, worker_id=worker_id),
                                       launch=launch)
    except wl.LifecycleError as exc:
        return None, f"{type(exc).__name__}: {exc}"

    _remember(job, worker_id, projection)
    if projection.get("state") == "failed":
        return projection, str(projection.get("error") or "worker start failed")
    return projection, None


def handback(job: dict[str, Any], reason: str, target: str = "route",
             runtime: str | os.PathLike[str] | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """worker 主动交回路由；终态后同 ticket 只能由 route 开新 worker。"""
    worker_id = job.get("lifecycle_worker_id")
    if not worker_id:
        return None, None
    service = _service(runtime)
    try:
        projection = service.handback(str(worker_id), reason, target=target)
    except wl.LifecycleError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    _remember(job, str(worker_id), projection)
    return projection, None


def mark_recovered(job: dict[str, Any],
                   runtime: str | os.PathLike[str] | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """在途现场恢复：不新开会话，但必须让账本知道它仍在跑。

    recover 分支没有 Popen，它只是把已有的 worktree/提交交给收口门禁。这里
    绝不能走 start/resume 的 launch 语义去伪装成新 session；账本不存在时补
    一条带 `recovered:` 外ref 的记录，已经 active 时只记 progress。
    """
    service = _service(runtime)
    try:
        spec = wl.normalize_spec(spec_for_job(job))
        base_id = str(job.get("lifecycle_worker_id") or spec["worker_id"])
        current = service.status(base_id)
        state = current["state"]
        if state == "absent":
            worker_id = spec["worker_id"]
            projection = service.start(
                spec, launch=lambda: f"recovered:{job.get('worktree') or ''}")
        elif state in wl.ACTIVE_STATES:
            worker_id = base_id
            projection = service.append(
                base_id, wl.PROGRESS,
                {"note": "recovered existing worktree without new session"})
        elif state in wl.RESUMABLE_STATES:
            worker_id = base_id
            projection = service.resume(
                base_id, "恢复在途现场",
                launch=lambda: f"recovered:{job.get('worktree') or ''}")
        elif state in wl.TERMINAL_STATES:
            worker_id = _fresh_worker_id(base_id)
            projection = service.start(
                dict(spec, worker_id=worker_id),
                launch=lambda: f"recovered:{job.get('worktree') or ''}")
        else:
            worker_id = base_id
            projection = current
    except wl.LifecycleError as exc:
        return None, f"{type(exc).__name__}: {exc}"

    _remember(job, worker_id, projection)
    return projection, None


def report(job: dict[str, Any], result: dict[str, Any],
           runtime: str | os.PathLike[str] | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """把 daemon 的回收 outcome 落成生命周期事件；重复上报是空操作。"""
    worker_id = job.get("lifecycle_worker_id")
    if not worker_id:
        return None, None
    service = _service(runtime)
    try:
        worker_id = str(worker_id)
        current = service.status(worker_id)
        state = current["state"]
        if state == "absent" or state in wl.TERMINAL_STATES:
            return current, None

        outcome = str(result.get("outcome") or "")
        payload = {
            "outcome": outcome,
            "exit": result.get("exit"),
            "attempts": result.get("attempts"),
        }
        if outcome == "channel_unavailable":
            projection = service.handback(
                worker_id, "模型通道不可用，交回路由退避", target="route")
        elif outcome in ("relaunched", "disk_held"):
            projection = service.append(
                worker_id, wl.PROGRESS, {"note": outcome, **payload})
        elif outcome == "worker_done":
            projection = service.append(
                worker_id, wl.WORKER_COMPLETED, payload)
        elif outcome == "landed":
            projection = service.append(
                worker_id, wl.WORKER_COMPLETED, payload)
        elif state in wl.ACTIVE_STATES | {"interrupted"}:
            projection = service.append(
                worker_id, wl.WORKER_FAILED, payload)
        else:
            projection = current
    except wl.LifecycleError as exc:
        return None, f"{type(exc).__name__}: {exc}"

    _remember(job, worker_id, projection)
    return projection, None
