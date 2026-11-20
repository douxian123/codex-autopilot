#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wake_resume —— 定时唤醒与显式跨工单恢复的运行层账本。

这个模块刻意不启动 worker、不改分支、不直接合流。它只做三件事：

  1. 把「到点提醒 / 显式恢复某个工单」落成 append-only 请求；
  2. 到期后把请求送回唯一入口（kgctl inbox），让既有 route/exec 接管；
  3. 用状态机保证失败可重试、成功不重复投递、坏账本 fail-closed。

跨工单恢复不是把旧 worker 唤醒复用。旧 worker 已经是终态时，只能带着
`resume_from` 证据新开一个工单；旧 worker 还在 active/resumable 时直接拒绝，
要求调用方走 `worker_lifecycle.resume`。这样 timer 永远不会绕过生命周期契约。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import worker_lifecycle as wl


class WakeResumeError(Exception):
    """唤醒/恢复账本的基类错误。"""


class AuthorizationError(WakeResumeError):
    """authorization 不是 explicit，或请求想越权。"""


class ConflictError(WakeResumeError):
    """当前状态不允许这个动作。"""


class LedgerCorrupt(WakeResumeError):
    """账本内容无法安全投影；绝不能当成「没有到期任务」。"""


KIND_WAKE = "wake"
KIND_RESUME = "resume"
KINDS = (KIND_WAKE, KIND_RESUME)

SPEC_EVENTS = {
    KIND_WAKE: "wake.scheduled",
    KIND_RESUME: "resume.requested",
}
FIRE_EVENTS = {
    KIND_WAKE: "wake.fired",
    KIND_RESUME: "resume.fired",
}
DELIVERED_EVENTS = {
    KIND_WAKE: "wake.delivered",
    KIND_RESUME: "resume.delivered",
}
FAILED_EVENTS = {
    KIND_WAKE: "wake.failed",
    KIND_RESUME: "resume.failed",
}
CANCELLED_EVENTS = {
    KIND_WAKE: "wake.cancelled",
    KIND_RESUME: "resume.cancelled",
}
EVENT_TYPES = (
    *SPEC_EVENTS.values(),
    *FIRE_EVENTS.values(),
    *DELIVERED_EVENTS.values(),
    *FAILED_EVENTS.values(),
    *CANCELLED_EVENTS.values(),
)

STATE_SCHEDULED = "scheduled"
STATE_FIRING = "firing"
STATE_DELIVERED = "delivered"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def wake_root(runtime: str | os.PathLike[str] | None = None) -> Path:
    """账本根；显式环境变量优先，默认挂在 daemon 状态根下。"""
    raw = os.environ.get("KG_WAKE_RESUME_ROOT")
    if raw and str(raw).strip():
        return Path(raw).expanduser().resolve()
    base = Path(runtime) if runtime else Path.home() / ".codex-autopilot"
    return base.expanduser().resolve() / "wake-resume"


def worker_root(runtime: str | os.PathLike[str] | None = None) -> Path:
    """worker 生命周期账本根，用于阻止跨工单恢复绕过 lifecycle。"""
    raw = os.environ.get("KG_WORKER_LIFECYCLE_ROOT")
    if raw and str(raw).strip():
        return Path(raw).expanduser().resolve()
    base = Path(runtime) if runtime else Path.home() / ".codex-autopilot"
    return base.expanduser().resolve() / "worker-lifecycle"


def retry_seconds() -> float:
    """失败重试基准退避；测试可设 0，生产默认 5 分钟。"""
    try:
        value = float(os.environ.get("KG_WAKE_RETRY_SECONDS", "300"))
    except (TypeError, ValueError):
        return 300.0
    return max(0.0, value)


def max_attempts() -> int:
    """送达失败上限；超过后停在 failed，不再无限打扰入口。"""
    try:
        value = int(os.environ.get("KG_WAKE_MAX_ATTEMPTS", "3"))
    except (TypeError, ValueError):
        return 3
    return max(1, value)


def normalize_due_at(value: Any) -> float:
    """把 epoch / ISO 时间收敛成 epoch 秒；解析失败 fail-closed。"""
    if isinstance(value, bool) or value is None:
        raise WakeResumeError("due_at 必须是 epoch 秒或 ISO 时间")
    if isinstance(value, (int, float)):
        out = float(value)
    else:
        raw = str(value).strip()
        if not raw:
            raise WakeResumeError("due_at 不能为空")
        try:
            out = float(raw)
        except ValueError:
            text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError as exc:
                raise WakeResumeError(f"due_at 无法解析：{raw!r}") from exc
            if parsed.tzinfo is None:
                parsed = parsed.astimezone()
            out = parsed.timestamp()
    if not math.isfinite(out):
        raise WakeResumeError("due_at 必须是有限时间")
    return out


