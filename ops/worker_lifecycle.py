#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""worker_lifecycle —— 后台 worker 的显式生命周期契约（stdlib only）。

它回答的是 DeepSeek Super-agent / Claude Projects 都绕不开的那层问题：
一个后台任务怎样被唤醒、恢复、停下、交回人，并且事后可审计。

这里不启动进程，也不认识 Codex session。它只做一件可以机器验证的事：
把 worker 的每次状态变化写成 append-only 事件账本，并禁止非法跳转。

状态机
------
    absent --start--> starting --started--> running
                         |                    |
                         |                    +--stop_requested--> stopping --stopped--> stopped
                         |                    |                        |                  |
                         +--start_failed--> failed                    +--interrupted-----> interrupted
                                                |                       |
                                                +--failed               +--resume--> starting

    running / stopping / interrupted --handback--> handed_back（终态）
    running / stopping --completed--> completed（终态）

`stopped` 和 `interrupted` 是唯一可恢复态。`resume` 必须带理由，并生成新一代
generation；`handed_back` 不能原地恢复，只能由路由层开新 worker。

权限与安全
----------
  * `authorization` 复用 pass_registry 的 explicit / inferred。
  * `effect` 复用 pass_registry 的 read / proposal / reversible / destructive。
  * `inferred + destructive` 直接拒绝，和 pass_registry 的禁行线一致。
  * destructive 还必须有 `authorization_reason`，否则 fail-closed。
  * 每个 worker 只记自己的账本文件，worker_id 受白名单字符约束。
  * 事件带递增 seq；调用方可传 `expected_seq` 做乐观并发检查。
  * 账本损坏时抛 `LedgerCorrupt`，绝不把「读不懂」当成「没有任务」。
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

from pass_registry import (
    AUTH_EXPLICIT,
    AUTH_INFERRED,
    EFFECT_DESTRUCTIVE,
    normalize_authorization,
    normalize_effect,
)

START_REQUESTED = "start.requested"
WORKER_STARTED = "worker.started"
START_FAILED = "worker.start_failed"
PROGRESS = "event.progress"
STOP_REQUESTED = "worker.stop_requested"
WORKER_STOPPED = "worker.stopped"
WORKER_INTERRUPTED = "worker.interrupted"
WORKER_COMPLETED = "worker.completed"
WORKER_FAILED = "worker.failed"
WORKER_HANDBACK = "worker.handback"
WORKER_RESUMED = "worker.resumed"

EVENT_TYPES = (
    START_REQUESTED,
    WORKER_STARTED,
    START_FAILED,
    PROGRESS,
    STOP_REQUESTED,
    WORKER_STOPPED,
    WORKER_INTERRUPTED,
    WORKER_COMPLETED,
    WORKER_FAILED,
    WORKER_HANDBACK,
    WORKER_RESUMED,
)

# 这些事件只能由 start / resume 内部产生。开放给外部调用会让调用方绕过
# 授权校验和 generation 分配，恰好把这套契约最硬的部分拆掉。
INTERNAL_EVENTS = {START_REQUESTED, WORKER_STARTED, START_FAILED, WORKER_RESUMED}

EVENT_ALIASES = {
    "start_requested": START_REQUESTED,
    "started": WORKER_STARTED,
    "start_failed": START_FAILED,
    "progress": PROGRESS,
    "event": PROGRESS,
    "stop_requested": STOP_REQUESTED,
    "stopped": WORKER_STOPPED,
    "interrupted": WORKER_INTERRUPTED,
    "completed": WORKER_COMPLETED,
    "failed": WORKER_FAILED,
    "handback": WORKER_HANDBACK,
    "resumed": WORKER_RESUMED,
}

ACTIVE_STATES = {"starting", "running", "stopping"}
RESUMABLE_STATES = {"stopped", "interrupted"}
TERMINAL_STATES = {"completed", "failed", "handed_back"}
SAFE_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class LifecycleError(Exception):
    """生命周期契约的基类错误。"""


class ConflictError(LifecycleError):
    """当前状态不允许该动作，或 expected_seq 与账本不一致。"""


class AuthorizationError(LifecycleError):
    """授权来源 / 副作用等级不满足硬闸。"""


