#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目级自主运行状态与持久任务队列。

状态放在 ``KG_DAEMON_HOME`` 下，而不是项目工作区：面板操作不会制造 Git
改动，多个项目也不会共用一个账本。这个模块只负责授权、队列和租约，代码
执行、验证和合并仍由 central_daemon 负责。
"""

import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

MODES = ("disabled", "enabled", "running", "paused", "waiting_human")
DEFAULT_MODE = "enabled"
LEASE_SECONDS = 30 * 60


def _runtime() -> Path:
    return Path(os.environ.get(
        "KG_DAEMON_HOME", str(Path.home() / ".codex-autopilot"))).expanduser()


def project_key(repo) -> str:
    """Use the real project path as a stable, non-secret namespace key."""
    path = str(Path(repo).expanduser().resolve())
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:24]


def _dir(repo) -> Path:
    return _runtime() / "projects" / project_key(repo)


def state_path(repo) -> Path:
    return _dir(repo) / "autonomy.json"


def queue_path(repo) -> Path:
    return _dir(repo) / "queue.json"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read(path: Path, fallback):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, type(fallback)) else fallback
    except (OSError, ValueError):
        return fallback


def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def load(repo) -> dict:
    """Return project autonomy state; an absent file means explicit setup not done."""
    data = _read(state_path(repo), {})
    mode = data.get("mode") if data.get("mode") in MODES else DEFAULT_MODE
    return {
        "project": str(Path(repo).expanduser().resolve()),
        "project_key": project_key(repo),
        "mode": mode,
        "configured": state_path(repo).exists(),
        "updated_at": data.get("updated_at"),
        "reason": data.get("reason") or "",
        "changed_by": data.get("changed_by") or "panel",
    }


def set_mode(repo, mode: str, reason: str = "", changed_by: str = "panel") -> dict:
    if mode not in MODES:
        raise ValueError("unknown project autonomy mode: %s" % mode)
    data = {
        "mode": mode,
        "reason": reason.strip(),
        "changed_by": changed_by,
        "updated_at": _now(),
    }
    _atomic_write(state_path(repo), data)
    return load(repo)


def should_dispatch(repo, legacy_enabled: bool = False) -> bool:
    """Project mode is authoritative once configured; legacy projects keep working."""
    state = load(repo)
    if state["configured"]:
        return state["mode"] == "running"
    return bool(legacy_enabled)


def _queue(repo) -> dict:
    data = _read(queue_path(repo), {})
    tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else []
    return {"version": 1, "updated_at": data.get("updated_at"), "tasks": tasks}


def _save_queue(repo, data: dict) -> None:
    data["updated_at"] = _now()
    _atomic_write(queue_path(repo), data)


def sync_tasks(repo, items) -> dict:
    """Upsert open ledger items without resetting leases or execution history."""
    data = _queue(repo)
    by_id = {str(t.get("id")): t for t in data["tasks"] if t.get("id")}
    now = _now()
    for item in items or []:
        rid = str(item.get("id") or "").strip()
        if not rid:
            continue
        old = by_id.get(rid, {})
        task = dict(old)
        task.update({
            "id": rid,
            "title": item.get("title") or old.get("title") or rid,
            "kind": item.get("kind") or old.get("kind") or "unknown",
            "status": old.get("status") if old.get("status") in (
                "running", "waiting_human", "blocked") else "queued",
            "updated_at": now,
        })
        by_id[rid] = task
    open_ids = {str(i.get("id")) for i in items or [] if i.get("id")}
    for task in by_id.values():
        if task.get("id") not in open_ids and task.get("status") not in (
                "done", "cancelled"):
            task["status"] = "done"
            task["lease_until"] = None
            task["updated_at"] = now
    data["tasks"] = sorted(by_id.values(), key=lambda t: (t.get("status") != "queued",
                                                            t.get("id", "")))
    _save_queue(repo, data)
    return data


def tasks(repo) -> list:
    return _queue(repo)["tasks"]


def claim(repo, task_id: str, worker: str, branch: str = "", worktree: str = "") -> dict:
    data = _queue(repo)
    now = time.time()
    found = None
    for task in data["tasks"]:
        if str(task.get("id")) != str(task_id):
            continue
        lease_until = float(task.get("lease_until_epoch") or 0)
        if task.get("status") == "running" and lease_until > now and task.get("worker") != worker:
            raise RuntimeError("task lease is held by %s" % task.get("worker"))
        task.update({
            "status": "running",
            "worker": worker,
            "branch": branch,
            "worktree": worktree,
            "lease_until_epoch": now + LEASE_SECONDS,
            "lease_until": datetime.fromtimestamp(now + LEASE_SECONDS,
                                                   timezone.utc).astimezone().isoformat(timespec="seconds"),
            "attempts": int(task.get("attempts") or 0) + 1,
            "updated_at": _now(),
        })
        found = task
        break
    if found is None:
        raise KeyError(task_id)
    _save_queue(repo, data)
    return dict(found)


def heartbeat(repo, task_id: str, worker: str) -> bool:
    data = _queue(repo)
    changed = False
    now = time.time()
    for task in data["tasks"]:
        if str(task.get("id")) == str(task_id) and task.get("worker") == worker:
            task["lease_until_epoch"] = now + LEASE_SECONDS
            task["lease_until"] = datetime.fromtimestamp(now + LEASE_SECONDS,
                                                          timezone.utc).astimezone().isoformat(timespec="seconds")
            task["updated_at"] = _now()
            changed = True
            break
    if changed:
        _save_queue(repo, data)
    return changed


def finish(repo, task_id: str, status: str = "done", error: str = "") -> bool:
    if status not in ("done", "blocked", "waiting_human", "queued"):
        raise ValueError("invalid task status: %s" % status)
    data = _queue(repo)
    changed = False
    for task in data["tasks"]:
        if str(task.get("id")) == str(task_id):
            task.update({"status": status, "last_error": error,
                         "lease_until": None, "lease_until_epoch": 0,
                         "updated_at": _now()})
            changed = True
            break
    if changed:
        _save_queue(repo, data)
    return changed


def summary(repo) -> dict:
    state = load(repo)
    rows = tasks(repo)
    counts = {key: 0 for key in ("queued", "running", "blocked", "waiting_human", "done")}
    now = time.time()
    expired = 0
    for task in rows:
        status = task.get("status") if task.get("status") in counts else "queued"
        counts[status] += 1
        if status == "running" and float(task.get("lease_until_epoch") or 0) <= now:
            expired += 1
    return {**state, "counts": counts, "expired_leases": expired,
            "total": len(rows), "queue_path": str(queue_path(repo))}
