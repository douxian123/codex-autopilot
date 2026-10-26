#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex native thread bridge with a read-only history fallback."""

import json
import base64
import hashlib
import os
import selectors
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path


CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
SOCKET = Path(os.environ.get("CODEX_APP_SERVER_SOCKET", CODEX_HOME / "app-server-control" / "app-server-control.sock"))
HISTORY_DB = Path(os.environ.get("CODEX_THREAD_HISTORY_DB",
                                 CODEX_HOME / "thread_history_1.sqlite"))
CLI = os.environ.get("CODEX_CLI", "codex")
# 子对话默认归进这个窗口集合：自动挡派人干活时统一开在这里，用户不必自己整理。
DEFAULT_SECTION_NAME = os.environ.get("CODEX_AUTOPILOT_SECTION", "Codex 自动挡")
# 查宿主会话/窗口集合的时间预算。桌面宿主「连得上但不回话」时，
# 控制面（面板开关、Companion、MCP status）绝不能被它拖住。
SNAPSHOT_TIMEOUT = float(os.environ.get("CODEX_AUTOPILOT_NATIVE_TIMEOUT", "6"))
_next_id = 0

# 历史兜底（慢路降级时用）：3.4GB 的历史库不能每拍都扫。
# 缓存 2 秒，兼顾「面板别被拖住」和「历史变化能及时出现」。
_HISTORY_CACHE = {"ts": 0.0, "rows": []}
_HISTORY_CACHE_TTL = float(os.environ.get("CODEX_AUTOPILOT_HISTORY_CACHE_TTL", "2"))
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _recv_exact(sock, size, deadline=None):
    chunks = bytearray()
    while len(chunks) < size:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("app-server WebSocket 未在限定时间内返回")
            sock.settimeout(min(5.0, remaining))
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise ConnectionError("app-server WebSocket 连接中断")
        chunks.extend(chunk)
    return bytes(chunks)


def _ws_send_frame(ws, payload):
    body = payload.encode("utf-8") if isinstance(payload, str) else payload
    mask = os.urandom(4)
    header = bytearray([0x81])
    length = len(body)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.extend([0x80 | 126])
        header.extend(struct.pack(">H", length))
    else:
        header.extend([0x80 | 127])
        header.extend(struct.pack(">Q", length))
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(body))
    ws.sock.sendall(bytes(header) + mask + masked)


def _ws_recv_frame(ws):
    deadline = time.monotonic() + ws.timeout
    first, second = _recv_exact(ws.sock, 2, deadline)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_exact(ws.sock, 2, deadline))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(ws.sock, 8, deadline))[0]
    mask = _recv_exact(ws.sock, 4, deadline) if masked else None
    payload = _recv_exact(ws.sock, length, deadline)
    if mask:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    if opcode == 0x8:
        raise ConnectionError("app-server WebSocket 已关闭")
    if opcode == 0x9:
        _ws_send_frame(ws, b"")
        return None
    if opcode in (0x1, 0x0):
        return payload.decode("utf-8")
    return None


