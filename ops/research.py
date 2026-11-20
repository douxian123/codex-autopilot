#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PM 研究账本：记录外部先验，但不替用户伪造研究结论。

研究是项目真相的一部分，却不是第二套任务系统。每条研究请求、证据、候选方案
和状态变化都追加到同一个 ``ops/truth/research.jsonl``；按 research_id 取最后一条
状态，历史记录永远保留。搜索由宿主或人工适配器完成，本模块只负责契约、去重、
证据完整性和可读投影。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


RESEARCH_REL = Path("ops") / "truth" / "research.jsonl"
LOCK_NAME = ".research.lock"
CHOICES = {"adopt", "adapt", "build", "defer", "reject"}
STATUSES = {
    "proposed", "researching", "decision_ready", "awaiting_approval", "approved",
    "rejected", "deferred", "superseded", "blocked", "awaiting_external_research",
}


def _repo(repo=None) -> Path:
    return Path(repo or os.environ.get("KG_REPO") or Path.cwd()).expanduser().resolve()


def path(repo=None) -> Path:
    return _repo(repo) / RESEARCH_REL


def _read(pathname: Path) -> list:
    if not pathname.exists():
        return []
    rows = []
    for line in pathname.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def rows(repo=None) -> list:
    return _read(path(repo))


def latest(repo=None) -> dict:
    """按 research_id 取最新快照；事件历史仍由 rows() 提供。"""
    out = {}
    for row in rows(repo):
        rid = row.get("research_id")
        if rid:
            out[str(rid)] = row
    return out


def _slug(value: str) -> str:
    return " ".join(str(value or "").split()).strip().lower()