class LedgerCorrupt(LifecycleError):
    """账本内容无法安全投影；必须显式处理，不能静默当空。"""


def normalize_event_type(value: Any) -> str:
    raw = str(value or "").strip()
    return EVENT_ALIASES.get(raw, raw)


def normalize_spec(raw: Any) -> dict[str, Any]:
    """把外部 spec 收敛成固定形状，并在入口拦住 inferred + destructive。"""
    if not isinstance(raw, dict):
        raise LifecycleError("worker spec 必须是对象")

    worker_id = str(raw.get("worker_id") or "").strip()
    if not SAFE_WORKER_ID.match(worker_id):
        raise LifecycleError("worker_id 不合法：只允许字母数字和 _ . : -")

    ticket_id = str(raw.get("ticket_id") or "").strip()
    lane = str(raw.get("lane") or "").strip()
    if not ticket_id or not lane:
        raise LifecycleError("ticket_id 和 lane 不能为空")

    command = raw.get("command")
    if isinstance(command, str):
        command = [command]
    if not isinstance(command, (list, tuple)) or not command:
        raise LifecycleError("command 必须是非空数组")
    command = [str(x) for x in command]

    def _string_tuple(key: str) -> tuple[str, ...]:
        value = raw.get(key) or []
        if not isinstance(value, (list, tuple)):
            raise LifecycleError(f"{key} 必须是数组")
        return tuple(str(x) for x in value)

    authorization = normalize_authorization(
        raw.get("authorization"), default=AUTH_INFERRED)
    effect = normalize_effect(raw.get("effect"), default=EFFECT_DESTRUCTIVE)
    reason = str(raw.get("authorization_reason") or "").strip()

    if authorization == AUTH_INFERRED and effect == EFFECT_DESTRUCTIVE:
        raise AuthorizationError("inferred + destructive 不允许启动 worker")
    if effect == EFFECT_DESTRUCTIVE and not reason:
        raise AuthorizationError("destructive worker 必须声明 authorization_reason")

    return {
        "worker_id": worker_id,
        "ticket_id": ticket_id,
        "lane": lane,
        "worktree": str(raw.get("worktree") or "").strip(),
        "command": command,
        "resources": list(_string_tuple("resources")),
        "tools": list(_string_tuple("tools")),
        "authorization": authorization,
        "effect": effect,
        "authorization_reason": reason,
        "expected_evidence": str(raw.get("expected_evidence") or "").strip(),
    }


def project(events: list[dict[str, Any]]) -> dict[str, Any]:
    """从事件账本推导当前状态。只读、纯函数、可重复调用。"""
    state = "absent"
    spec: dict[str, Any] | None = None
    generation = 0
    launched = False
    external_ref: Any = None
    stop_reason = ""
    handback_reason = ""
    handback_target = ""
    resume_reason = ""
    error = ""
    updated_at = ""
    last_seq = 0

    def need(expected: set[str], event_type: str) -> None:
        if state not in expected:
            raise LedgerCorrupt(
                f"{event_type} 不能发生在状态 {state!r}")

    for index, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            raise LedgerCorrupt(f"第 {index} 条事件不是对象")
        if event.get("seq") != index:
            raise LedgerCorrupt(f"第 {index} 条事件 seq 不连续：{event.get('seq')!r}")
        event_type = str(event.get("type") or "")
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            raise LedgerCorrupt(f"第 {index} 条事件 payload 不是对象")
        last_seq = index
        updated_at = str(event.get("ts") or updated_at)

        if event_type == START_REQUESTED:
            need({"absent"}, event_type)
            spec = payload.get("spec")
            if not isinstance(spec, dict):
                raise LedgerCorrupt("start.requested 缺少 spec")
            state = "starting"
            generation = 1
            launched = bool(payload.get("launch_claimed"))
            error = ""
        elif event_type == WORKER_RESUMED:
            need(RESUMABLE_STATES, event_type)
            state = "starting"
            generation = int(payload.get("generation") or generation + 1)
            resume_reason = str(payload.get("reason") or "")
            launched = bool(payload.get("launch_claimed"))
            external_ref = None
            error = ""
        elif event_type == WORKER_STARTED:
            need({"starting"}, event_type)
            state = "running"
            launched = True
            external_ref = payload.get("ref")
            error = ""
        elif event_type == START_FAILED:
            need({"starting"}, event_type)
            state = "failed"
            error = str(payload.get("error") or "start failed")
        elif event_type == PROGRESS:
            need(ACTIVE_STATES | {"interrupted"}, event_type)
        elif event_type == STOP_REQUESTED:
            need({"starting", "running"}, event_type)
            state = "stopping"
            stop_reason = str(payload.get("reason") or "")
        elif event_type == WORKER_STOPPED:
            need({"starting", "running", "stopping"}, event_type)
            state = "stopped"
            stop_reason = stop_reason or str(payload.get("reason") or "")
        elif event_type == WORKER_INTERRUPTED:
            need(ACTIVE_STATES, event_type)
            state = "interrupted"
        elif event_type == WORKER_COMPLETED:
            need(ACTIVE_STATES, event_type)
            state = "completed"
        elif event_type == WORKER_FAILED:
            need(ACTIVE_STATES | {"interrupted"}, event_type)
            state = "failed"
            error = str(payload.get("error") or "worker failed")
        elif event_type == WORKER_HANDBACK:
            need(ACTIVE_STATES | {"interrupted"}, event_type)
            state = "handed_back"
            handback_reason = str(payload.get("reason") or "")
            handback_target = str(payload.get("target") or "route")
        else:
            raise LedgerCorrupt(f"未知事件类型：{event_type!r}")

    return {
        "state": state,
        "spec": spec,
        "generation": generation,
        "launch_claimed": launched,
        "external_ref": external_ref,
        "stop_reason": stop_reason,
        "handback_reason": handback_reason,
        "handback_target": handback_target,
        "resume_reason": resume_reason,
        "error": error,
        "updated_at": updated_at,
        "event_count": len(events),
        "last_seq": last_seq,
    }