class _WsSession:
    kind = "ws"

    def __init__(self, sock, timeout=30):
        self.sock = sock
        self.timeout = max(1.0, float(timeout))
        self.stderr = None

    def send(self, payload):
        _ws_send_frame(self, json.dumps(payload, ensure_ascii=False))

    def receive(self):
        deadline = time.monotonic() + self.timeout
        while True:
            self.sock.settimeout(max(0.1, deadline - time.monotonic()))
            text = _ws_recv_frame(self)
            if text is None:
                continue
            return json.loads(text)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _open_ws_session(socket_path, timeout=30):
    """Open Codex's local app-server control socket over its WebSocket protocol."""
    deadline = time.monotonic() + max(1.0, float(timeout))
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(min(5.0, deadline - time.monotonic()))
    try:
        sock.connect(str(socket_path))
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % key
        )
        sock.sendall(request.encode("ascii"))
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("app-server WebSocket 握手被关闭")
            response.extend(chunk)
        text = bytes(response).decode("utf-8", "replace")
        headers = {}
        lines = text.splitlines()
        if not lines or " 101 " not in lines[0]:
            raise ConnectionError("app-server WebSocket 握手失败：" + lines[0] if lines else "空响应")
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1(
            (key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise ConnectionError("app-server WebSocket 握手校验失败")
        ws = _WsSession(sock, timeout=max(1.0, deadline - time.monotonic()))
        init_id = _request_id()
        ws.send({
            "id": init_id,
            "method": "initialize",
            "params": {"clientInfo": {"name": "codex-autopilot",
                                      "title": "Codex Autopilot",
                                      "version": _plugin_version()},
                       "capabilities": {}},
        })
        message = ws.receive()
        if str(message.get("id")) != str(init_id):
            raise ConnectionError("app-server WebSocket initialize 响应缺失")
        if message.get("error"):
            raise ConnectionError(str(message["error"].get("message") or message["error"]))
        ws.send({"method": "initialized"})
        return ws, None
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError, ConnectionError) as exc:
        sock.close()
        return None, str(exc)


def _plugin_version() -> str:
    """客户端协议版本与插件清单一致，避免版本号散落多处后漂移。"""
    base = Path(__file__).resolve().parent
    for candidate in (base.parent / "plugin" / ".codex-plugin" / "plugin.json",
                      base.parent / ".codex-plugin" / "plugin.json"):
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))["version"]
        except (OSError, ValueError, KeyError):
            continue
    return "0.0.0"


def _request_id():
    global _next_id
    _next_id += 1
    return _next_id


def _socket_present():
    """路径入口存在且看起来是控制 socket，不要求能 stat 到目标。

    macOS 受限进程里常见 `~/.codex/app-server-control/...sock` 是指向
    /private/tmp 的 symlink。`Path.is_socket()` 会沿目标 stat，被系统权限
    挡住时返回 False，于是把存在且实际在运行的 socket 误报成「没有找到」。
    这里只看 lstat 的入口类型，把“路径在不在”和“能不能连”分开报告；
    具体访问失败由 probe 的原始错误继续诊断。
    """
    try:
        st = SOCKET.lstat()
    except OSError:
        return False
    return stat.S_ISSOCK(st.st_mode) or stat.S_ISLNK(st.st_mode)


def _server_command():
    """Prefer the running Codex host; fall back to an isolated app-server."""
    if _socket_present():
        return [CLI, "app-server", "proxy", "--sock", str(SOCKET)]
    return [CLI, "app-server"]


def _server_commands(include_isolated=True):
    commands = [_server_command()]
    fallback = [CLI, "app-server"]
    if include_isolated and commands[0] != fallback:
        commands.append(fallback)
    return commands


def _rpc(method, params, host_only=False, timeout=30):
    """Issue one request over a handshaken app-server connection.

    timeout 是这一步的总预算：握手与发请求共享同一个 deadline，
    否则「连得上但不回话」会变成两段各自等满。
    """
    deadline = time.monotonic() + max(MIN_SLICE, float(timeout))
    proc, error = _session(host_only=host_only, timeout=_remaining(deadline))
    if error:
        return None, error
    try:
        return _session_request(proc, method, params,
                                timeout=_remaining(deadline))
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
                # 子进程断了。它写过的 stderr 才是「为什么没通」的原文，
                # 拿回来贴进原因里，免得把「连不上」误报成「协议超时」。
                # 但 drain 也必须吃掉自己的预算：它原来固定 1.0s，一段 RPC 就多等
                # 1 秒，snapshot 的 1 秒预算实际变成 2~3 秒。
                detail = _drain_stderr(proc, timeout=_remaining(deadline, 0.0))
                if detail:
                    return None, messages, f"app-server 连接中断：{detail}"
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