def _string_list(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        raise WakeResumeError(f"{key} 必须是数组")
    return [str(x) for x in value if str(x).strip()]


def normalize_spec(raw: Any) -> dict[str, Any]:
    """把外部请求收敛成固定形状；timer 只允许 explicit 授权。"""
    if not isinstance(raw, dict):
        raise WakeResumeError("wake/resume spec 必须是对象")

    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in KINDS:
        raise WakeResumeError(f"kind 只能是 {' / '.join(KINDS)}，收到 {kind!r}")

    ticket_id = str(raw.get("ticket_id") or "").strip()
    text = str(raw.get("text") or "").strip()
    reason = str(raw.get("reason") or "").strip()
    if not ticket_id:
        raise WakeResumeError("ticket_id 不能为空")
    if not text:
        raise WakeResumeError("text 不能为空")
    if not reason:
        raise WakeResumeError("必须说明为什么唤醒/恢复，reason 不能为空")

    authorization = str(raw.get("authorization") or "inferred").strip().lower()
    if authorization != "explicit":
        raise AuthorizationError("timer/跨工单恢复只允许 explicit 授权")

    effect = str(raw.get("effect") or "reversible").strip().lower()
    if effect not in ("read", "proposal", "reversible"):
        raise AuthorizationError(f"wake/resume 不接受 effect={effect!r}")

    target = str(raw.get("target") or "inbox").strip().lower()
    if target != "inbox":
        raise WakeResumeError("目前只支持把唤醒送回 inbox")

    resume_from = str(raw.get("resume_from") or "").strip()
    if kind == KIND_RESUME and not resume_from:
        raise WakeResumeError("resume 请求必须声明 resume_from")
    if kind == KIND_WAKE and resume_from:
        raise WakeResumeError("普通 wake 不应声明 resume_from；请用 kind=resume")

    due_at = normalize_due_at(raw.get("due_at"))
    files = _string_list(raw.get("files"), "files")
    lifecycle_worker_id = str(raw.get("lifecycle_worker_id") or "").strip()

    dedupe_payload = json.dumps(
        {
            "kind": kind,
            "ticket_id": ticket_id,
            "due_at": round(due_at, 3),
            "text": text,
            "reason": reason,
            "resume_from": resume_from,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    dedupe_key = hashlib.sha256(dedupe_payload.encode("utf-8")).hexdigest()[:16]
    request_id = str(raw.get("request_id") or f"{kind}-{dedupe_key}").strip()
    if not SAFE_REQUEST_ID.match(request_id):
        raise WakeResumeError("request_id 不合法：只允许字母数字和 _ . : -")

    return {
        "request_id": request_id,
        "dedupe_key": dedupe_key,
        "kind": kind,
        "ticket_id": ticket_id,
        "due_at": due_at,
        "text": text,
        "reason": reason,
        "authorization": authorization,
        "effect": effect,
        "target": target,
        "resume_from": resume_from,
        "project": str(raw.get("project") or "").strip(),
        "files": files,
        "lifecycle_worker_id": lifecycle_worker_id,
        "source": str(raw.get("source") or "user").strip() or "user",
    }


def delivery_text(spec: dict[str, Any]) -> str:
    """给人看的投递文案；marker 同时是幂等键，不能删。"""
    rid = spec["request_id"]
    marker = f"[{spec['kind']}-request:{rid}]"
    if spec["kind"] == KIND_RESUME:
        head = (f"{marker} 继续工单 {spec['ticket_id']}，"
                f"承接 {spec['resume_from']}")
    else:
        head = f"{marker} 定时唤醒工单 {spec['ticket_id']}"
    return f"{head}：{spec['text']}（原因：{spec['reason']}）"


def deliver_to_inbox(
    spec: dict[str, Any],
    current: dict[str, Any],
    runner: Callable[[str, dict[str, Any]], Any] | None = None,
) -> Any:
    """把到期请求送回唯一入口；实际写 inbox 的 runner 由 daemon 注入。

    这一层只负责生成带幂等 marker 的文案和调用 runner。runner 失败会向上抛，
    由 `fire()` 落成 failed + 退避；成功后的 ref 原样记进账本，便于核对送达结果。
    """
    if runner is None:
        raise RuntimeError("inbox runner 未配置；唤醒不能绕过唯一入口直接派发")
    text = delivery_text(spec)
    return runner(text, spec)


def parse_events(raw: str, path: Path) -> list[dict[str, Any]]:
    """JSONL 解析 + seq 连续性；任何坏行都让账本进入 fail-closed。"""
    events: list[dict[str, Any]] = []
    for line_no, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError as exc:
            raise LedgerCorrupt(f"{path}:{line_no} 不是合法 JSON：{exc}") from exc
        if not isinstance(event, dict):
            raise LedgerCorrupt(f"{path}:{line_no} 事件必须是对象")
        if event.get("seq") != line_no:
            raise LedgerCorrupt(
                f"{path}:{line_no} seq 断裂：{event.get('seq')!r}")
        if event.get("type") not in EVENT_TYPES:
            raise LedgerCorrupt(
                f"{path}:{line_no} 未知事件类型：{event.get('type')!r}")
        events.append(event)
    return events


def project(events: list[dict[str, Any]], path: Path | None = None) -> dict[str, Any]:
    """从事件序列投影当前状态；非法状态转移按坏账本处理。"""
    state = "absent"
    spec: dict[str, Any] | None = None
    attempts = 0
    delivery_ref: Any = None
    last_error = ""
    next_retry_at = 0.0
    request_id = ""

    def bad(message: str) -> LedgerCorrupt:
        suffix = f" {path}" if path else ""
        return LedgerCorrupt(f"唤醒账本状态非法：{message}{suffix}")

    for event in events:
        kind_event = event.get("type")
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            raise bad("payload 必须是对象")
        event_rid = str(event.get("request_id") or "")
        if request_id and event_rid != request_id:
            raise bad("同一账本出现不同 request_id")
        if event_rid:
            request_id = event_rid

        if kind_event in SPEC_EVENTS.values():
            if state != "absent" or spec is not None:
                raise bad("重复 scheduled/requested")
            try:
                spec = normalize_spec(payload.get("spec"))
            except WakeResumeError as exc:
                raise bad(f"spec 无法解析：{exc}") from exc
            if request_id and request_id != spec["request_id"]:
                raise bad("事件 request_id 与 spec 不一致")
            request_id = spec["request_id"]
            state = STATE_SCHEDULED
        elif kind_event in FIRE_EVENTS.values():
            if spec is None or state not in (STATE_SCHEDULED, STATE_FAILED):
                raise bad(f"{state} 不能 fired")
            attempt = int(payload.get("attempt") or 0)
            if attempt != attempts + 1:
                raise bad(f"attempt 跳变：{attempts} -> {attempt}")
            attempts = attempt
            state = STATE_FIRING
            last_error = ""
        elif kind_event in DELIVERED_EVENTS.values():
            if state != STATE_FIRING:
                raise bad(f"{state} 不能 delivered")
            delivery_ref = payload.get("ref")
            state = STATE_DELIVERED
        elif kind_event in FAILED_EVENTS.values():
            if state != STATE_FIRING:
                raise bad(f"{state} 不能 failed")
            if int(payload.get("attempt") or 0) != attempts:
                raise bad("failed 的 attempt 与 fired 不一致")
            last_error = str(payload.get("error") or "delivery failed")
            next_retry_at = float(payload.get("next_retry_at") or 0)
            state = STATE_FAILED
        elif kind_event in CANCELLED_EVENTS.values():
            if state not in (STATE_SCHEDULED, STATE_FAILED):
                raise bad(f"{state} 不能 cancelled")
            state = STATE_CANCELLED
        else:  # parse_events 已拦过，这里只做双保险
            raise bad(f"未知事件 {kind_event!r}")

    if spec is None:
        return {
            "request_id": request_id,
            "state": "absent",
            "spec": None,
            "attempts": 0,
            "delivery_ref": None,
            "last_error": "",
            "next_retry_at": 0.0,
            "last_seq": len(events),
            "event_count": len(events),
        }
    return {
        "request_id": spec["request_id"],
        "state": state,
        "spec": spec,
        "attempts": attempts,
        "delivery_ref": delivery_ref,
        "last_error": last_error,
        "next_retry_at": next_retry_at,
        "last_seq": len(events),
        "event_count": len(events),
    }


class WakeResume:
    """append-only 唤醒/恢复账本。"""

    def __init__(self, runtime: str | os.PathLike[str] | None = None):
        self.runtime = (Path(runtime) if runtime else
                        Path.home() / ".codex-autopilot").expanduser().resolve()
        self.root = wake_root(self.runtime)

    def _path(self, request_id: str) -> Path:
        request_id = str(request_id or "").strip()
        if not SAFE_REQUEST_ID.match(request_id):
            raise WakeResumeError("request_id 不合法：只允许字母数字和 _ . : -")
        return self.root / f"{request_id}.jsonl"

    def _read(self, request_id: str) -> tuple[Path, list[dict[str, Any]]]:
        path = self._path(request_id)
        if not path.exists():
            return path, []
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LedgerCorrupt(f"读唤醒账本失败 {path}: {exc}") from exc
        return path, parse_events(raw, path)

    def _mutate(
        self,
        request_id: str,
        make_event: Callable[[dict[str, Any], list[dict[str, Any]], int],
                             tuple[str | None, dict[str, Any] | None]],
        expected_seq: int | None = None,
    ) -> tuple[dict[str, Any], int]:
        path = self._path(request_id)
        self.root.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    events: list[dict[str, Any]] = []
                else:
                    handle.seek(0)
                    events = parse_events(handle.read(), path)
                projection = project(events, path)
                seq = len(events) + 1
                if expected_seq is not None and seq != expected_seq:
                    raise ConflictError(
                        f"{request_id} 账本已被并发修改：期望 seq={expected_seq}，"
                        f"实际 seq={seq}")
                event_type, payload = make_event(projection, events, seq)
                if event_type is None:
                    return projection, len(events)
                if event_type not in EVENT_TYPES:
                    raise WakeResumeError(f"未知事件类型：{event_type!r}")
                candidate = {
                    "seq": seq,
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "request_id": request_id,
                    "type": event_type,
                    "payload": payload or {},
                }
                try:
                    after = project(events + [candidate], path)
                except LedgerCorrupt as exc:
                    raise ConflictError(str(exc)) from exc
                handle.seek(0, os.SEEK_END)
                handle.write(json.dumps(candidate, ensure_ascii=False,
                                        sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                return after, seq
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _check_resume_worker(self, spec: dict[str, Any]) -> None:
        """跨工单恢复不能接管仍在跑、或本可直接 resume 的旧 worker。"""
        if spec["kind"] != KIND_RESUME or not spec["lifecycle_worker_id"]:
            return
        worker_id = spec["lifecycle_worker_id"]
        service = wl.WorkerLifecycle(worker_root(self.runtime))
        state = service.status(worker_id)["state"]
        if state in wl.ACTIVE_STATES | wl.RESUMABLE_STATES:
            raise ConflictError(
                f"旧 worker {worker_id} 仍在 {state}；"
                "请走 worker_lifecycle.resume，不要用跨工单恢复")
        if state == "absent":
            raise ConflictError(
                f"旧 worker {worker_id} 没有生命周期账本，拒绝无审计恢复")

    def schedule(self, raw: dict[str, Any]) -> dict[str, Any]:
        normal = normalize_spec(raw)
        self._check_resume_worker(normal)
        rid = normal["request_id"]

        def make_event(current, _events, _seq):
            if current["state"] != "absent":
                if current.get("spec") == normal:
                    return None, None
                raise ConflictError(
                    f"request_id {rid} 已存在不同的唤醒/恢复请求")
            return SPEC_EVENTS[normal["kind"]], {"spec": normal}

        projection, _ = self._mutate(rid, make_event)
        return projection

    def status(self, request_id: str) -> dict[str, Any]:
        _path, events = self._read(request_id)
        return project(events, _path)

    def cancel(self, request_id: str, reason: str) -> dict[str, Any]:
        reason = str(reason or "").strip()
        if not reason:
            raise WakeResumeError("cancel 必须给出 reason")
        current = self.status(request_id)
        kind = (current.get("spec") or {}).get("kind")
        if kind not in CANCELLED_EVENTS:
            raise WakeResumeError(f"未知请求 kind：{kind!r}")

        def make_event(state, _events, _seq):
            if state["state"] not in (STATE_SCHEDULED, STATE_FAILED):
                return None, None
            return CANCELLED_EVENTS[kind], {"reason": reason}

        projection, _ = self._mutate(request_id, make_event)
        return projection

    def _is_due(self, projection: dict[str, Any], now: float) -> bool:
        spec = projection.get("spec") or {}
        state = projection["state"]
        if state == STATE_SCHEDULED:
            return float(spec.get("due_at") or 0) <= now
        if state == STATE_FAILED:
            return (int(projection.get("attempts") or 0) < max_attempts()
                    and float(projection.get("next_retry_at") or 0) <= now)
        return False

    def due(self, now: float | None = None) -> list[dict[str, Any]]:
        """返回到期请求；任何坏账本都抛出，不能静默当空。"""
        now = time.time() if now is None else float(now)
        self.root.mkdir(parents=True, exist_ok=True)
        out: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.jsonl")):
            try:
                raw = path.read_text(encoding="utf-8")
                events = parse_events(raw, path)
                projection = project(events, path)
            except (OSError, LedgerCorrupt) as exc:
                raise LedgerCorrupt(f"扫描唤醒账本失败：{exc}") from exc
            if projection["state"] == "absent":
                continue
            if self._is_due(projection, now):
                out.append(projection)
        out.sort(key=lambda p: (
            float((p.get("spec") or {}).get("due_at") or 0),
            p["request_id"],
        ))
        return out

    def fire(
        self,
        request_id: str,
        deliverer: Callable[[dict[str, Any], dict[str, Any]], Any],
        now: float | None = None,
    ) -> dict[str, Any]:
        """领到到期请求后投递；投递失败留退避，成功永不重复送达。"""
        now = time.time() if now is None else float(now)
        current = self.status(request_id)
        if not self._is_due(current, now):
            raise ConflictError(
                f"{request_id} 当前 {current['state']} 不可触发（now={now}）")
        spec = current["spec"]
        kind = spec["kind"]
        attempt = int(current.get("attempts") or 0) + 1

        def start_event(_projection, _events, _seq):
            return FIRE_EVENTS[kind], {"attempt": attempt}

        _firing, seq = self._mutate(request_id, start_event)
        try:
            ref = deliverer(spec, current)
        except Exception as exc:  # 投递失败必须落账，不能停在 firing 黑洞
            delay = retry_seconds() * (2 ** max(0, attempt - 1))
            error = f"{type(exc).__name__}: {exc}"

            def failed_event(_projection, _events, _seq):
                return FAILED_EVENTS[kind], {
                    "attempt": attempt,
                    "error": error[:1000],
                    "next_retry_at": now + delay,
                }

            projection, _ = self._mutate(
                request_id, failed_event, expected_seq=seq + 1)
            return projection

        def done_event(_projection, _events, _seq):
            return DELIVERED_EVENTS[kind], {
                "ref": str(ref)[:1000] if ref is not None else "ok",
            }

        projection, _ = self._mutate(
            request_id, done_event, expected_seq=seq + 1)
        return projection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_schedule = sub.add_parser("schedule")
    p_schedule.add_argument("--kind", choices=KINDS, default=KIND_WAKE)
    p_schedule.add_argument("--ticket", required=True)
    p_schedule.add_argument("--due", required=True, help="epoch 秒或 ISO 时间")
    p_schedule.add_argument("--text", required=True)
    p_schedule.add_argument("--reason", required=True)
    p_schedule.add_argument("--resume-from", default="")
    p_schedule.add_argument("--project", default="")
    p_schedule.add_argument("--files", default="", help="冒号分隔的文件线索")
    p_schedule.add_argument("--worker-id", default="", help="旧 lifecycle worker id")
    p_schedule.add_argument("--runtime", default="")

    p_due = sub.add_parser("due")
    p_due.add_argument("--runtime", default="")

    p_status = sub.add_parser("status")
    p_status.add_argument("--request-id", required=True)
    p_status.add_argument("--runtime", default="")

    p_list = sub.add_parser("list")
    p_list.add_argument("--runtime", default="")

    args = parser.parse_args(argv)
    runtime = args.runtime or None
    service = WakeResume(runtime)

    if args.command == "schedule":
        projection = service.schedule({
            "kind": args.kind,
            "ticket_id": args.ticket,
            "due_at": args.due,
            "text": args.text,
            "reason": args.reason,
            "resume_from": args.resume_from,
            "project": args.project,
            "files": [x for x in args.files.split(":") if x],
            "lifecycle_worker_id": args.worker_id,
            "authorization": "explicit",
        })
        print(json.dumps(projection, ensure_ascii=False, indent=1))
        return 0
    if args.command == "due":
        print(json.dumps(service.due(), ensure_ascii=False, indent=1))
        return 0
    if args.command == "status":
        print(json.dumps(service.status(args.request_id), ensure_ascii=False,
                         indent=1))
        return 0
    rows = []
    service.root.mkdir(parents=True, exist_ok=True)
    for path in sorted(service.root.glob("*.jsonl")):
        try:
            rows.append(service.status(path.stem))
        except LedgerCorrupt as exc:
            rows.append({"request_id": path.stem, "state": "unknown",
                         "error": str(exc)})
    print(json.dumps(rows, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WakeResumeError as exc:
        print(f"wake_resume: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