class WorkerLifecycle:
    """以 append-only JSONL 为唯一真相的 worker 生命周期。"""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).expanduser().resolve()

    def _path(self, worker_id: str) -> Path:
        worker_id = str(worker_id or "").strip()
        if not SAFE_WORKER_ID.match(worker_id):
            raise LifecycleError("worker_id 不合法：只允许字母数字和 _ . : -")
        return self.root / f"worker-{worker_id}.jsonl"

    def _read(self, worker_id: str) -> tuple[Path, list[dict[str, Any]]]:
        path = self._path(worker_id)
        if not path.exists():
            return path, []
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LedgerCorrupt(f"读账本失败：{exc}") from exc
        events: list[dict[str, Any]] = []
        for number, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise LedgerCorrupt(f"账本第 {number} 行不是合法 JSON") from exc
        project(events)  # 提前校验，坏账本不许继续追加
        return path, events

    def _mutate(
        self,
        worker_id: str,
        make_event: Callable[[dict[str, Any], list[dict[str, Any]], int],
                             tuple[str, dict[str, Any]] | None],
        expected_seq: int | None = None,
    ) -> tuple[dict[str, Any], int]:
        """追加一条事件。

        `expected_seq` 表示调用方最后一次看到的事件序号，也就是当前账本的
        last_seq；不是下一条要写的 seq。两个校验点都必须用这个语义。
        """
        path, events = self._read(worker_id)
        seq = len(events) + 1
        if expected_seq is not None and int(expected_seq) != len(events):
            raise ConflictError(
                f"expected_seq={expected_seq} 与账本 last_seq={len(events)} 不一致")

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                current_raw = handle.read()
                current: list[dict[str, Any]] = []
                for number, line in enumerate(current_raw.splitlines(), start=1):
                    if not line.strip():
                        continue
                    try:
                        current.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        raise LedgerCorrupt(
                            f"账本第 {number} 行不是合法 JSON") from exc
                current_projection = project(current)
                current_seq = len(current) + 1
                if expected_seq is not None and int(expected_seq) != len(current):
                    raise ConflictError(
                        f"expected_seq={expected_seq} 与账本 last_seq={len(current)} 不一致")

                made = make_event(current_projection, current, current_seq)
                if made is None:
                    return current_projection, len(current)
                event_type, payload = made
                event_type = normalize_event_type(event_type)
                if event_type not in EVENT_TYPES:
                    raise LifecycleError(f"未知事件类型：{event_type!r}")
                candidate_event = {
                    "seq": current_seq,
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "worker_id": worker_id,
                    "type": event_type,
                    "payload": payload,
                }
                try:
                    candidate_projection = project(current + [candidate_event])
                except LedgerCorrupt as exc:
                    # 已落盘的 current 已在 _read/project 校验过；这里失败只可能是
                    # 本次候选事件违反状态机。对调用方应报冲突，而不是把健康账本
                    # 说成损坏；真正损坏的账本仍由 _read 保持 fail-closed。
                    raise ConflictError(str(exc)) from exc
                handle.seek(0, os.SEEK_END)
                handle.write(json.dumps(candidate_event, ensure_ascii=False,
                                        sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                return candidate_projection, current_seq
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def status(self, worker_id: str) -> dict[str, Any]:
        _, events = self._read(worker_id)
        return project(events)

    def start(
        self,
        spec: dict[str, Any],
        launch: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        """声明并启动一个 worker。重复调用同一个 spec 是幂等的。"""
        normal = normalize_spec(spec)
        worker_id = normal["worker_id"]
        claimed = False

        def make_event(current, _events, _seq):
            nonlocal claimed
            if current["state"] == "absent":
                claimed = True
                return START_REQUESTED, {"spec": normal, "launch_claimed": True}
            if current["state"] in ACTIVE_STATES and current.get("spec") == normal:
                return None
            raise ConflictError(
                f"worker {worker_id} 当前状态 {current['state']!r} 不允许 start；"
                "可恢复态请先 resume，终态请新建 worker")

        projection, seq = self._mutate(worker_id, make_event)
        if not claimed:
            return projection

        ref = None
        error = ""
        try:
            if launch is not None:
                ref = launch()
        except Exception as exc:  # 启动失败也要落账，不许留下 starting 黑洞
            error = f"{type(exc).__name__}: {exc}"

        if error:
            return self._append_internal(worker_id, START_FAILED,
                                         {"error": error}, expected_seq=seq)
        return self._append_internal(worker_id, WORKER_STARTED,
                                     {"ref": ref}, expected_seq=seq)

    def resume(
        self,
        worker_id: str,
        reason: str,
        launch: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        """显式恢复 stopped / interrupted worker，并分配新一代 generation。"""
        reason = str(reason or "").strip()
        if not reason:
            raise LifecycleError("resume 必须给出 reason")
        claimed = False
        generation = 0

        def make_event(current, _events, _seq):
            nonlocal claimed, generation
            if current["state"] not in RESUMABLE_STATES:
                raise ConflictError(
                    f"worker {worker_id} 当前状态 {current['state']!r} 不可恢复")
            generation = int(current.get("generation") or 0) + 1
            claimed = True
            return WORKER_RESUMED, {
                "reason": reason,
                "generation": generation,
                "launch_claimed": True,
            }

        projection, seq = self._mutate(worker_id, make_event)
        if not claimed:
            return projection

        ref = None
        error = ""
        try:
            if launch is not None:
                ref = launch()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        if error:
            return self._append_internal(worker_id, START_FAILED,
                                         {"error": error}, expected_seq=seq)
        return self._append_internal(worker_id, WORKER_STARTED,
                                     {"ref": ref}, expected_seq=seq)

    def stop(self, worker_id: str, reason: str,
             expected_seq: int | None = None) -> dict[str, Any]:
        reason = str(reason or "").strip()
        if not reason:
            raise LifecycleError("stop 必须给出 reason")

        def make_event(current, _events, _seq):
            if current["state"] in {"stopping", "stopped"} | TERMINAL_STATES:
                return None
            if current["state"] not in ACTIVE_STATES:
                raise ConflictError(
                    f"worker {worker_id} 当前状态 {current['state']!r} 不能 stop")
            return STOP_REQUESTED, {"reason": reason}

        return self._mutate(worker_id, make_event, expected_seq=expected_seq)[0]

    def handback(
        self,
        worker_id: str,
        reason: str,
        target: str = "route",
        expected_seq: int | None = None,
    ) -> dict[str, Any]:
        """超出工单范围 / 权限不足时交回路由，不许硬做。"""
        reason = str(reason or "").strip()
        if not reason:
            raise LifecycleError("handback 必须给出 reason")

        def make_event(current, _events, _seq):
            if current["state"] not in ACTIVE_STATES | {"interrupted"}:
                raise ConflictError(
                    f"worker {worker_id} 当前状态 {current['state']!r} 不能 handback")
            return WORKER_HANDBACK, {
                "reason": reason,
                "target": str(target or "route"),
            }

        return self._mutate(worker_id, make_event, expected_seq=expected_seq)[0]

    def append(
        self,
        worker_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        expected_seq: int | None = None,
    ) -> dict[str, Any]:
        """追加一个受状态机约束的事件。"""
        resolved = normalize_event_type(event_type)
        if resolved in INTERNAL_EVENTS:
            raise LifecycleError(f"{event_type} 是内部事件，不能用 append 伪造")
        if resolved not in EVENT_TYPES:
            raise LifecycleError(f"未知事件类型：{event_type!r}")
        return self._append_internal(worker_id, resolved, payload,
                                     expected_seq=expected_seq)

    def _append_internal(
        self,
        worker_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        expected_seq: int | None = None,
    ) -> dict[str, Any]:
        """start/resume 专用落账；内部事件的授权与 generation 已由调用方校验。"""
        resolved = normalize_event_type(event_type)
        if resolved not in EVENT_TYPES:
            raise LifecycleError(f"未知事件类型：{event_type!r}")
        payload = dict(payload or {})

        def make_event(_current, _events, _seq):
            return resolved, payload

        return self._mutate(worker_id, make_event, expected_seq=expected_seq)[0]

    def list_workers(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        result = []
        for path in sorted(self.root.glob("worker-*.jsonl")):
            worker_id = path.stem[len("worker-"):]
            try:
                projection = self.status(worker_id)
            except LedgerCorrupt as exc:
                projection = {"state": "unknown", "error": str(exc)}
            result.append({"worker_id": worker_id, **projection})
        return result


def _load_spec(value: str) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        raise LifecycleError("start 需要 --spec 或 --spec-json")
    if text.startswith("{"):
        return json.loads(text)
    return json.loads(Path(text).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=os.environ.get("KG_WORKER_LIFECYCLE_ROOT"),
                        required=not os.environ.get("KG_WORKER_LIFECYCLE_ROOT"),
                        help="事件账本根目录，也可用 KG_WORKER_LIFECYCLE_ROOT")
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start")
    p_start.add_argument("--spec", help="spec JSON 文件路径")
    p_start.add_argument("--spec-json", help="spec JSON 文本")
    p_start.add_argument("--ref", default=None, help="外部进程 / session 引用")

    p_status = sub.add_parser("status")
    p_status.add_argument("--worker-id", required=True)

    p_stop = sub.add_parser("stop")
    p_stop.add_argument("--worker-id", required=True)
    p_stop.add_argument("--reason", required=True)

    p_event = sub.add_parser("event")
    p_event.add_argument("--worker-id", required=True)
    p_event.add_argument("--type", required=True)
    p_event.add_argument("--payload-json", default="{}")

    p_handback = sub.add_parser("handback")
    p_handback.add_argument("--worker-id", required=True)
    p_handback.add_argument("--reason", required=True)
    p_handback.add_argument("--target", default="route")

    p_resume = sub.add_parser("resume")
    p_resume.add_argument("--worker-id", required=True)
    p_resume.add_argument("--reason", required=True)
    p_resume.add_argument("--ref", default=None)

    sub.add_parser("list")
    args = parser.parse_args(argv)

    service = WorkerLifecycle(args.root)
    try:
        if args.command == "start":
            spec = _load_spec(args.spec_json or args.spec)
            out = service.start(spec, launch=lambda: args.ref)
        elif args.command == "status":
            out = service.status(args.worker_id)
        elif args.command == "stop":
            out = service.stop(args.worker_id, args.reason)
        elif args.command == "event":
            out = service.append(args.worker_id, args.type,
                                 json.loads(args.payload_json))
        elif args.command == "handback":
            out = service.handback(args.worker_id, args.reason, args.target)
        elif args.command == "resume":
            out = service.resume(args.worker_id, args.reason,
                                 launch=lambda: args.ref)
        else:
            out = service.list_workers()
    except LifecycleError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    print(json.dumps(out, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
