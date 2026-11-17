#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""relationship_ledger —— 跨项目长期关系对象的显式账本（stdlib only）。

它回答的是「关系层」唯一一个此前没有稳定落点的缺口：一个目标跨多个项目存在，
有标题、优先级、状态、涉及项目，以及什么时候必须回到人这里。这个对象不能被
某次对话、某个 worktree 或一条 mainline 记录替代，所以单独用 append-only JSONL
落盘，默认路径 `~/.codex-autopilot/relationships/goals.jsonl`，可用
`KG_RELATIONSHIP_GOALS_PATH` 覆盖。

这里只有显式写入：

  * `source` 必须是 `user` 或 `operator`，推断来源直接拒绝；
  * 本模块不读产品主线、不读工单、不调用 worker、不发消息；
  * 读取层只做投影，不把目标自动派活，也不替用户决定优先级或状态。

账本损坏、seq 断裂、字段不合法都 fail-closed：summary 返回 unknown，写入直接
拒绝。文件不存在与空文件是两种事实：前者是「取不到」，后者才是「还没有显式
登记目标」，绝不把取不到输出成 0 条。
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

CREATED = "goal.created"
UPDATED = "goal.updated"
EVENT_TYPES = (CREATED, UPDATED)

EXPLICIT_SOURCES = ("user", "operator")
PRIORITIES = ("high", "normal", "low")
STATUSES = ("proposed", "active", "waiting_human", "paused", "done")
OPTIONAL_FIELDS = (
    "north_star", "source_thread", "linked_ledger", "linked_acceptance",
    "last_touched", "evidence",
)

PRIORITY_ALIASES = {
    "high": "high", "urgent": "high", "p0": "high", "高": "high",
    "normal": "normal", "medium": "normal", "p1": "normal", "中": "normal",
    "low": "low", "p2": "low", "低": "low",
}
STATUS_ALIASES = {
    "proposed": "proposed", "待确认": "proposed",
    "active": "active", "进行中": "active",
    "waiting_human": "waiting_human", "需要人": "waiting_human",
    "paused": "paused", "暂停": "paused",
    "done": "done", "完成": "done",
}

GOAL_ID_RE = re.compile(r"^G-(\d{4,})$")


class RelationshipError(Exception):
    """关系账本的基类错误。"""


class LedgerCorrupt(RelationshipError):
    """账本内容无法安全投影；必须显式处理，不能静默当空。"""


class ConflictError(RelationshipError):
    """目标 id 重复、更新不存在或字段互相冲突。"""


class AuthorizationError(RelationshipError):
    """写入来源不是显式授权，或写了不该由本层决定的字段。"""


