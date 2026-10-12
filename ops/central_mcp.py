#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动挡的 MCP server（stdio，JSON-RPC 2.0，stdlib only）。

它把 ops/kgctl.py 的能力暴露成 MCP 工具，让任意 Codex 对话不必知道 CLI 细节，
就能：记录零散需求、刷新项目真相、看下一步、路由到固定泳道、给分支建议。

设计约束：
  * 只走 stdio，stdout 只允许出现 JSON-RPC 消息；子进程输出全部被捕获后随
    tool result 返回，绝不直接 print 到 stdout。
  * 每个工具都委托给 kgctl.py / truthctl.py，不复制逻辑；状态根由 kgctl 的
    state_root 统一解析（KG_REPO > KG_TRUNK > 主工作树 > 当前 worktree）。
  * 自包含安装下，本文件随插件走（<插件目录>/ops/central_mcp.py），运行时代码就是
    这一份；目标项目只存账本。插件的 .mcp.json 因此注入 KG_REPO=<目标项目>
    （可选 KG_TRUNK=<落盘用的主干 worktree>），把状态写回项目而不是写进插件目录。
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

OPS_DIR = Path(__file__).resolve().parent
REPO = OPS_DIR.parent
KGCTL = OPS_DIR / "kgctl.py"
TRUTHCTL = OPS_DIR / "truthctl.py"


def _server_version() -> str:
    """版本以插件清单为准，避免 MCP 里再写死一份导致漂移。"""
    for rel in (".codex-plugin/plugin.json", "plugin/.codex-plugin/plugin.json"):
        manifest = OPS_DIR.parent / rel
        try:
            return json.loads(manifest.read_text(encoding="utf-8")).get("version", "0.0.0")
        except (OSError, ValueError):
            continue
    return "0.0.0"


SERVER_VERSION = _server_version()

from plugin_binding import apply_plugin_env  # noqa: E402

# 自包含安装下，Codex 一般已从 .mcp.json 注入 env；这里再兜底一次，
# 保证脱离 Codex 手动拉起 MCP 时也绑定到同一个项目。
apply_plugin_env()


def _run(argv, extra_env=None, timeout=900):
    env = os.environ.copy()
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items() if v is not None})
    # cwd 用状态根：kgctl 的 git 回退路径（worktree list / rev-parse）要落在目标项目里，
    # 而不是插件自己所在的目录（那里通常不是一个 Git 仓库）。
    cwd = os.environ.get("KG_REPO") or str(REPO)
    try:
        p = subprocess.run([sys.executable, *argv], capture_output=True, text=True,
                           cwd=cwd, env=env, timeout=timeout)
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
                "kind": {"type": "string", "enum": ["visual", "ux", "bug", "arch", "gate", "unknown"],
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
    {
        "name": "intake_request",
        "description": "一句话入口：把用户原话记进唯一入口，立刻归类、写入问题台账、刷新真相与看板、给出下一步。用户抛零散需求（视觉/体验/bug/架构/门禁）时优先用它，一步走完整条链，不用先 capture 再 sync。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "用户原话，别重写、别归类"},
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "相关文件路径（可选，帮助路由到既有泳道）"},
                "kind": {"type": "string", "enum": ["visual", "ux", "bug", "arch", "gate", "unknown"],
                         "description": "粗分类，拿不准就用 unknown"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "convergence_plan",
        "description": "把几十个冲突分支按“抢同一批文件”收成少数几个 convergence 任务。默认只预演，不建分支、不动 main。用户被“到底哪个分支先合”反复打断时用它，把逐条裁决收成一族一族的处理。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "confirm": {"type": "boolean", "description": "true 才真正建 convergence 分支/worktree（不改 main、不改源分支）"},
                "limit": {"type": "integer", "description": "本次最多收拢几族（默认全部）"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "generate_context",
        "description": "为分支生成版本化 Context Pack（主线/决策/相关未决问题/验收 DoD/归属/收拢任务），写进 ops/context/。让接手任务不靠聊天记忆。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "branch": {"type": "string", "description": "分支名；给 all 则对全部未合并分支生成"},
            },
            "required": ["branch"],
            "additionalProperties": False,
        },
    },
    {
        "name": "dashboard",
        "description": "生成（可选顺带打开）驾驶舱 HTML：待路由输入、可落盘分支、冲突收拢族、未决问题、最近进展。",
        "inputSchema": {
            "type": "object",
            "properties": {"open": {"type": "boolean", "description": "macOS 上顺带用浏览器打开"}},
            "additionalProperties": False,
        },
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
    if name == "intake_request":
        text = (args.get("text") or "").strip()
        if not text:
            return "intake_request 需要 text", 2
        files = args.get("files") or []
        env = {"KG_INBOX_FILES": ":".join(files) if files else None,
               "KG_INBOX_KIND": args.get("kind") or "unknown"}
        return kgctl("intake", text, extra_env=env)
    if name == "convergence_plan":
        argv = ["converge"]
        if args.get("confirm"):
            argv.append("--go")
        if args.get("limit"):
            argv += ["--limit", str(args["limit"])]
        return kgctl(*argv)
    if name == "generate_context":
        branch = (args.get("branch") or "").strip()
        if not branch:
            return "generate_context 需要 branch（或 all）", 2
        return kgctl("context", branch)
    if name == "dashboard":
        return kgctl("dash", *(["--open"] if args.get("open") else []))
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
                "门禁红灯/多分支要不要合/该在哪继续）时：直接调用 intake_request 把原话"
                "收进唯一入口并归类落账，再按 next_actions 的优先级执行；多条需求汇聚或"
                "需要重扫时用 sync_control_plane。冲突分支不要再逐条问用户，用 "
                "convergence_plan 收成族处理。不要要求用户自己选对话、分支或角色。"
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
