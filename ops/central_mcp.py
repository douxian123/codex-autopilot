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

import control_state  # noqa: E402

# 自包含安装下，Codex 一般已从 .mcp.json 注入 env；这里再兜底一次，
# 保证脱离 Codex 手动拉起 MCP 时也绑定到同一个项目。
apply_plugin_env()

RUNTIME = control_state.home()
PANEL_URI = "ui://autopilot/panel-v2.html"


def _panel_html_path():
    """面板 HTML 在源码树里是 plugin/ui/，自包含安装后是 <插件根>/ui/。"""
    for cand in (OPS_DIR.parent / "ui" / "panel.html",
                 OPS_DIR.parent / "plugin" / "ui" / "panel.html"):
        if cand.exists():
            return cand
    return None


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
        "name": "resolve_list",
        "description": "列出正在进行的冲突执行任务：每条对应一个独立 worktree + 任务书，含待解文件。用户问“还有哪些冲突没解 / 冲突现在什么状态”时用它。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "resolve_plan",
        "description": "预演：挑一条「冲突文件少、未落改动小」的冲突分支，说明它有几个冲突文件、多大，拆了会怎样。只读，不建 worktree、不动 main。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "branch": {"type": "string", "description": "指定分支；不填则按体量自动挑一条"},
                "max_files": {"type": "integer", "description": "冲突文件数闸门（默认 8）"},
                "max_lines": {"type": "integer", "description": "未落改动行数闸门（默认 1200）"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "resolve_start",
        "description": "把一条冲突分支拆成独立可执行单元：在 origin/main 上开独立 worktree，append-only JSONL 自动行级并集，其余保留 marker 并写任务书。main 与源分支都不动。用户说“继续处理冲突 / 把冲突跑完”时用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "branch": {"type": "string", "description": "指定分支；不填则按体量自动挑一条"},
                "max_files": {"type": "integer", "description": "冲突文件数闸门（默认 8）"},
                "max_lines": {"type": "integer", "description": "未落改动行数闸门（默认 1200）"},
                "confirm": {"type": "boolean", "description": "true 才真正建 worktree / 任务；默认只预演"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "resolve_finish",
        "description": "把已解干净的任务走 land 的完整门禁后落盘并回收 worktree。只在 worktree 无 marker、无未提交改动时通过。用户说“冲突解完了，收口”时用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "任务 slug 或源分支名"},
                "confirm": {"type": "boolean", "description": "true 才真正落盘；默认只检查"},
            },
            "required": ["target"],
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
    {
        "name": "autopilot_status",
        "description": "自动挡的当前状态：是否暂停、三个自动开关、常驻是否在跑、正在做什么/为什么、最近替你完成了什么、累计收到多少反馈与收口多少项、是否有需要你拍板的事。面板用它渲染；模型回答“自动挡现在在干嘛 / 开着吗 / 做了什么”时也用它。默认 fast=true 只读本机缓存，不发命令、不改任何东西。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "fast": {"type": "boolean", "description": "true（默认）只读缓存，秒回；false 才现场重扫（较慢）"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "autopilot_control",
        "description": "控制自动挡。action=pause 随时停（不再启动新工作，当前 Git 操作安全收尾）；resume 恢复；run_once 立刻检查一轮；set_auto_land/set_auto_resolve/set_auto_port 真开关三个自动动作；enable_daemon/disable_daemon 开关后台常驻。返回控制后的最新状态。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["pause", "resume", "run_once", "set_auto_land",
                             "set_auto_resolve", "set_auto_port",
                             "enable_daemon", "disable_daemon"],
                },
                "enabled": {"type": "boolean", "description": "set_auto_* 的目标值"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "render_autopilot_panel",
        "description": "打开自动挡面板（inline 卡片 / 对话侧栏 / 全局侧栏 / 全屏）。面板第一屏用大白话说明：它是什么、怎么用（普通 Codex 输入框就是总输入框，不用新建对话或分支）、它替你做了什么、现在在做什么、你需要做什么、随时可暂停。用户问“自动挡在哪看 / 怎么用 / 现在开着吗”时用它。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "_meta": {
            "ui": {"resourceUri": PANEL_URI},
            "openai/ui": {
                "entrypoints": [{"type": "global"}, {"type": "thread"}],
                "availableDisplayModes": ["inline", "fullscreen"],
            },
            "openai/toolInvocation/invoking": "打开自动挡面板…",
            "openai/toolInvocation/invoked": "自动挡面板已打开。",
        },
    },
]