def _drain_stderr(proc, timeout=1.0, limit=800):
    """取回子进程 stderr 里已有的内容（只取现有，不阻塞等它退出）。"""
    chunks = []
    selector = selectors.DefaultSelector()
    try:
        selector.register(proc.stderr, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        total = 0
        while time.monotonic() < deadline:
            if not selector.select(max(0, deadline - time.monotonic())):
                break
            line = proc.stderr.readline()
            if not line:
                break
            chunks.append(line)
            total += len(line)
            if total > limit:
                break
    except (OSError, ValueError):
        pass
    finally:
        selector.close()
    return "".join(chunks).strip()[-limit:]


def _chatgpt_pairing_hint(reason=""):
    """只读检测官方 remote-control 的 ChatGPT 登录/配对硬闸，给出可执行的提示。"""
    if not reason:
        return None
    hints = []
    if "remote control requires ChatGPT authentication" in reason:
        hints.append("官方 remote-control 要求 ChatGPT 登录；当前 CLI 是 API key，不能用于配对。")
        hints.append("若保留 API key：请接受原生派活不可达，继续走只读历史 + 本地 worktree 主链路。")
        hints.append("若接受切换登录：先备份并确认当前 config/provider，再执行 `codex login --device-auth`，"
                     "完成 ChatGPT 登录后执行 `codex remote-control start` 与 `codex remote-control pair --json`。")
        return "\n".join(hints)
    if "enrollment" in reason:
        hints.append("remote-control enrollment/配对尚未完成。")
        hints.append("先确认当前 CLI 是 ChatGPT 登录而不是 API key：`codex login status`；"
                     "再用 `codex remote-control start --json` 与 `codex remote-control pair --json` 完成配对。")
        return "\n".join(hints)
    return None


def _remote_control_requires_chatgpt():
    """读 daemon 日志，判断是否卡在官方 remote-control 的 ChatGPT 登录/配对硬闸。"""
    candidates = [
        CODEX_HOME / "app-server-daemon" / "daemon.stderr.log",
        CODEX_HOME / "app-server" / "daemon.stderr.log",
        CODEX_HOME / "app-server-daemon.log",
        CODEX_HOME / "app-server" / "daemon.log",
    ]
    for path in candidates:
        try:
            if not path.is_file():
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in reversed(lines):
                if "remote control requires ChatGPT authentication; API key auth is not supported" in line:
                    return True
            return False
        except OSError:
            continue
    return False


def _open_session(command, timeout=30):
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
                                      "version": _plugin_version()},
                       "capabilities": {}},
        }) + "\n")
        proc.stdin.flush()
        response, _, error = _read_response(proc, init_id, timeout=timeout)
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


MIN_SLICE = 0.05


def _remaining(deadline: float, floor: float = MIN_SLICE) -> float:
    """距 deadline 还剩多少秒（下限 floor，而不是抬回 1 秒整）。

    旧写法 `max(1.0, deadline - now)` 有个隐蔽后果：剩余预算变成负数时它把每一段
    重新抬回 1.0 秒，于是「总预算 1 秒」的 snapshot 会被拆成 N 段各等 1 秒 ——
    2026-10-27 selftest 实测 1 秒预算等了 4.9s，把面板快路拖成慢路。
    """
    return max(floor, deadline - time.monotonic())


def _session(host_only=False, timeout=30):
    """在同一个预算内逐个试候选服务，不按候选数把等待翻倍。"""
    deadline = time.monotonic() + max(MIN_SLICE, float(timeout))
    last_error = None
    if _socket_present():
        remaining = deadline - time.monotonic()
        ws, error = _open_ws_session(SOCKET, timeout=_remaining(deadline))
        if ws is not None:
            return ws, None
        last_error = error
    for command in _server_commands(include_isolated=not host_only):
        remaining = deadline - time.monotonic()
        if remaining <= 0.2:
            last_error = last_error or "app-server 没有在限定时间内回应"
            break
        proc, error = _open_session(command, timeout=remaining)
        if proc is not None:
            return proc, None
        last_error = error
    return None, last_error or "app-server 不可用"


