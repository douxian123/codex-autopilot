#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex native thread bridge with a read-only history fallback."""

import json
import os
import selectors
import sqlite3
import subprocess
import time
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


def _server_command():
    """Prefer the running Codex host; fall back to an isolated app-server."""
    if SOCKET.is_socket():
        return [CLI, "app-server", "proxy", "--sock", str(SOCKET)]
    return [CLI, "app-server"]


def _server_commands(include_isolated=True):
    commands = [_server_command()]
    fallback = [CLI, "app-server"]
    if include_isolated and commands[0] != fallback:
        commands.append(fallback)
    return commands


def _rpc(method, params, host_only=False):
    """Issue one request over a handshaken app-server connection."""
    proc, error = _session(host_only=host_only)
    if error:
        return None, error
    try:
        return _session_request(proc, method, params)
    finally:
        _close_session(proc)


def _read_response(proc, response_id, timeout=8):
    """Read JSONL until a response id arrives, preserving notifications."""
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    messages = []
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            ready = selector.select(max(0, deadline - time.monotonic()))
            if not ready:
                break
            line = proc.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except ValueError:
                continue
            messages.append(message)
            if str(message.get("id")) == str(response_id):
                return message, messages, None
        return None, messages, "app-server 未在限定时间内返回方法结果"
    finally:
        selector.close()


def _open_session(command):
    """Open one official app-server session for dependent requests."""
    try:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
        init_id = _request_id()
        proc.stdin.write(json.dumps({
            "id": init_id, "method": "initialize",
            "params": {"clientInfo": {"name": "codex-autopilot",
                                      "title": "Codex Autopilot",
                                      "version": "0.6.0-preview.1"},
                       "capabilities": {}},
        }) + "\n")
        proc.stdin.flush()
        response, _, error = _read_response(proc, init_id, timeout=30)
        if error:
            _close_session(proc)
            return None, error
        if response.get("error"):
            _close_session(proc)
            return None, str(response["error"].get("message") or response["error"])
        proc.stdin.write(json.dumps({"method": "initialized"}) + "\n")
        proc.stdin.flush()
        return proc, None
    except OSError as exc:
        return None, str(exc)


def _session(host_only=False):
    last_error = None
    for command in _server_commands(include_isolated=not host_only):
        proc, error = _open_session(command)
        if proc is not None:
            return proc, None
        last_error = error
    return None, last_error or "app-server 不可用"


def _session_request(proc, method, params):
    request_id = _request_id()
    try:
        proc.stdin.write(json.dumps({
            "id": request_id, "method": method, "params": params
        }) + "\n")
        proc.stdin.flush()
        response, _, error = _read_response(proc, request_id)
    except (OSError, ValueError) as exc:
        return None, str(exc)
    if error:
        return None, error
    if response.get("error"):
        return None, str(response["error"].get("message") or response["error"])
    return response.get("result"), None


def _close_session(proc):
    try:
        proc.stdin.close()
    except (OSError, ValueError):
        pass
    try:
        proc.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()


def _turn_input(prompt):
    return [{"type": "text", "text": str(prompt)}]


def dispatch_task(cwd, prompt, section_id=None):
    """Create a native thread and start its first turn in the same session."""
    path = Path(cwd).expanduser()
    if not path.is_absolute() or not path.is_dir():
        return None, "cwd 必须是已存在的绝对目录"
    if not str(prompt or "").strip():
        return None, "prompt 不能为空"
    proc, error = _session(host_only=True)
    if error:
        return None, error
    try:
        params = {"cwd": str(path.resolve())}
        started, error = _session_request(proc, "thread/start", params)
        if error:
            return None, error
        thread = (started or {}).get("thread") or {}
        thread_id = thread.get("id")
        if not thread_id:
            return None, "thread/start 未返回 thread.id"
        if section_id:
            _, error = _session_request(proc, "threadSection/move", {
                "threadId": thread_id,
                "sectionId": section_id,
            })
            if error:
                return {
                    "thread_id": thread_id,
                    "cwd": thread.get("cwd") or str(path.resolve()),
                    "section_id": None,
                    "thread": thread,
                }, "thread 已创建，但加入窗口集合失败：" + error
        turn, error = _session_request(proc, "turn/start", {
            "threadId": thread_id,
            "input": _turn_input(prompt),
        })
        if error:
            return {
                "thread_id": thread_id,
                "turn_id": None,
                "cwd": thread.get("cwd") or str(path.resolve()),
                "section_id": thread.get("sectionId") or section_id,
                "thread": thread,
            }, "thread 已创建，但 turn/start 失败：" + error
        turn_data = turn or {}
        return {
            "thread_id": thread_id,
            "turn_id": turn_data.get("turn", {}).get("id") or turn_data.get("turnId"),
            "cwd": thread.get("cwd") or str(path.resolve()),
            "section_id": thread.get("sectionId") or section_id,
            "thread": thread,
            "turn": turn_data,
        }, None
    finally:
        _close_session(proc)


def follow_up(thread_id, prompt):
    """Start another turn on an existing native thread."""
    if not thread_id:
        return None, "thread_id 不能为空"
    if not str(prompt or "").strip():
        return None, "prompt 不能为空"
    proc, error = _session(host_only=True)
    if error:
        return None, error
    try:
        return _session_request(proc, "turn/start", {
            "threadId": thread_id,
            "input": _turn_input(prompt),
        })
    finally:
        _close_session(proc)


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
    result, error = _rpc("threadSection/create", {"name": name}, host_only=True)
    if error:
        return None, error
    return result.get("section"), None


def start_thread(cwd, section_id=None):
    path = Path(cwd).expanduser()
    if not path.is_absolute() or not path.is_dir():
        return None, "cwd 必须是已存在的绝对目录"
    result, error = _rpc("thread/start", {"cwd": str(path.resolve())}, host_only=True)
    if error:
        return None, error
    thread = result.get("thread") or {}
    thread_id = thread.get("id")
    if not thread_id:
        return None, "thread/start 未返回 thread.id"
    if section_id:
        _, error = _rpc("threadSection/move", {
            "threadId": thread_id, "sectionId": section_id,
        }, host_only=True)
        if error:
            return {"thread_id": thread_id, "cwd": thread.get("cwd"),
                    "section_id": None}, "thread 已创建，但加入窗口集合失败：" + error
    return {"thread_id": thread.get("id"), "cwd": thread.get("cwd"),
            "status": thread.get("status"),
            "section_id": section_id or thread.get("sectionId")}, None


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
    result, error = _rpc("threadSection/move", params, host_only=True)
    if error:
        return None, error
    return result, None
