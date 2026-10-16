#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex native thread bridge with a read-only history fallback."""

import json
import os
import selectors
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
SOCKET = Path(os.environ.get("CODEX_APP_SERVER_SOCKET", CODEX_HOME / "ipc" / "ipc.sock"))
HISTORY_DB = Path(os.environ.get("CODEX_THREAD_HISTORY_DB",
                                 CODEX_HOME / "thread_history_1.sqlite"))
CLI = os.environ.get("CODEX_CLI", "codex")
# 子对话默认归进这个窗口集合：自动挡派人干活时统一开在这里，用户不必自己整理。
DEFAULT_SECTION_NAME = os.environ.get("CODEX_AUTOPILOT_SECTION", "Codex 自动挡")
# 查宿主会话/窗口集合的时间预算。桌面宿主「连得上但不回话」时，
# 控制面（面板开关、Companion、MCP status）绝不能被它拖住。
SNAPSHOT_TIMEOUT = float(os.environ.get("CODEX_AUTOPILOT_NATIVE_TIMEOUT", "6"))
_next_id = 0


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


def _rpc(method, params, host_only=False, timeout=30):
    """Issue one request over a handshaken app-server connection.

    timeout 是这一步的总预算：握手与发请求共享同一个 deadline，
    否则「连得上但不回话」会变成两段各自等满。
    """
    deadline = time.monotonic() + max(1.0, float(timeout))
    proc, error = _session(host_only=host_only, timeout=timeout)
    if error:
        return None, error
    try:
        return _session_request(proc, method, params,
                                timeout=max(1.0, deadline - time.monotonic()))
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
                detail = _drain_stderr(proc)
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


def _session(host_only=False, timeout=30):
    """在同一个预算内逐个试候选服务，不按候选数把等待翻倍。"""
    deadline = time.monotonic() + max(1.0, float(timeout))
    last_error = None
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


def dispatch_task(cwd, prompt, section_id=None, auto_section=None):
    """Create a native thread and start its first turn in the same session."""
    path = Path(cwd).expanduser()
    if not path.is_absolute() or not path.is_dir():
        return None, "cwd 必须是已存在的绝对目录"
    if not str(prompt or "").strip():
        return None, "prompt 不能为空"
    section_note = None
    if not section_id and auto_section:
        section, section_error = ensure_section(auto_section)
        if section:
            section_id = section.get("id")
        else:
            section_note = "没能建/找到窗口集合「%s」：%s" % (auto_section, section_error)
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


_HISTORY_CACHE = {"t": 0.0, "rows": []}
_HISTORY_CACHE_TTL = 5.0


def _history_snapshot(force=False):
    """只读历史快照，短 TTL 缓存让面板/开关快路不反复扫整个 SQLite。"""
    now = time.monotonic()
    if not force and _HISTORY_CACHE["rows"] and (now - _HISTORY_CACHE["t"]) < _HISTORY_CACHE_TTL:
        return _HISTORY_CACHE["rows"]
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
        _HISTORY_CACHE.update(t=now, rows=result)
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
                "sections": [], "threads": _history_snapshot()}
    deadline = time.monotonic() + max(
        1.0, float(SNAPSHOT_TIMEOUT if timeout is None else timeout))
    sections, section_error = _rpc("threadSection/list", {"limit": 100},
                                   timeout=max(1.0, deadline - time.monotonic()))
    threads, thread_error = _rpc(
        "thread/list", {"limit": 100, "sortKey": "updated_at",
                        "sortDirection": "desc", "useStateDbOnly": True},
        timeout=max(1.0, deadline - time.monotonic()))
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
        "socket_present": SOCKET.is_socket(),
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
            info.update(transport="codex-app-server-stdio", connected=True,
                        can_dispatch=True, reason=None,
                        sample=len((result or {}).get("data") or []))
            return info
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
        # 连不上但没报权限错：多半是宿主没在等超时时间内回应。给可执行的下一步。
        info["hint"] = ("桌面宿主没有在这个时间内回应。先确认 Codex 正在运行；"
                        "仍不通就跑 `bash preview-check.sh` 看完整结论。")
    return info


def history(limit=100):
    """只读历史：桌面端的 thread 列表（不写任何宿主状态）。"""
    return _history_snapshot()[:limit]


def socket_state():
    """便宜的状态：只回答「socket 在不在」，不做连接（可以高频调用）。"""
    return {"socket": str(SOCKET), "socket_present": SOCKET.is_socket(),
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
        python3 ops/codex_native.py dispatch --cwd DIR --prompt "…" [--section SECTION]
        python3 ops/codex_native.py follow --thread THREAD --prompt "…"
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
        payload, error = dispatch_task(take("--cwd", os.getcwd()),
                                       take("--prompt", ""), take("--section"))
    elif action == "follow":
        payload, error = follow_up(take("--thread"), take("--prompt", ""))
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
