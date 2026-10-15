#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only Codex native thread snapshot with a safe history DB fallback."""

import json
import os
import sqlite3
import subprocess
from pathlib import Path


CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
SOCKET = Path(os.environ.get("CODEX_APP_SERVER_SOCKET", CODEX_HOME / "ipc" / "ipc.sock"))
HISTORY_DB = Path(os.environ.get("CODEX_THREAD_HISTORY_DB",
                                 CODEX_HOME / "thread_history_1.sqlite"))
CLI = os.environ.get("CODEX_CLI", "codex")
_next_id = 0


def _request_id():
    global _next_id
    _next_id += 1
    return _next_id


def _rpc(method, params):
    """Run one request against the official app-server stdio transport."""
    init_id, request_id = _request_id(), _request_id()
    requests = [
        {"jsonrpc": "2.0", "id": init_id, "method": "initialize",
         "params": {"clientInfo": {"name": "codex-autopilot", "title": "Codex Autopilot",
                                   "version": "0.6.0-preview.1"},
                     "capabilities": {}}},
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
    ]
    payload = "\n".join(json.dumps(item) for item in requests) + "\n"
    try:
        proc = subprocess.run([CLI, "app-server"], input=payload, capture_output=True,
                              text=True, timeout=4)
        if proc.returncode:
            return None, (proc.stderr.strip() or "app-server exit %d" % proc.returncode)
        messages = {}
        for line in proc.stdout.splitlines():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "id" in msg:
                messages[str(msg["id"])] = msg
        init_response = messages.get(str(init_id), {})
        if init_response.get("error"):
            error = init_response["error"]
            return None, str(error.get("message") or error)
        response = messages.get(str(request_id), {})
        if response.get("error"):
            error = response["error"]
            return None, str(error.get("message") or error)
        if "result" not in response:
            return None, "app-server 未返回方法结果"
        return response["result"], None
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)


def _history_snapshot():
    if not HISTORY_DB.is_file():
        return []
    try:
        uri = HISTORY_DB.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=0.5) as db:
            rows = db.execute(
                """SELECT t.thread_id,
                          (SELECT item_json FROM thread_items i
                           WHERE i.thread_id=t.thread_id AND i.item_type='userMessage'
                           ORDER BY i.rollout_ordinal LIMIT 1),
                          MAX(t.completed_at, t.started_at),
                          t.status
                   FROM thread_turns t
                   GROUP BY t.thread_id
                   ORDER BY MAX(t.completed_at, t.started_at) DESC
                   LIMIT 100"""
            ).fetchall()
        result = []
        for thread_id, item_json, updated, status in rows:
            title = ""
            if item_json:
                try:
                    item = json.loads(item_json)
                    title = item.get("text") or item.get("content", "")
                    if isinstance(title, list):
                        title = " ".join(x.get("text", "") for x in title
                                         if isinstance(x, dict))
                    title = str(title).strip().splitlines()[0][:120]
                except (ValueError, TypeError, AttributeError):
                    pass
            result.append({"id": thread_id, "name": title or "Codex 对话",
                           "updatedAt": updated, "status": {"type": status or "idle"},
                           "source": "history-read-only"})
        return result
    except (OSError, sqlite3.Error):
        return []


def snapshot():
    sections, section_error = _rpc("threadSection/list", {"limit": 100})
    threads, thread_error = _rpc(
        "thread/list", {"limit": 100, "sortKey": "updated_at",
                        "sortDirection": "desc", "useStateDbOnly": True})
    if sections is not None and threads is not None:
        return {"available": True, "transport": "codex-app-server-stdio",
                "reason": None, "sections": sections.get("data", []),
                "threads": threads.get("data", [])}
    reason = section_error or thread_error
    return {"available": False, "transport": "history-read-only" if HISTORY_DB.exists()
            else "unavailable", "reason": reason,
            "sections": [], "threads": _history_snapshot()}


def create_section(name):
    if not name:
        return None, "name 不能为空"
    result, error = _rpc("threadSection/create", {"name": name})
    if error:
        return None, error
    return result.get("section"), None


def start_thread(cwd, section_id=None):
    path = Path(cwd).expanduser()
    if not path.is_absolute() or not path.is_dir():
        return None, "cwd 必须是已存在的绝对目录"
    params = {"cwd": str(path.resolve())}
    if section_id:
        params["sectionId"] = section_id
    result, error = _rpc("thread/start", params)
    if error:
        return None, error
    thread = result.get("thread") or {}
    return {"thread_id": thread.get("id"), "cwd": thread.get("cwd"),
            "status": thread.get("status"), "section_id": thread.get("sectionId")}, None


def move_thread(thread_id, section_id=None, before_thread_id=None):
    """Move a host-owned thread through the official section API.

    ``section_id=None`` intentionally means remove from a section.  The
    autopilot never edits the host's SQLite files or fabricates a local folder.
    """
    if not thread_id:
        return None, "thread_id 不能为空"
    params = {"threadId": thread_id, "sectionId": section_id}
    if before_thread_id:
        params["beforeThreadId"] = before_thread_id
    result, error = _rpc("threadSection/move", params)
    if error:
        return None, error
    return result, None
