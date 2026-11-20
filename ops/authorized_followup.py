#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""authorized_followup —— 授权后自动追进度的 append-only 授权账本（stdlib only）。

PM 管家只生成追问候选。候选不会、也不能因为「时间久了」被系统自行升级成动作。
只有人在面板上明确点头，才在这里落一条 explicit 授权；守护的 followup pass 只
消费 approved 且尚未 sent 的授权。撤销、失败、取消都追加事件，不覆盖历史。

硬边界：
  * authorization 只能是 explicit；inferred 直接拒绝；
  * effect 只能是 reversible；破坏性动作不在这里接受；
  * 相同 ticket + prompt 的重复授权幂等，不会造成第二次追问；
  * 发送前必须再次确认当前工作项仍活跃且仍有原生 thread 绑定；
  * 这里的函数不调用 Codex 原生接口，真正发送由 daemon 注入 sender。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

OPS = Path(__file__).resolve().parent

STATE_APPROVED = "approved"
STATE_SENT = "sent"
STATE_CANCELLED = "cancelled"
STATE_FAILED = "failed"

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")


class AuthorizationError(ValueError):
    pass


class LedgerCorrupt(RuntimeError):
    pass


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ledger_path(root: Path | None = None) -> Path:
    base = Path(root) if root else Path(os.environ.get("KG_REPO") or OPS.parent)
    return base / "ops" / "truth" / "authorized_followups.jsonl"


def _dedupe_key(ticket_id: str, thread_id: str, prompt: str) -> str:
    payload = json.dumps(
        {"ticket_id": ticket_id, "thread_id": thread_id, "prompt": prompt},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    ticket_id = str(raw.get("ticket_id") or "").strip()
    thread_id = str(raw.get("thread_id") or "").strip()
    prompt = " ".join(str(raw.get("prompt") or "").split())
    reason = " ".join(str(raw.get("reason") or "").strip().split())
    if not ticket_id or not SAFE_ID.match(ticket_id):
        raise AuthorizationError("ticket_id 不合法")
    if not thread_id or not SAFE_ID.match(thread_id):
        raise AuthorizationError("thread_id 不合法；追问必须绑定真实原生窗口")
    if not prompt or len(prompt) > 500:
        raise AuthorizationError("prompt 不能为空且不能超过 500 字")
    if not reason:
        raise AuthorizationError("必须说明为什么授权追问")
    authorization = str(raw.get("authorization") or "").strip().lower()
    if authorization != "explicit":
        raise AuthorizationError("自动追问只允许 explicit 授权，不能 inferred")
    effect = str(raw.get("effect") or "reversible").strip().lower()
    if effect != "reversible":
        raise AuthorizationError("自动追问只接受 reversible 副作用")
    key = _dedupe_key(ticket_id, thread_id, prompt)
    return {
        "request_id": "FU-" + key,
        "dedupe_key": key,
        "ticket_id": ticket_id,
        "thread_id": thread_id,
        "prompt": prompt,
        "reason": reason,
        "authorization": authorization,
        "effect": effect,
        "source": str(raw.get("source") or "user").strip() or "user",
        "ts": _now(),
    }


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise LedgerCorrupt(f"授权账本坏行：{exc}") from exc
        if not isinstance(row, dict):
            raise LedgerCorrupt("授权账本行不是对象")
        rows.append(row)
    return rows


def _append(path: Path, row: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def latest(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        rid = row.get("request_id")
        if rid:
            out[rid] = row
    return out


def approve(raw: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    """逐项显式授权。重复授权返回现有记录，不重复发送。"""
    spec = _normalize(raw)
    path = ledger_path(root)
    current = latest(_read(path)).get(spec["request_id"])
    if current and current.get("state") in (STATE_APPROVED, STATE_SENT):
        return dict(current, idempotent=True)
    if current and current.get("state") == STATE_CANCELLED:
        # 明确撤销后，同一句原话不再复活；避免面板重复提示骚扰用户。
        return dict(current, idempotent=True)
    event = dict(spec)
    event["type"] = "followup.approved"
    event["state"] = STATE_APPROVED
    return _append(path, event)


def cancel(request_id: str, reason: str, root: Path | None = None) -> dict[str, Any]:
    """撤销尚未发送的授权。发送后不能再撤回已经发生的追问。"""
    request_id = str(request_id or "").strip()
    reason = " ".join(str(reason or "").split())
    if not SAFE_ID.match(request_id) or not reason:
        raise AuthorizationError("cancel 需要合法 request_id 和非空 reason")
    path = ledger_path(root)
    current = latest(_read(path)).get(request_id)
    if not current:
        raise AuthorizationError(f"没有这条授权：{request_id}")
    if current.get("state") in (STATE_SENT, STATE_CANCELLED):
        return dict(current, idempotent=True)
    return _append(path, {
        "type": "followup.cancelled", "request_id": request_id,
        "state": STATE_CANCELLED, "reason": reason, "ts": _now(),
    })


def pending(root: Path | None = None) -> list[dict[str, Any]]:
    path = ledger_path(root)
    return [row for row in latest(_read(path)).values()
            if row.get("state") == STATE_APPROVED]


def mark_sent(request_id: str, result: Any, root: Path | None = None) -> dict[str, Any]:
    return _append(ledger_path(root), {
        "type": "followup.sent", "request_id": request_id,
        "state": STATE_SENT, "ts": _now(), "result": result,
    })


def mark_failed(request_id: str, error: str, root: Path | None = None) -> dict[str, Any]:
    return _append(ledger_path(root), {
        "type": "followup.failed", "request_id": request_id,
        "state": STATE_FAILED, "ts": _now(), "error": str(error)[:1000],
    })


def summary(root: Path | None = None) -> dict[str, Any]:
    """面板只读投影：approved 才算待发送；cancel/fail 都说清，不冒充正常。"""
    path = ledger_path(root)
    try:
        rows = list(latest(_read(path)).values())
    except LedgerCorrupt as exc:
        return {"status": "unknown", "items": [],
                "note": f"追问授权账本取不到：{exc}"}
    counts = {state: sum(1 for r in rows if r.get("state") == state)
              for state in (STATE_APPROVED, STATE_SENT, STATE_CANCELLED, STATE_FAILED)}
    return {"status": "ok", "items": rows, "counts": counts,
            "note": "只有你逐项点头的候选才会进入自动追问；可随时撤销未发送项。"}


def deliver_approved(
    sender: Callable[[str, str], Any],
    is_eligible: Callable[[dict[str, Any]], bool] | None = None,
    root: Path | None = None,
    ineligible_reason: str = "发送前资格校验未通过，已撤销未发送授权",
) -> dict[str, Any]:
    """发送已授权项。sender(request_id, prompt) 由 daemon 注入。"""
    sent, failed, skipped = [], [], []
    for spec in pending(root):
        if is_eligible is not None and not is_eligible(spec):
            # 资格消失后不能让它永远停在 approved，否则每拍都重复跳过。
            # 撤销 Append-only 留痕，且只撤未发送项。
            cancel(spec["request_id"], ineligible_reason, root)
            skipped.append(spec["request_id"])
            continue
        try:
            result = sender(spec["request_id"], spec["prompt"])
        except Exception as exc:  # 失败留账，不重复打扰后续项
            mark_failed(spec["request_id"], repr(exc), root)
            failed.append({"id": spec["request_id"], "error": repr(exc)})
            continue
        mark_sent(spec["request_id"], result, root)
        sent.append(spec["request_id"])
    return {"sent": sent, "failed": failed, "skipped": skipped}