def _session_request(proc, method, params, timeout=8):
    request_id = _request_id()
    if getattr(proc, "kind", None) == "ws":
        deadline = time.monotonic() + max(1.0, float(timeout))
        try:
            proc.send({
                "id": request_id,
                "method": method,
                "params": params,
            })
            while True:
                proc.sock.settimeout(max(0.1, deadline - time.monotonic()))
                message = proc.receive()
                if str(message.get("id")) == str(request_id):
                    if message.get("error"):
                        return None, str(message["error"].get("message") or message["error"])
                    return message.get("result"), None
        except (OSError, TimeoutError, ValueError, json.JSONDecodeError, ConnectionError) as exc:
            return None, str(exc)
    try:
        proc.stdin.write(json.dumps({
            "id": request_id, "method": method, "params": params
        }) + "\n")
        proc.stdin.flush()
        response, _, error = _read_response(proc, request_id, timeout=timeout)
    except (OSError, ValueError) as exc:
        return None, str(exc)
    if error:
        return None, error
    if response.get("error"):
        return None, str(response["error"].get("message") or response["error"])
    return response.get("result"), None


def _close_session(proc):
    if getattr(proc, "kind", None) == "ws":
        proc.close()
        return
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


def _thread_section_id(thread):
    """会话归属的窗口集合：官方协议是嵌套 section.id，兼容早期扁平 sectionId。"""
    thread = thread or {}
    nested = thread.get("section") or {}
    return nested.get("id") or thread.get("sectionId") or None


def _resolve_section_id(proc, name):
    """在同一次 app-server 会话里把集合名解析成 id；找不到时创建。"""
    name = str(name or "").strip()
    if not name:
        return None, None
    listed, error = _session_request(proc, "threadSection/list", {"limit": 100})
    if error:
        return None, error
    sections = (listed or {}).get("data") or []
    for section in sections:
        if str((section or {}).get("name") or "").strip() == name:
            return section.get("id"), None
    created, error = _session_request(proc, "threadSection/create", {"name": name})
    if error:
        return None, error
    return ((created or {}).get("section") or {}).get("id"), None


def dispatch_task(cwd, prompt, section_id=None, auto_section=None, allow_isolated=False):
    """Create a native thread and start its first turn in the same session.

    Default is desktop-host only so callers do not accidentally fabricate a
    desktop-window result. Pass allow_isolated=True when the caller explicitly
    accepts an independent app-server session.
    """
    path = Path(cwd).expanduser()
    if not path.is_absolute() or not path.is_dir():
        return None, "cwd 必须是已存在的绝对目录"
    if not str(prompt or "").strip():
        return None, "prompt 不能为空"
    section_note = None
    proc, error = _session(host_only=not allow_isolated)
    if error:
        return None, error
    try:
        if not section_id and auto_section:
            section_id, section_error = _resolve_section_id(proc, auto_section)
            if section_error:
                section_note = "没能建/找到窗口集合「%s」：%s" % (
                    auto_section, section_error)
        params = {"cwd": str(path.resolve())}
        started, error = _session_request(proc, "thread/start", params)
        if error:
            return None, error
        thread = (started or {}).get("thread") or {}
        thread_id = thread.get("id")
        if not thread_id:
            return None, "thread/start 未返回 thread.id"
        if section_id:
            _, error = _session_request(proc, "thread/section/move", {
                "threadId": thread_id,
                "sectionId": section_id,
            })
            if error:
                return {
                    "thread_id": thread_id,
                    "cwd": thread.get("cwd") or str(path.resolve()),
                    "section_id": None,
                    "section_note": section_note,
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
                "section_id": _thread_section_id(thread) or section_id,
                "section_note": section_note,
                "thread": thread,
            }, "thread 已创建，但 turn/start 失败：" + error
        turn_data = turn or {}
        return {
            "thread_id": thread_id,
            "turn_id": turn_data.get("turn", {}).get("id") or turn_data.get("turnId"),
            "cwd": thread.get("cwd") or str(path.resolve()),
            "section_id": _thread_section_id(thread) or section_id,
            "section_note": section_note,
            "thread": thread,
            "turn": turn_data,
        }, None
    finally:
        _close_session(proc)