def fingerprint(intent_id: str, question: str, scope=None) -> str:
    payload = json.dumps({
        "intent_id": str(intent_id or ""),
        "question": _slug(question),
        "scope": sorted(str(x) for x in (scope or [])),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


@contextlib.contextmanager
def _lock(repo: Path):
    lock = repo / "ops" / "truth" / LOCK_NAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
        yield
        return
    with lock.open("a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _next_id(repo: Path) -> str:
    numbers = []
    for row in _read(path(repo)):
        match = re.match(r"^R-(\d+)$", str(row.get("research_id") or ""))
        if match:
            numbers.append(int(match.group(1)))
    return "R-%04d" % (max(numbers or [0]) + 1)


def _append(repo: Path, row: dict) -> dict:
    target = path(repo)
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "type": "research"}
    record.update(row)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def _find(repo: Path, research_id: str):
    return latest(repo).get(str(research_id))


def create_request(repo=None, *, intent_id, question, scope=None,
                   research_level="L2", risk_level="medium", project_id=None,
                   source="pm") -> dict:
    repo = _repo(repo)
    question = " ".join(str(question or "").split()).strip()
    if not question:
        raise ValueError("question 不能为空")
    scope = [str(item) for item in (scope or []) if str(item).strip()]
    fp = fingerprint(intent_id, question, scope)
    with _lock(repo):
        for row in latest(repo).values():
            if row.get("fingerprint") == fp and row.get("status") not in {
                    "rejected", "deferred", "superseded"}:
                return {"status": "existing", "request": row}
        rid = _next_id(repo)
        return {"status": "created", "request": _append(repo, {
            "research_id": rid,
            "event": "research_request",
            "status": "proposed",
            "intent_id": str(intent_id or ""),
            "project_id": project_id,
            "question": question,
            "scope": scope,
            "research_level": str(research_level or "L2"),
            "risk_level": str(risk_level or "medium"),
            "fingerprint": fp,
            "source": source,
            "evidence_count": 0,
            "option_count": 0,
        })}


def _event(repo, research_id: str, event: str, status=None, **fields) -> dict:
    repo = _repo(repo)
    with _lock(repo):
        current = _find(repo, research_id)
        if current is None:
            raise ValueError("研究请求不存在：%s" % research_id)
        record = dict(current)
        record.update(fields)
        record.update({"research_id": str(research_id), "event": event})
        if status:
            record["status"] = status
        record.pop("type", None)
        return _append(repo, record)


def add_evidence(repo=None, *, research_id, source_url, source_kind="unknown",
                 claims=None, license_status="unknown", maintenance_signal="unknown",
                 relevance="unknown", confidence="unknown", retrieved_at=None,
                 title="", notes="", source="host") -> dict:
    source_url = str(source_url or "").strip()
    if not source_url:
        raise ValueError("source_url 不能为空")
    if not (source_url.startswith(("https://", "http://", "file://", "repo:"))):
        raise ValueError("source_url 必须是 http(s)、file:// 或 repo: 来源")
    repo = _repo(repo)
    current = _find(repo, research_id)
    evidence = list(current.get("evidence") or []) if current else []
    item = {
        "url": source_url,
        "kind": str(source_kind or "unknown"),
        "title": str(title or ""),
        "claims": [str(x) for x in (claims or []) if str(x).strip()],
        "license_status": str(license_status or "unknown"),
        "maintenance_signal": str(maintenance_signal or "unknown"),
        "relevance": str(relevance or "unknown"),
        "confidence": str(confidence or "unknown"),
        "retrieved_at": retrieved_at or time.strftime("%Y-%m-%d"),
        "notes": str(notes or ""),
        "source": source,
    }
    if not any(e.get("url") == source_url for e in evidence if isinstance(e, dict)):
        evidence.append(item)
    return _event(repo, research_id, "research_evidence", status="researching",
                  evidence=evidence, evidence_count=len(evidence))


def add_option(repo=None, *, research_id, name, disposition="adapt", rationale="",
               capabilities=None, gaps=None, license_status="unknown", confidence="unknown",
               selected=False, source="pm") -> dict:
    disposition = str(disposition or "adapt").lower()
    if disposition not in CHOICES:
        raise ValueError("disposition 必须是 %s" % ", ".join(sorted(CHOICES)))
    repo = _repo(repo)
    current = _find(repo, research_id)
    options = list(current.get("options") or []) if current else []
    item = {
        "name": str(name or "").strip(),
        "disposition": disposition,
        "rationale": str(rationale or ""),
        "capabilities": [str(x) for x in (capabilities or []) if str(x).strip()],
        "gaps": [str(x) for x in (gaps or []) if str(x).strip()],
        "license_status": str(license_status or "unknown"),
        "confidence": str(confidence or "unknown"),
        "selected": bool(selected),
        "source": source,
    }
    if not item["name"]:
        raise ValueError("option name 不能为空")
    options = [x for x in options if x.get("name") != item["name"]]
    options.append(item)
    status = "decision_ready" if current and (current.get("evidence") or []) else "researching"
    return _event(repo, research_id, "research_option", status=status,
                  options=options, option_count=len(options))


def update_status(repo=None, *, research_id, status, note="", source="pm") -> dict:
    if status not in STATUSES:
        raise ValueError("不支持研究状态：%s" % status)
    return _event(repo, research_id, "status", status=status, note=str(note or ""), source=source)


def summary(repo=None, research_id=None) -> dict:
    """面板和 PM 读取的低噪声投影；研究文件缺失等同于还没有研究任务。"""
    repo = _repo(repo)
    if research_id:
        item = _find(repo, research_id)
        if not item:
            return {"status": "unknown", "research_id": research_id,
                    "reason": "研究请求不存在：%s" % research_id, "items": []}
        return {"status": "ok", "research_id": research_id, "item": item, "items": [item]}
    items = list(latest(repo).values())
    terminal = {"approved", "rejected", "deferred", "superseded"}
    active = [x for x in items if x.get("status") not in terminal]
    attention = [x for x in active if x.get("status") in {
        "awaiting_approval", "blocked", "awaiting_external_research"}]
    return {
        "status": "attention" if attention else "ok",
        "count": len(items),
        "active_count": len(active),
        "attention_count": len(attention),
        # 面板需要知道当前 PM 判断，即使它已经批准；attention_count 仍只统计未收口事项，
        # 因此把最近的已完成判断带出来不会把健康状态误报成 attention。
        "items": [{
            "research_id": x.get("research_id"),
            "intent_id": x.get("intent_id"),
            "question": x.get("question"),
            "status": x.get("status"),
            "research_level": x.get("research_level"),
            "risk_level": x.get("risk_level"),
            "evidence_count": len(x.get("evidence") or []),
            "option_count": len(x.get("options") or []),
        } for x in items[-8:]],
        "note": "研究结果由宿主或人工适配器写回；没有证据时不会假装没有成熟方案。",
    }