def ledger_path(path: str | os.PathLike | None = None) -> Path:
    override = path if path is not None else os.environ.get("KG_RELATIONSHIP_GOALS_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex-autopilot" / "relationships" / "goals.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def now() -> str:
    """公开时间戳入口；上层命令不直接依赖私有函数。"""
    return _now()


def _normalize_goal(raw: Any, allow_legacy: bool = False) -> dict:
    if not isinstance(raw, dict):
        raise RelationshipError("关系目标必须是对象")

    source = str(raw.get("source") or "").strip()
    if source not in EXPLICIT_SOURCES:
        raise AuthorizationError("关系目标只能由 user/operator 显式写入；推断来源不接受")

    title = str(raw.get("title") or "").strip()
    if not title or len(title) > 200:
        raise RelationshipError("title 必须是 1-200 字")

    priority_raw = str(raw.get("priority") or "").strip().lower()
    priority = PRIORITY_ALIASES.get(priority_raw)
    if priority is None:
        raise RelationshipError("priority 只能是 high/normal/low")

    status_raw = str(raw.get("status") or "").strip().lower()
    status = STATUS_ALIASES.get(status_raw)
    if status is None:
        raise RelationshipError("status 只能是 %s" % "/".join(STATUSES))

    projects_raw = raw.get("projects")
    if not isinstance(projects_raw, (list, tuple)):
        raise RelationshipError("projects 必须是项目名数组")
    projects = []
    for item in projects_raw:
        name = str(item or "").strip()
        if name and name not in projects:
            projects.append(name)
    if not projects:
        raise RelationshipError("projects 至少要有一个项目")

    needs_human = raw.get("needs_human")
    if not isinstance(needs_human, bool):
        raise RelationshipError("needs_human 必须是 true/false")

    reason = str(raw.get("reason") or "").strip()
    if needs_human and not reason:
        raise RelationshipError("needs_human=true 必须说明 reason（需要人的时机与原因）")

    optional: dict[str, Any] = {}
    if not allow_legacy or "north_star" in raw:
        north_star = str(raw.get("north_star") or "").strip()
        if len(north_star) > 300:
            raise RelationshipError("north_star 不能超过 300 字")
        optional["north_star"] = north_star

    if not allow_legacy or "source_thread" in raw:
        source_thread = str(raw.get("source_thread") or "").strip()
        if len(source_thread) > 200:
            raise RelationshipError("source_thread 不能超过 200 字")
        optional["source_thread"] = source_thread

    for key in ("linked_ledger", "linked_acceptance"):
        if allow_legacy and key not in raw:
            continue
        raw_items = raw.get(key) or []
        if not isinstance(raw_items, (list, tuple)):
            raise RelationshipError("%s 必须是数组" % key)
        values = []
        for item in raw_items:
            value = str(item or "").strip()
            if not value:
                continue
            if len(value) > 120:
                raise RelationshipError("%s 单项不能超过 120 字" % key)
            if value not in values:
                values.append(value)
        optional[key] = values

    if not allow_legacy or "last_touched" in raw:
        last_touched = str(raw.get("last_touched") or "").strip()
        if len(last_touched) > 120:
            raise RelationshipError("last_touched 不能超过 120 字")
        optional["last_touched"] = last_touched or _now()

    if not allow_legacy or "evidence" in raw:
        evidence = str(raw.get("evidence") or "").strip()
        if len(evidence) > 500:
            raise RelationshipError("evidence 不能超过 500 字")
        optional["evidence"] = evidence

    goal_id = str(raw.get("id") or "").strip()
    if goal_id:
        if not GOAL_ID_RE.match(goal_id):
            raise RelationshipError("id 必须形如 G-0001")
    else:
        goal_id = None

    return {
        "id": goal_id,
        "title": title,
        "priority": priority,
        "projects": projects,
        "status": status,
        "needs_human": needs_human,
        "reason": reason,
        **optional,
        "source": source,
    }


def _validate_event(row: Any, expected_seq: int, seen: set[str]) -> None:
    if not isinstance(row, dict):
        raise LedgerCorrupt("事件必须是对象")
    event_type = str(row.get("type") or "")
    if event_type not in EVENT_TYPES:
        raise LedgerCorrupt("未知事件类型：%s" % event_type)
    seq = row.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq != expected_seq:
        raise LedgerCorrupt("事件 seq 断裂：期望 %d，实得 %r" % (expected_seq, seq))
    goal_id = str(row.get("id") or "")
    if not GOAL_ID_RE.match(goal_id):
        raise LedgerCorrupt("事件 id 不合法：%r" % goal_id)
    if event_type == CREATED and goal_id in seen:
        raise LedgerCorrupt("重复创建目标 %s" % goal_id)
    if event_type == UPDATED and goal_id not in seen:
        raise LedgerCorrupt("更新了不存在的目标 %s" % goal_id)
    if not str(row.get("updated_at") or "").strip():
        raise LedgerCorrupt("事件缺少 updated_at")
    try:
        normalized = _normalize_goal(row, allow_legacy=True)
    except RelationshipError as exc:
        raise LedgerCorrupt("事件字段不合法：%s" % exc) from exc
    allowed = ({"type", "id", "seq", "ts", "updated_at"}
               | set(normalized) | set(OPTIONAL_FIELDS))
    unexpected = sorted(set(row) - allowed)
    if unexpected:
        raise LedgerCorrupt("事件含未知字段：%s" % "、".join(unexpected))
    if normalized.get("id") != goal_id:
        raise LedgerCorrupt("事件字段与 id 不一致：%s" % goal_id)
    for key, value in normalized.items():
        if key == "id":
            continue
        if row.get(key) != value:
            raise LedgerCorrupt("事件字段 %s 与规范值不一致：%s" % (key, goal_id))


def _parse_events_text(text: str) -> list[dict]:
    events: list[dict] = []
    seen: set[str] = set()
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            raise LedgerCorrupt("第 %d 行是空行" % lineno)
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise LedgerCorrupt("第 %d 行不是 JSON：%s" % (lineno, exc)) from exc
        _validate_event(row, len(events) + 1, seen)
        events.append(row)
        seen.add(str(row["id"]))
    return events


def _read_events(target: Path) -> list[dict]:
    if not target.exists():
        return []
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise LedgerCorrupt("读不到关系目标账本：%s" % exc) from exc
    return _parse_events_text(text)


def project(events: Iterable[dict]) -> list[dict]:
    """把事件流投影成当前目标对象；只做投影，不产生动作。"""
    latest: dict[str, dict] = {}
    for row in events:
        latest[str(row["id"])] = row
    priority_order = {name: idx for idx, name in enumerate(PRIORITIES)}
    status_order = {name: idx for idx, name in enumerate(STATUSES)}
    return sorted(
        latest.values(),
        key=lambda r: (priority_order.get(r.get("priority"), 99),
                       status_order.get(r.get("status"), 99),
                       str(r.get("updated_at") or "")),
    )


def list_goals(path: str | os.PathLike | None = None) -> list[dict]:
    """返回全部当前目标；文件不存在返回空，损坏 fail-closed。"""
    target = ledger_path(path)
    if not target.exists():
        return []
    return project(_read_events(target))


def get(goal_id: str, path: str | os.PathLike | None = None) -> dict:
    """取一条当前目标；不存在或损坏都拒绝操作。"""
    gid = str(goal_id or "").strip()
    if not GOAL_ID_RE.match(gid):
        raise ConflictError("目标 id 不合法：%r" % gid)
    for row in list_goals(path):
        if str(row.get("id")) == gid:
            return row
    raise ConflictError("目标 %s 不存在" % gid)


def fields_for(goal_id: str, path: str | os.PathLike | None = None,
               source: str = "user") -> dict:
    """取当前目标的可写字段，供局部更新使用；source 必须显式传入。"""
    current = get(goal_id, path)
    fields = {}
    for key in ("title", "priority", "status", "reason", "north_star",
                "source_thread", "last_touched", "evidence"):
        fields[key] = current.get(key) or ""
    for key in ("projects", "linked_ledger", "linked_acceptance"):
        fields[key] = list(current.get(key) or [])
    fields["needs_human"] = bool(current.get("needs_human"))
    fields["source"] = source
    return fields


def _append_unique(values, additions) -> list[str]:
    result = []
    for value in list(values or []) + list(additions or []):
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def link(goal_id: str, *, ledger=None, acceptance=None, thread: str = "",
         source: str = "user", path: str | os.PathLike | None = None) -> dict:
    """追加关联，不覆盖已有链接；每一项都显式声明来源。"""
    fields = fields_for(goal_id, path, source)
    if ledger:
        fields["linked_ledger"] = _append_unique(fields["linked_ledger"], ledger)
    if acceptance:
        fields["linked_acceptance"] = _append_unique(fields["linked_acceptance"], acceptance)
    if thread:
        fields["source_thread"] = str(thread).strip()
    fields["last_touched"] = _now()
    return update(goal_id, fields, path)


def close(goal_id: str, conclusion: str, *, source: str = "user",
          evidence: str = "", path: str | os.PathLike | None = None) -> dict:
    """收口必须有理由；理由同时进 reason 和可复核 evidence。"""
    text = str(conclusion or "").strip()
    if not text:
        raise RelationshipError("close 必须写结论（证据或人确认的理由）")
    fields = fields_for(goal_id, path, source)
    prior_reason = str(fields.get("reason") or "").strip()
    fields["reason"] = (prior_reason + "；收口：" + text).strip("；")
    fields["status"] = "done"
    fields["needs_human"] = False
    fields["evidence"] = str(evidence or text).strip()
    fields["last_touched"] = _now()
    return update(goal_id, fields, path)


def _unknown(reason: str) -> dict:
    return {
        "status": "unknown",
        "level": "unknown",
        "headline": "跨项目目标取不到：%s" % reason,
        "note": "取不到不等于 0 条；本层不自动派活，也不替你决定目标。",
        "count": None,
        "needs_human_count": None,
        "items": [],
    }


def summary(path: str | os.PathLike | None = None) -> dict:
    """面板只读摘要：显式登记的目标、需要人的项，以及硬边界。"""
    target = ledger_path(path)
    try:
        if not target.exists():
            return _unknown("goals.jsonl 不存在")
        goals = project(_read_events(target))
    except (OSError, LedgerCorrupt, RelationshipError) as exc:
        return _unknown(str(exc))

    needs_human = [r for r in goals if r.get("needs_human") is True]
    items = [{
        "id": r.get("id"),
        "title": r.get("title"),
        "priority": r.get("priority"),
        "status": r.get("status"),
        "projects": list(r.get("projects") or []),
        "needs_human": bool(r.get("needs_human")),
        "reason": r.get("reason") or "",
        "north_star": r.get("north_star") or "",
        "source_thread": r.get("source_thread") or "",
        "linked_ledger": list(r.get("linked_ledger") or []),
        "linked_acceptance": list(r.get("linked_acceptance") or []),
        "last_touched": r.get("last_touched"),
        "evidence": r.get("evidence") or "",
        "updated_at": r.get("updated_at"),
    } for r in goals[:8]]
    headline = ("跨项目目标 %d 条，其中 %d 条需要人判断"
                % (len(goals), len(needs_human)) if needs_human else
                "跨项目目标 %d 条，暂无需要人判断" % len(goals))
    return {
        "status": "ok",
        "level": "attention" if needs_human else "ok",
        "headline": headline,
        "note": "只记录你显式登记的目标；不会自动派活，也不会替你决定优先级。",
        "count": len(goals),
        "needs_human_count": len(needs_human),
        "items": items,
    }


def _next_goal_id(events: Iterable[dict]) -> str:
    nums = [int(GOAL_ID_RE.match(str(r["id"])).group(1)) for r in events]
    return "G-%04d" % ((max(nums) + 1) if nums else 1)


def _event(gid: str, seq: int, event_type: str, fields: dict, ts: str) -> dict:
    return {
        "type": event_type,
        "id": gid,
        "seq": seq,
        "ts": ts,
        **fields,
        "updated_at": ts,
    }


def create(raw: dict, path: str | os.PathLike | None = None) -> dict:
    """显式登记一个新目标。不传 id 时分配稳定 id。"""
    normalized = _normalize_goal(raw)
    requested_id = normalized.pop("id")
    target = ledger_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        fh.seek(0)
        events = _parse_events_text(fh.read())
        ids = {str(r["id"]) for r in events}
        if requested_id and requested_id in ids:
            raise ConflictError("目标 %s 已存在，请用 update" % requested_id)
        gid = requested_id or _next_goal_id(events)
        ts = _now()
        row = _event(gid, len(events) + 1, CREATED, normalized, ts)
        fh.seek(0, os.SEEK_END)
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return row


def update(goal_id: str, raw: dict, path: str | os.PathLike | None = None) -> dict:
    """显式更新已有目标；整条替换，保留原 id。"""
    gid = str(goal_id or "").strip()
    if not GOAL_ID_RE.match(gid):
        raise ConflictError("要更新的 id 不合法：%r" % gid)
    normalized = _normalize_goal(raw)
    raw_id = normalized.pop("id")
    if raw_id and raw_id != gid:
        raise ConflictError("update 的 id 与内容 id 不一致")
    target = ledger_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        fh.seek(0)
        events = _parse_events_text(fh.read())
        ids = {str(r["id"]) for r in events}
        if gid not in ids:
            raise ConflictError("目标 %s 不存在" % gid)
        ts = _now()
        row = _event(gid, len(events) + 1, UPDATED, normalized, ts)
        fh.seek(0, os.SEEK_END)
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跨项目长期关系目标的显式账本")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_summary = sub.add_parser("summary", help="只读摘要")
    p_summary.add_argument("--path")

    def add_fields(p):
        p.add_argument("--id")
        p.add_argument("--title", required=True)
        p.add_argument("--priority", required=True, help="high/normal/low")
        p.add_argument("--projects", required=True, help="逗号分隔的项目名")
        p.add_argument("--status", required=True,
                       help="proposed/active/waiting_human/paused/done")
        p.add_argument("--needs-human", action="store_true")
        p.add_argument("--reason", default="")
        p.add_argument("--north-star", default="")
        p.add_argument("--source-thread", default="")
        p.add_argument("--linked-ledger", default="",
                       help="逗号分隔的 L-id")
        p.add_argument("--linked-acceptance", default="",
                       help="逗号分隔的 A-id")
        p.add_argument("--last-touched", default="")
        p.add_argument("--evidence", default="", help="收口证据或人确认理由")
        p.add_argument("--source", required=True, help="user 或 operator")
        p.add_argument("--path")

    p_create = sub.add_parser("create", help="显式登记新目标")
    add_fields(p_create)
    p_update = sub.add_parser("update", help="显式更新已有目标")
    add_fields(p_update)
    p_update.add_argument("--goal-id", required=True)

    args = parser.parse_args(argv)
    if args.cmd == "summary":
        result = summary(args.path)
    else:
        raw = {
            "id": args.id,
            "title": args.title,
            "priority": args.priority,
            "projects": [x.strip() for x in args.projects.split(",") if x.strip()],
            "status": args.status,
            "needs_human": bool(args.needs_human),
            "reason": args.reason,
            "north_star": args.north_star,
            "source_thread": args.source_thread,
            "linked_ledger": [x.strip() for x in args.linked_ledger.split(",") if x.strip()],
            "linked_acceptance": [x.strip() for x in args.linked_acceptance.split(",") if x.strip()],
            "last_touched": args.last_touched,
            "evidence": args.evidence,
            "source": args.source,
        }
        try:
            result = (create(raw, args.path) if args.cmd == "create"
                      else update(args.goal_id, raw, args.path))
        except RelationshipError as exc:
            print("关系目标写入被拒绝：%s" % exc)
            return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