def follow_up(thread_id, prompt, allow_isolated=False):
    """Start another turn on an existing native thread.

    allow_isolated=True follows the same explicit opt-in as dispatch_task;
    the default remains desktop-host only.
    """
    if not thread_id:
        return None, "thread_id 不能为空"
    if not str(prompt or "").strip():
        return None, "prompt 不能为空"
    proc, error = _session(host_only=not allow_isolated)
    if error:
        return None, error
    try:
        resumed, resume_error = _session_request(proc, "thread/resume", {
            "threadId": thread_id,
        })
        if resume_error and "thread not found" not in resume_error.lower():
            return None, "thread/resume 失败：" + resume_error
        return _session_request(proc, "turn/start", {
            "threadId": thread_id,
            "input": _turn_input(prompt),
        })
    finally:
        _close_session(proc)


# 历史兜底（慢路降级时用）：3.4GB 的历史库不能每拍都扫。
# 缓存 2 秒，兼顾「面板别被拖住」和「历史变化能及时出现」。
_HISTORY_CACHE = {"ts": 0.0, "rows": []}
_HISTORY_CACHE_TTL = float(os.environ.get("CODEX_AUTOPILOT_HISTORY_CACHE_TTL", "2"))


def _history_snapshot(budget=None):
    """只读历史快照，短 TTL 缓存让慢路降级不反复扫整个 SQLite。

    budget 是这个兜底查询的**硬预算**（秒）。必须给：实测这台机器的
    ~/.codex/thread_history_1.sqlite 是 5.0GB / 3.2 万 turn / 111 万 item，
    一条 GROUP BY 全表要 3.2 秒。旧实现不设预算，于是「snapshot(timeout=1)」
    里真正的 4 秒全花在这条兜底 SQL 上，面板快路被拖成慢路
    （2026-10-27 selftest：1 秒预算等了 4.9s）。
    超预算就如实返回上一次的缓存（或空），绝不把调用方按住。
    """
    now = time.monotonic()
    if now - _HISTORY_CACHE["ts"] < _HISTORY_CACHE_TTL:
        return _HISTORY_CACHE["rows"]
    if not HISTORY_DB.is_file():
        return []
    if budget is not None and float(budget) <= 0:
        return _HISTORY_CACHE["rows"]
    if budget is not None:
        # 放到独立线程里跑，到点立刻走人。查询本身没法中途取消（SQLite 不支持），
        # 但调用方不用陪它跑完；线程是 daemon，进程退出不会被它拖住。
        box = {}
        def _work():
            try:
                box["rows"] = _history_snapshot_uncached()
            except BaseException as exc:  # noqa: BLE001 - 兜底查询失败一律降级
                box["error"] = exc
        th = threading.Thread(target=_work, daemon=True)
        th.start()
        th.join(max(MIN_SLICE, float(budget)))
        if th.is_alive() or "rows" not in box:
            # 没在预算内跑完：把 ts 也推进，避免下一个调用立刻再来一次同样的等待。
            _HISTORY_CACHE.update(ts=time.monotonic(), rows=_HISTORY_CACHE["rows"])
            return _HISTORY_CACHE["rows"]
        _HISTORY_CACHE.update(ts=time.monotonic(), rows=box["rows"])
        return box["rows"]
    return _history_snapshot_uncached()


def _history_snapshot_uncached():
    """真正扫 SQLite 的那一段（无预算，只能由带预算的壳调用）。"""
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


def snapshot(timeout=None):
    """读宿主里的窗口集合与会话；连不上就按时降级，绝不把调用方拖住。

    timeout=0 表示这次只读缓存状态、压根不连宿主——面板/开关那条快路用它，
    免得一个不回话的宿主把「点一下开关」变成几秒钟的等待。
    """
    if timeout is not None and float(timeout) <= 0:
        return {"available": False, "transport": "not-queried",
                "reason": "本次只读缓存状态，没有连宿主",
                "sections": [], "threads": []}
    deadline = time.monotonic() + max(
        1.0, float(SNAPSHOT_TIMEOUT if timeout is None else timeout))
    sections, section_error = _rpc("threadSection/list", {"limit": 100},
                                   timeout=_remaining(deadline))
    threads, thread_error = _rpc(
        "thread/list", {"limit": 100, "sortKey": "updated_at",
                        "sortDirection": "desc", "useStateDbOnly": True},
        timeout=_remaining(deadline))
    if sections is not None and threads is not None:
        return {"available": True,
                "transport": "codex-app-server-websocket" if _socket_present()
                else "codex-app-server-stdio",
                "reason": None, "sections": sections.get("data", []),
                "threads": threads.get("data", [])}
    reason = section_error or thread_error
    return {"available": False, "transport": "history-read-only" if HISTORY_DB.exists()
            else "unavailable", "reason": reason,
            "sections": [],
            "threads": _history_snapshot(budget=max(MIN_SLICE, deadline - time.monotonic()))}