def _json_file(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _daemon_state():
    return _json_file(RUNTIME / "state.json")


def _daemon_live():
    """常驻是否真在跑：launchd 的 plist 存在，且实例锁背后确实有一个活进程。"""
    if not (Path.home() / "Library" / "LaunchAgents"
            / "com.codex-autopilot.daemon.plist").exists():
        return False, None
    lock = RUNTIME / "daemon.lock"
    try:
        raw = lock.read_text(encoding="utf-8").strip()
    except OSError:
        return True, None
    pid = None
    for tok in raw.replace("\n", " ").split():
        if tok.isdigit():
            pid = int(tok)
            break
    if pid is None:
        return True, None
    try:
        os.kill(pid, 0)
        return True, pid
    except (OSError, ProcessLookupError):
        return False, pid


def _ledger_count():
    """唯一入口累计收到多少条反馈：项目真相层里 ledger 的行数（只读、秒级）。"""
    repo = os.environ.get("KG_REPO")
    if not repo:
        return 0
    ledger = Path(repo) / "ops" / "truth" / "ledger.jsonl"
    try:
        return sum(1 for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return 0


def _active_resolve_tasks():
    """进行中的冲突拆解任务（needs_agent / gate_fail）。只读任务目录。"""
    repo = os.environ.get("KG_REPO")
    if not repo:
        return []
    reg = Path(repo) / "ops" / "tasks" / "resolve" / "registry.json"
    data = _json_file(reg)
    tasks = data.get("tasks") or []
    settled = {t.get("slug") for t in tasks if t.get("status") in ("landed", "abandoned")}
    settled |= {t.get("branch") for t in tasks if t.get("status") in ("landed", "abandoned")}
    out = []
    for t in tasks:
        if t.get("status") in ("needs_agent", "gate_fail") \
                and t.get("slug") not in settled and t.get("branch") not in settled:
            out.append({"slug": t.get("slug"), "branch": t.get("branch"),
                        "status": t.get("status"),
                        "files": t.get("files") or t.get("conflict_files") or []})
    return out


def _status_payload(fast=True):
    """把控制开关 + 缓存状态 + 项目计数，拼成面板和模型都能用的一个 JSON。"""
    from datetime import datetime, timezone
    ctrl = control_state.load()
    st = _daemon_state()
    live, pid = _daemon_live()
    summary = st.get("last_summary") or {}
    failures = [{"target": t, "kind": k} for t, k in (st.get("last_failures") or [])]
    rtasks = _active_resolve_tasks()
    blocked = st.get("resolve_blocked") or {}
    port_blocked = st.get("port_blocked") or {}
    paused = bool(ctrl.get("paused"))

    if ctrl.get("error"):
        state_key, state_label = "error", "状态异常，已安全暂停"
    elif paused:
        state_key, state_label = "paused", "已暂停"
    elif failures:
        state_key, state_label = "blocked", "有分支需要你拍板"
    elif not live:
        state_key, state_label = "session_only", "仅会话内运行（常驻未开）"
    elif summary.get("clean") or summary.get("merged"):
        state_key, state_label = "working", "正在检查可合入的分支"
    else:
        state_key, state_label = "idle", "运行中，暂无待办"

    # 大白话的「正在做什么 / 为什么 / 最近做了什么 / 需要你做什么」。
    if paused:
        doing = "暂停中，没有在动任何分支"
        why = "你按了暂停。恢复后它才会继续检查、合入"
    elif failures:
        tgt, kind = failures[0]["target"], failures[0]["kind"]
        doing = f"卡在 {tgt}（{kind}），已停下等你决定"
        why = "这类改动要么改了产品方向、要么门禁过不了，按约定不自动合"
    elif summary.get("clean"):
        doing = f"正在把 {summary.get('clean')} 个和主线无冲突的分支逐个合入"
        why = "它们和 main 无冲突、构建门禁已过，属于可以安全自动合入的那一类"
    elif rtasks:
        doing = f"正在处理 {len(rtasks)} 条冲突，拆成可执行任务后逐条收口"
        why = "冲突不丢给你逐条裁决，先自动拆成任务，解完再走同一道门禁"
    else:
        doing = "已检查一轮，当前没有能安全自动合入的分支"
        why = "和 main 冲突的、或门禁没过的，不会被自动合，等你处理"

    last = st.get("last_landed") or []
    if last:
        recent = f"最近自动合入 {len(last)} 个分支：{', '.join(last[:3])}" + (
            " 等" if len(last) > 3 else "")
    else:
        recent = "还没有自动合入过分支"

    if failures:
        need_action = f"需要你拍板：{failures[0]['target']}（{failures[0]['kind']}）"
    elif ctrl.get("error"):
        need_action = "控制文件异常，去高级面板看一眼或恢复默认"
    else:
        need_action = "不需要你做什么，继续照常提需求就行"

    payload = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "fast": bool(fast),
        "paused": paused,
        "error": ctrl.get("error"),
        "state": state_key,
        "state_label": state_label,
        "doing": doing,
        "why": why,
        "recent": recent,
        "need_action": need_action,
        "switches": {
            "auto_land": bool(ctrl.get("auto_land", True)),
            "auto_resolve": bool(ctrl.get("auto_resolve", True)),
            "auto_port": bool(ctrl.get("auto_port", False)),
            "daemon_enabled": bool(ctrl.get("daemon_enabled", True)),
        },
        "daemon": {
            "live": bool(live),
            "pid": pid,
            "last_cycle": st.get("last_cycle_iso"),
            "interval_min": st.get("interval_min"),
        },
        "counters": {
            "received": _ledger_count(),
            "landed": int(st.get("total_landed", 0)),
            "resolved": int(st.get("total_resolved", 0)),
            "superseded": int(st.get("total_superseded", 0)),
            "ported": int(st.get("total_ported", 0)),
        },
        "queue": {
            "clean": summary.get("clean"),
            "merged": summary.get("merged"),
            "conflict": summary.get("conflict"),
            "unresolved": summary.get("unresolved"),
        },
        "blocked": {
            "failures": failures,
            "resolve": [{"branch": b, "reason": (v or {}).get("reason")}
                        for b, v in list(blocked.items())[:20]],
            "port": [{"branch": b, "reason": (v or {}).get("reason")}
                     for b, v in list(port_blocked.items())[:20]],
        },
        "resolve_tasks": rtasks[:20],
        "events": control_state.read_events(30),
    }
    payload["__text__"] = _status_text(payload)
    return payload


def _status_text(payload):
    sw = payload["switches"]
    q = payload["queue"]
    lines = [
        "自动挡 · " + payload["state_label"],
        "正在做：" + payload["doing"],
        "为什么：" + payload["why"],
        payload["recent"],
        payload["need_action"],
        "",
        "开关：自动合入=%s  自动拆冲突=%s  自动移植=%s  后台常驻=%s"
        % ("开" if sw["auto_land"] else "关",
           "开" if sw["auto_resolve"] else "关",
           "开" if sw["auto_port"] else "关",
           "开" if sw["daemon_enabled"] else "关"),
        "队列：可合 %s · 已合待回收 %s · 冲突 %s"
        % (q.get("clean"), q.get("merged"), q.get("conflict")),
        "累计：收到反馈 %s 条 · 自动合入 %s 个 · 拆解冲突 %s 条"
        % (payload["counters"]["received"], payload["counters"]["landed"],
           payload["counters"]["resolved"]),
    ]
    return "\n".join(lines)


def _with_status(text):
    """给控制结果附上最新状态，让面板一次调用就拿到权威快照。"""
    payload = _status_payload()
    payload["__text__"] = text + "\n" + payload.get("__text__", "")
    return payload


def control_action(action, enabled=None):
    """执行一个控制动作，返回 (dict 或文本, rc)。开关都真落到用户级 control.json。"""
    if not action:
        return "autopilot_control 需要 action", 2
    if action == "pause":
        control_state.set_keys(paused=True)
        control_state.append_event("pause")
        return _with_status("已暂停：不再启动新工作；当前 Git 操作会安全收尾。"), 0
    if action == "resume":
        control_state.set_keys(paused=False)
        control_state.append_event("resume")
        return _with_status("已恢复运行：下一轮会继续检查、合入。"), 0
    if action == "run_once":
        if control_state.is_paused():
            return "当前处于暂停中，不会偷偷跑一轮。先恢复运行再试。", 2
        daemon = OPS_DIR / "central_daemon.py"
        control_state.append_event("run_once")
        out, rc = _run([str(daemon), "--once"], timeout=2400)
        return _with_status("已跑完一轮：\n" + out), rc
    if action in ("set_auto_land", "set_auto_resolve", "set_auto_port"):
        if not isinstance(enabled, bool):
            return "%s 需要 enabled（true/false）" % action, 2
        key = action[len("set_"):]
        control_state.set_keys(**{key: enabled})
        control_state.append_event("switch", name=key, value=enabled)
        return _with_status("%s 已%s。" % (key, "打开" if enabled else "关闭")), 0
    if action in ("enable_daemon", "disable_daemon"):
        daemon = OPS_DIR / "central_daemon.py"
        want = action == "enable_daemon"
        out, rc = _run([str(daemon), "--install" if want else "--uninstall"], timeout=120)
        if rc == 0:
            control_state.set_keys(daemon_enabled=want)
            control_state.append_event("daemon", enabled=want)
        return _with_status(out or ("已%s常驻" % ("开启" if want else "关闭"))), rc
    return "未知 action：%s" % action, 2


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
    if name == "resolve_list":
        return kgctl("resolve", "--list")
    if name in ("resolve_plan", "resolve_start"):
        branch = (args.get("branch") or "").strip()
        argv = ["resolve"]
        if branch:
            argv.append(branch)
        else:
            argv.append("--auto")
        if args.get("max_files"):
            argv += ["--max-files", str(args["max_files"])]
        if args.get("max_lines"):
            argv += ["--max-lines", str(args["max_lines"])]
        if name == "resolve_start" and args.get("confirm"):
            argv.append("--go")
        return kgctl(*argv)
    if name == "resolve_finish":
        target = (args.get("target") or "").strip()
        if not target:
            return "resolve_finish 需要 target（slug 或分支名）", 2
        argv = ["resolve", "--finish", target]
        if args.get("confirm"):
            argv.append("--go")
        return kgctl(*argv)
    if name == "generate_context":
        branch = (args.get("branch") or "").strip()
        if not branch:
            return "generate_context 需要 branch（或 all）", 2
        return kgctl("context", branch)
    if name == "dashboard":
        return kgctl("dash", *(["--open"] if args.get("open") else []))
    if name == "autopilot_status":
        return _status_payload(fast=True), 0
    if name == "autopilot_control":
        return control_action(args.get("action"), args.get("enabled"))
    if name == "render_autopilot_panel":
        panel = _panel_html_path()
        if panel is None:
            return "面板文件缺失（plugin/ui/panel.html）。请重装插件后再试。", 2
        return _status_payload(fast=True), 0
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
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False},
            },
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "这是项目的自动挡。用户抛出零散、即时的需求（视觉/体验/bug/架构/"
                "门禁红灯/多分支要不要合/该在哪继续）时：直接调用 intake_request 把原话"
                "收进唯一入口并归类落账，再按 next_actions 的优先级执行；多条需求汇聚或"
                "需要重扫时用 sync_control_plane。冲突分支不要再逐条问用户，用 "
                "resolve_list / resolve_plan / resolve_start / resolve_finish 逐条拆成可执行"
                "任务并收口；只有冲突量大且分散时才用 convergence_plan 收成族。用户说"
                "“继续 / 处理冲突 / 把冲突跑完”时优先走 resolve_start（确认后），解完用 "
                "resolve_finish 走门禁落盘。不要要求用户自己选对话、分支或角色。"
                "用户问“自动挡在哪看 / 怎么用 / 现在开着吗 / 做了什么”时，调用 "
                "render_autopilot_panel 打开面板（含小白说明与开关）；要看纯文本状态用 "
                "autopilot_status；要暂停/恢复/开关自动动作或后台常驻用 autopilot_control。"
            ),
        })
        return
    if method in ("notifications/initialized", "notifications/cancelled"):
        return
    if method == "ping":
        _result(rid, {})
        return
    if method == "resources/list":
        _result(rid, {"resources": [{
            "uri": PANEL_URI,
            "name": "Codex 自动挡面板",
            "description": "自动挡的状态面板：怎么用、正在做什么、为什么、替你完成了什么、要不要你拍板、可暂停与开关。",
            "mimeType": "text/html;profile=mcp-app",
        }]})
        return
    if method == "resources/read":
        uri = (msg.get("params") or {}).get("uri")
        if uri != PANEL_URI:
            _error(rid, -32602, "未知资源：%s" % uri)
            return
        panel = _panel_html_path()
        if panel is None:
            _error(rid, -32603, "面板文件缺失（plugin/ui/panel.html）")
            return
        _result(rid, {"contents": [{
            "uri": PANEL_URI,
            "mimeType": "text/html;profile=mcp-app",
            "text": panel.read_text(encoding="utf-8"),
            "_meta": {"ui": {"prefersBorder": True}},
        }]})
        return
    if method == "tools/list":
        _result(rid, {"tools": TOOLS})
        return
    if method == "tools/call":
        params = msg.get("params") or {}
        out, code = call_tool(params.get("name"), params.get("arguments"))
        if isinstance(out, dict):
            text = out.get("__text__") or "已更新。"
            result = {"content": [{"type": "text", "text": text}],
                      "structuredContent": out, "isError": code != 0}
        else:
            result = {"content": [{"type": "text", "text": out}], "isError": code != 0}
        _result(rid, result)
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
