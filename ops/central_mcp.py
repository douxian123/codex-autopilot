#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动挡的 MCP server（stdio，JSON-RPC 2.0，stdlib only）。

它把 ops/kgctl.py 的能力暴露成 MCP 工具，让任意 Codex 对话不必知道 CLI 细节，
就能：记录零散需求、刷新项目真相、看下一步、路由到固定泳道、给分支建议。

设计约束：
  * 只走 stdio，stdout 只允许出现 JSON-RPC 消息；子进程输出全部被捕获后随
    tool result 返回，绝不直接 print 到 stdout。
  * 每个工具都委托给 kgctl.py / truthctl.py，不复制逻辑；状态根由 kgctl 的
    state_root 统一解析（KG_REPO > KG_TRUNK > 主工作树 > 当前 worktree），
    本 server 不强行覆盖 KG_REPO，避免又从插件检出里长出第三份账本。
  * 默认不破坏：记录是追加；路由默认预演，confirm=true 才落账；合并仍交给
    `kgctl land`，本 server 不直接合并。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "codex-autopilot"
SERVER_VERSION = "0.1.0"

OPS_DIR = Path(__file__).resolve().parent
REPO = OPS_DIR.parent
KGCTL = OPS_DIR / "kgctl.py"
TRUTHCTL = OPS_DIR / "truthctl.py"


def _run(argv, extra_env=None, timeout=900):
    env = os.environ.copy()
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items() if v is not None})
    try:
        p = subprocess.run([sys.executable, *argv], capture_output=True, text=True,
                           cwd=str(REPO), env=env, timeout=timeout)
        out = (p.stdout or "").rstrip()
        err = (p.stderr or "").rstrip()
        text = out + (("\n" + err) if err else "")
        return text.strip() or "(exit %d，无输出)" % p.returncode, p.returncode
    except subprocess.TimeoutExpired:
        return "命令超时（>%ss）：%s" % (timeout, " ".join(argv)), 124


def kgctl(*args, extra_env=None):
    return _run([str(KGCTL), *args], extra_env)


def truthctl(*args):
    return _run([str(TRUTHCTL), *args])


TOOLS = [
    {
        "name": "project_status",
        "description": "一屏看清项目到哪了：状态根、主干、待路由输入、分支可合并/冲突数、共享检出脏度。用户问“现在到哪了 / 还有什么没做”时先用它。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "capture_request",
        "description": "把一条零散、即时的需求收进唯一入口（inbox）。视觉/体验/bug/架构问题都用它，用户不用自己分类、不用选对话或分支。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "用户原话，别重写、别归类"},
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "相关文件路径（可选，帮助路由到既有泳道）"},
                "kind": {"type": "string", "enum": ["visual", "ux", "bug", "arch", "unknown"],
                         "description": "粗分类，拿不准就用 unknown"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sync_control_plane",
        "description": "自动挡主循环：路由 inbox 到固定泳道 -> 刷新项目真相 -> 刷新控制看板 -> 落盘真相层 -> 给出下一步。用户一次抛了多条需求后调用。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "route_inbox",
        "description": "把唯一入口里的零散项落到固定泳道并写进问题台账。默认预演（只读），confirm=true 才落账。",
        "inputSchema": {
            "type": "object",
            "properties": {"confirm": {"type": "boolean", "description": "true 才真正写台账"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "next_actions",
        "description": "只回答一个问题：现在最该动的是哪几条（按优先级排出待办）。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "suggest_branch",
        "description": "按文件重叠给“该并进哪个既有分支”的只读建议，不落账。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def call_tool(name, args):
    args = args or {}
    if name == "project_status":
        return kgctl("status")
    if name == "capture_request":
        text = (args.get("text") or "").strip()
        if not text:
            return "capture_request 需要 text", 2
        files = args.get("files") or []
        env = {"KG_INBOX_FILES": ":".join(files) if files else None,
               "KG_INBOX_KIND": args.get("kind") or "unknown"}
        return kgctl("inbox", text, extra_env=env)
    if name == "sync_control_plane":
        return kgctl("tick")
    if name == "route_inbox":
        return kgctl("route", *(["--go"] if args.get("confirm") else []))
    if name == "next_actions":
        return truthctl("next")
    if name == "suggest_branch":
        return kgctl("suggest")
    return "未知工具：%s" % name, 2


def _send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(rid, result):
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid, code, message):
    _send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def handle(msg):
    method = msg.get("method")
    rid = msg.get("id")
    if method == "initialize":
        client_pv = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        _result(rid, {
            "protocolVersion": client_pv,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "这是项目的自动挡。用户抛出零散、即时的需求（视觉/体验/bug/架构/"
                "门禁红灯/多分支要不要合/该在哪继续）时：先 capture_request 记录原话，"
                "再 sync_control_plane 归类并刷新真相，然后按 next_actions 的优先级执行。"
                "不要要求用户自己选对话、分支或角色。"
            ),
        })
        return
    if method in ("notifications/initialized", "notifications/cancelled"):
        return
    if method == "ping":
        _result(rid, {})
        return
    if method == "tools/list":
        _result(rid, {"tools": TOOLS})
        return
    if method == "tools/call":
        params = msg.get("params") or {}
        text, code = call_tool(params.get("name"), params.get("arguments"))
        _result(rid, {"content": [{"type": "text", "text": text}], "isError": code != 0})
        return
    if rid is not None:
        _error(rid, -32601, "method not found: %s" % method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        try:
            handle(msg)
        except Exception as e:  # 不让单条请求把整条流打断
            if isinstance(msg, dict) and msg.get("id") is not None:
                _error(msg["id"], -32603, "%s: %s" % (type(e).__name__, e))


if __name__ == "__main__":
    main()