def create_section(name):
    if not name:
        return None, "name 不能为空"
    result, error = _rpc("threadSection/create", {"name": name}, host_only=True)
    if error:
        return None, error
    return result.get("section"), None


def list_sections():
    """列出宿主里现有的窗口集合；只在真实桌面宿主上查。"""
    result, error = _rpc("threadSection/list", {"limit": 100}, host_only=True)
    if error:
        return None, error
    return (result or {}).get("data") or [], None


def ensure_section(name):
    """按名字复用窗口集合，只有不存在时才建；绝不出现两个同名集合。

    用户要的是「窗口拉太多时自动给它们一个文件夹」，不是每次派活都新开一个。
    查/建都走宿主，失败时如实返回原因，由调用方决定是否降级。
    """
    name = str(name or "").strip()
    if not name:
        return None, "name 不能为空"
    sections, error = list_sections()
    if error:
        return None, error
    for section in sections:
        if str((section or {}).get("name") or "").strip() == name:
            return section, None
    return create_section(name)


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
        _, error = _rpc("thread/section/move", {
            "threadId": thread_id, "sectionId": section_id,
        }, host_only=True)
        if error:
            return {"thread_id": thread_id, "cwd": thread.get("cwd"),
                    "section_id": None}, "thread 已创建，但加入窗口集合失败：" + error
    return {"thread_id": thread.get("id"), "cwd": thread.get("cwd"),
            "status": thread.get("status"),
            "section_id": section_id or _thread_section_id(thread)}, None


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
    result, error = _rpc("thread/section/move", params, host_only=True)
    if error:
        return None, error
    return result, None


def probe(timeout=12, progress=None):
    """如实报告「原生直连」现在通不通，以及不通是因为什么。

    这一条是给用户和面板用的：能派活的唯一条件是桌面端宿主的控制 socket 可达。
    socket 不可达时只降级成只读历史（能看，不能派），绝不把只读结果说成已连通，
    也不去改宿主的 SQLite 来伪造一个「已连接」。
    """
    info = {
        "socket": str(SOCKET),
        "socket_present": _socket_present(),
        "history_db": HISTORY_DB.is_file(),
        "transport": "unavailable",
        "connected": False,
        "can_dispatch": False,
        "reason": None,
        "sample": 0,
    }
    if info["socket_present"]:
        if progress:
            progress("正在连接 Codex 桌面宿主的控制 socket（最多等 %d 秒）…" % timeout)
        result, error = _rpc("thread/list",
                             {"limit": 1, "useStateDbOnly": True}, host_only=True,
                             timeout=timeout)
        if error is None:
            info.update(transport="codex-app-server-websocket" if _socket_present()
                        else "codex-app-server-stdio", connected=True,
                        can_dispatch=True, reason=None,
                        sample=len((result or {}).get("data") or []))
            return info
        if "Operation not permitted" in error or "Permission" in error:
            info["reason"] = f"Codex 控制 socket 存在，但当前进程无法访问：{error}"
        else:
            info["reason"] = error
    else:
        info["reason"] = f"没有找到 Codex 桌面端控制 socket：{SOCKET}"
    if info["history_db"]:
        info["transport"] = "history-read-only"
        info["sample"] = len(_history_snapshot())
        info["reason"] = (info["reason"] or "") + "（只能只读看历史，不能派活）"
    reason = info["reason"] or ""
    if "Operation not permitted" in reason or "Permission" in reason:
        info["hint"] = ("当前进程被系统权限挡住，连不到桌面端的控制 socket；"
                        "在终端（不受该限制的环境）里重跑一次 "
                        "`python3 ops/codex_native.py probe` 才是有效结论。")
    elif not info["socket_present"]:
        info["hint"] = "先启动 Codex 桌面端；它起来之后才会创建控制 socket。"
    elif not info["connected"]:
        pairing_hint = _chatgpt_pairing_hint(info.get("reason") or "")
        if not pairing_hint and _remote_control_requires_chatgpt():
            pairing_hint = _chatgpt_pairing_hint(
                "remote control requires ChatGPT authentication; API key auth is not supported")
        if pairing_hint:
            info["hint"] = pairing_hint
            info["blocked_by"] = "remote-control-chatgpt-pairing"
        else:
            # 连不上但没报权限错：多半是宿主没在等超时时间内回应。给可执行的下一步。
            info["hint"] = ("桌面宿主没有在这个时间内回应。先确认 Codex 正在运行；"
                            "仍不通就跑 `bash preview-check.sh` 看完整结论。")
    return info


def history(limit=100):
    """只读历史：桌面端的 thread 列表（不写任何宿主状态）。"""
    return _history_snapshot()[:limit]


def socket_state():
    """便宜的状态：只回答「socket 在不在」，不做连接（可以高频调用）。"""
    return {"socket": str(SOCKET), "socket_present": _socket_present(),
            "history_db": HISTORY_DB.is_file()}


def _probe_record_path():
    try:
        import control_state
        return control_state.home() / "native-probe.json"
    except Exception:
        return None


def record_probe(result):
    """把一次真实探测的结果落盘。面板/Companion 只显示这个已验证据，
    不自己猜「应该连得上」，也不把从没跑过自检说成已连通。"""
    path = _probe_record_path()
    if path is None:
        return None
    payload = dict(result)
    payload["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return None
    return payload


def last_probe():
    """上一次自检结果；从没跑过就是 None（区别于「跑过但没通」）。"""
    path = _probe_record_path()
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cli(argv):
    """命令行入口：用户在终端里能直接自证原生直连通不通。

    例：
        python3 ops/codex_native.py probe
        python3 ops/codex_native.py history --limit 5
        python3 ops/codex_native.py dispatch --cwd DIR --prompt "…" [--section SECTION] [--allow-isolated]
        python3 ops/codex_native.py follow --thread THREAD --prompt "…" [--allow-isolated]
        python3 ops/codex_native.py section --name "窗口集合名"
        python3 ops/codex_native.py move --thread THREAD --section SECTION
    """
    def take(flag, default=None):
        if flag in argv:
            index = argv.index(flag)
            if index + 1 < len(argv):
                return argv[index + 1]
        return default

    action = argv[0] if argv else "probe"
    if action == "probe":
        # 连不上时会等一个超时；先打一行进度，别让人对着空屏幕猜是不是卡死了。
        result = probe(progress=lambda line: print(line, file=sys.stderr, flush=True))
        record_probe(result)
        payload, error = result, None
        if not (result or {}).get("can_dispatch"):
            # 退出码要能一眼判断：连得上能派活才是 0，否则 1 并说明原因。
            error = ((result or {}).get("hint") or (result or {}).get("reason")
                     or "原生直连当前不可派活")
    elif action == "history":
        payload, error = history(int(take("--limit", 20))), None
    elif action == "snapshot":
        payload, error = snapshot(), None
    elif action == "dispatch":
        section_arg = take("--section")
        payload, error = dispatch_task(
            take("--cwd", os.getcwd()),
            take("--prompt", ""),
            None if section_arg else None,
            auto_section=section_arg,
            allow_isolated="--allow-isolated" in argv)
    elif action == "follow":
        payload, error = follow_up(take("--thread"), take("--prompt", ""),
                                   allow_isolated="--allow-isolated" in argv)
    elif action == "section":
        payload, error = create_section(take("--name", ""))
    elif action == "move":
        payload, error = move_thread(take("--thread"), take("--section"))
    else:
        print(json.dumps({"code": "UNKNOWN_ACTION", "message": action},
                         ensure_ascii=False))
        return 2
    print(json.dumps({"ok": error is None, "result": payload, "error": error},
                     ensure_ascii=False, indent=2))
    return 0 if error is None else 1


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
