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
import re
import subprocess
import sys
import time
from pathlib import Path

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "codex-autopilot"

OPS_DIR = Path(__file__).resolve().parent
REPO = OPS_DIR.parent
KGCTL = OPS_DIR / "kgctl.py"
TRUTHCTL = OPS_DIR / "truthctl.py"


def _closure_rate_summary():
    """北极星「首问闭环率」的三态摘要。

    取不到就说取不到：这里绝不把失败降级成 0% 或「正常」，否则面板会用一个
    假绿灯掩盖「根本没在测」这件事（本项目自己的硬规则 10）。
    """
    try:
        import closure_rate
        res = closure_rate.compute(os.environ.get("KG_REPO") or REPO)
    except Exception as exc:  # 模块坏了本身就是要上报的问题，不能静默成「没问题」
        res = {"status": "unknown",
               "reason": "闭环率计算失败（%s）" % exc,
               "denominator": 0, "numerator": 0, "rate_pct": None}
    return res


def _closure_rate_line(res):
    """给模型/人看的一行；算不出来必须说「取不到」，不能伪装成 0% 或正常。"""
    res = res or {}
    if res.get("status") == "ok":
        return "北极星首问闭环率：%s%%（%s/%s 已闭环）" % (
            res.get("rate_pct"), res.get("numerator"), res.get("denominator"))
    return "北极星首问闭环率取不到：%s" % (res.get("reason") or "原因未记录")


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
import codex_native  # noqa: E402

try:
    import exec_status  # noqa: E402
except Exception:  # 自动实现状态探测缺失时降级：面板只少一行，不影响其它判断
    exec_status = None

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


def truthctl(*args, extra_env=None):
    return _run([str(TRUTHCTL), *args], extra_env)


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
        "name": "product_state",
        "description": "产品今天是怎么工作的：按模块列出当前产品的实际行为，以及相对主干的变化。用户问“产品现在到底怎么样了 / 这次改动对用户有什么变化 / 改完是什么样”时用它。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "product_update",
        "description": "更新一条产品状态：某个模块今天实际会怎么表现。改完产品行为的会话应在同一轮调用它，让人只看产品变化、不读代码。why/files 可选。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "area": {"type": "string", "description": "模块名，如“自动挡面板”“落盘”"},
                "behavior": {"type": "string", "description": "产品今天的实际行为，一句话"},
                "why": {"type": "string", "description": "为什么这样，可选"},
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "相关文件路径，可选"},
            },
            "required": ["area", "behavior"],
            "additionalProperties": False,
        },
    },
    {
        "name": "distill_review",
        "description": "回看这一轮人额外说了什么，以及被卡住需要拍板的地方，列出候选判断。收口一轮后先用它，再把判断逻辑写回。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "distill_add",
        "description": "把「为什么这么判断」写回判断逻辑台账，让同一个判断人只做一次。只记可复用的判断逻辑，不记一次性细节。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "statement": {"type": "string", "description": "判断逻辑，一句话"},
                "why": {"type": "string", "description": "为什么这么判断，可选"},
                "scope": {"type": "string", "enum": ["product", "design", "arch", "process"],
                          "description": "适用范围，默认 process"},
            },
            "required": ["statement"],
            "additionalProperties": False,
        },
    },
    {
        "name": "synthesize_mainline",
        "description": "把决策/进展/问题/验收/产品状态综合成一页产品主线（docs/PRODUCT_MAINLINE.md）。产品讨论和零散需求会自动长成产品叙事，人只读这一页就知道现状、最近在推什么、还差什么。每轮收口或用户问“产品主线/PRD/到哪了”时调用。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "close_item",
        "description": "关闭问题台账里的一条（bug/视觉/体验/架构）：标 done 并把结论追加成进展；可带 commit 与证据。用户报的 bug 修完、验收过之后调用，让它从「收到」走到「已关闭」，不再依赖用户自己记。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "问题台账 id，如 L-0003"},
                "note": {"type": "string", "description": "结论/证据，一句话"},
                "commit": {"type": "string", "description": "落盘 commit sha，可选"},
                "evidence": {"type": "string", "description": "可复核的验证证据，可选"},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "reopen_item",
        "description": "把已关闭的问题重新打开（回归/证据不足/用户再次复现）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "问题台账 id"},
                "reason": {"type": "string", "description": "为什么重新打开"},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "register_acceptance",
        "description": "登记一个验收项，并分清「机器能自证」还是「必须人拍板」。机器自证的写清楚命令+结果，面板归到「已机器验收」；需要人看的用 needs_human=true，并把「人要怎么复核」写进 steps，面板会单列成「需要你验收」，原样展示步骤。改完一个功能、准备让人确认时用它，别让用户自己去猜要怎么验。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "验收项标题，一句话"},
                "needs_human": {"type": "boolean", "description": "true=必须人拍板（审美/手感/真实产品里点一下）；false=机器自证"},
                "steps": {"type": "string", "description": "人要怎么复核，如“1) 打开面板 2) 看第一屏 3) 确认不挤”"},
                "level": {"type": "string", "description": "优先级，默认 P1"},
                "lane": {"type": "string", "description": "归属分支，可选"},
                "status": {"type": "string", "description": "todo / wip / verified，默认 todo"},
                "evidence": {"type": "string", "description": "机器自证的真实命令+结果，可选"},
            },
            "required": ["title"],
            "additionalProperties": False,
        },
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
        "description": "自动挡的当前状态：总开关 on/draining/off/error、自动开关、Codex 原生 task/worktree 映射（仅已有真实记录）、正在做什么和需要处理的事项。面板用它渲染；fast=true 只读缓存。",
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
        "description": "控制自动挡总开关与运行状态。disable 关闭外挂并拒绝新自动工作，enable 恢复；pause 暂停推进但允许安全收尾，resume 恢复；其余 action 控制自动动作和后台常驻。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["enable", "disable", "pause", "resume", "run_once", "set_auto_land",
                             "set_auto_resolve", "set_auto_port", "set_auto_exec",
                             "enable_daemon", "disable_daemon"],
                },
                "enabled": {"type": "boolean", "description": "set_auto_* 的目标值"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_native_threads",
        "description": "只读查看 Codex 对话与窗口集合；优先读取独立 app-server，连接不可用时降级到本地只读历史。独立 app-server 不保证等同于当前桌面窗口实时状态。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "codex_native_probe",
        "description": "真跑一次「原生直连」自检：桌面端控制 socket 通不通、能不能把任务派进 Codex 窗口。结果会记下来，面板显示的是这次实测结果而不是猜测。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "codex_native_create_section",
        "description": "通过 Codex app-server 创建原生 thread section；属于显式写操作，自动挡关闭时拒绝。独立 app-server 不保证立即同步到当前桌面窗口。",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_native_start_thread",
        "description": "通过 Codex app-server 创建对话窗口。必须提供真实项目 cwd；可选 section_id 指定 Codex 原生窗口集合。该接口的结果不保证立即出现在当前桌面窗口。",
        "inputSchema": {
            "type": "object",
            "properties": {"cwd": {"type": "string"}, "section_id": {"type": "string"}},
            "required": ["cwd"],
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_native_move_thread",
        "description": "通过 Codex thread/section/move 调整真实对话窗口的集合归属；section_id 为空表示移出集合。独立 app-server 不保证立即同步到当前桌面窗口。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "section_id": {"type": ["string", "null"]},
                "before_thread_id": {"type": "string"},
            },
            "required": ["thread_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_native_dispatch_task",
        "description": "创建一个真实 Codex 原生子对话并立即用 turn/start 派活；成功后把 thread/turn 与自动挡台账关联。未指定窗口集合时自动归入本项目的集合（同名复用，不重复创建）；集合操作失败不拦派活，但会如实记进路线记录。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ledger_id": {"type": "string"},
                "cwd": {"type": "string"},
                "prompt": {"type": "string"},
                "section_id": {"type": "string"},
            },
            "required": ["ledger_id", "cwd", "prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_native_follow_up",
        "description": "向已关联的 Codex 原生子对话继续派活；这是 follow-up，不创建第二份任务账本。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "prompt": {"type": "string"},
            },
            "required": ["thread_id", "prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "render_autopilot_panel",
        "description": "打开自动挡面板（inline 卡片 / 对话侧栏 / 全局侧栏 / 全屏）。面板第一屏用大白话说明：它是什么、怎么用（普通 Codex 输入框就是总输入框，不用新建对话或分支）、它替你做了什么、现在在做什么、你需要做什么、随时可暂停。用户问“自动挡在哪看 / 怎么用 / 现在开着吗”时用它。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "_meta": {
            "ui": {"resourceUri": PANEL_URI},
            # 只放 entrypoint 规范认识的键：openai/ui 下多一个未知字段，
            # 整份 metadata 会被宿主判为 invalid 并丢掉 entrypoint（入口就不见了）。
            # 显示模式属于资源侧（resources/read 的 contents[]._meta），不在这里写。
            "openai/ui": {
                "entrypoints": [{"type": "global"}, {"type": "thread"}],
                "preferredModelDisplayMode": "inline",
            },
            "openai/toolInvocation/invoking": "打开自动挡面板…",
            "openai/toolInvocation/invoked": "自动挡面板已打开。",
        },
    },
    {
        "name": "autopilot_run_once",
        "description": "立刻检查一轮：扫描可落盘分支、已通过门禁的改动与冲突队列，该合的合、该收的收。暂停中会拒绝执行。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "autopilot_settings_read",
        "description": "读取自动挡的设置：类型、当前值与分组。由宿主在插件设置页渲染成原生控件。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
        "outputSchema": {
            "type": "object",
            "properties": {
                "schema": {"type": "object"},
                "values": {"type": "object"},
                "layout": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["schema", "values"],
        },
    },
    {
        "name": "autopilot_settings_update",
        "description": "更新自动挡设置。宿主只会把发生变化的项传进来。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "set": {
                    "type": "object",
                    "properties": {
                        "enabled": {"type": "boolean"},
                        "paused": {"type": "boolean"},
                        "auto_land": {"type": "boolean"},
                        "auto_resolve": {"type": "boolean"},
                        "auto_port": {"type": "boolean"},
                        "auto_exec": {"type": "boolean"},
                        "daemon_enabled": {"type": "boolean"},
                    },
                    "minProperties": 1,
                    "additionalProperties": False,
                },
            },
            "required": ["set"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {"values": {"type": "object"}},
            "required": ["values"],
        },
    },
]

# 设置项：宿主在插件设置页用原生控件渲染，本 server 负责读值、写值、持久化。
# 这些开关本来就存在 control_state.json 里（用户级、不进版本库），这里只是把它们
# 从「只有面板上那排自绘开关能改」变成「宿主设置页也能改」。
SETTINGS_READ_TOOL = "autopilot_settings_read"
SETTINGS_UPDATE_TOOL = "autopilot_settings_update"

SETTINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "enabled": {
            "type": "boolean",
            "title": "启用 Codex 自动挡",
            "description": "关闭后停止自动挡的 intake、调度与后台动作；已有任务和工作区保留。",
        },
        "paused": {
            "type": "boolean",
            "title": "暂停自动挡",
            "description": "暂停后不再启动新工作；正在跑的 Git 操作会安全收尾。",
        },
        "auto_land": {
            "type": "boolean",
            "title": "自动合入安全改动",
            "description": "门禁通过且无冲突的分支，直接合进主分支并同步引用。",
        },
        "auto_resolve": {
            "type": "boolean",
            "title": "自动收拢冲突分支",
            "description": "把有冲突的分支拆成可执行任务逐条收口，不逐条问用户。",
        },
        "auto_port": {
            "type": "boolean",
            "title": "自动移植小改动",
            "description": "把体量很小、明显安全的在途改动搬到主干上。",
        },
        "auto_exec": {
            "type": "boolean",
            "title": "自动实现需求",
            "description": "按台账里的未决问题，在独立 worktree 里起真实会话实现并跑门禁。会动代码、会花钱。",
        },
        "daemon_enabled": {
            "type": "boolean",
            "title": "后台常驻",
            "description": "用系统自带的常驻机制定时检查；关掉后只在对话里手动触发。",
        },
    },
    "required": ["enabled", "paused", "auto_land", "auto_resolve", "auto_port", "auto_exec",
                 "daemon_enabled"],
}

SETTINGS_LAYOUT = [
    {"kind": "group", "title": "运行", "items": [
        {"kind": "property", "property": "enabled"},
        {"kind": "property", "property": "paused"},
        {"kind": "tool", "tool": "autopilot_run_once",
         "description": "立刻检查一轮，不改任何开关"},
    ]},
    {"kind": "group", "title": "自动动作", "items": [
        {"kind": "property", "property": "auto_land"},
        {"kind": "property", "property": "auto_resolve"},
        {"kind": "property", "property": "auto_port"},
        {"kind": "property", "property": "auto_exec"},
    ]},
    {"kind": "group", "title": "后台", "items": [
        {"kind": "property", "property": "daemon_enabled"},
    ]},
]


def _json_file(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _daemon_state():
    return _json_file(RUNTIME / "state.json")


def _daemon_live():
    """从 launchd 读取存活状态和绑定项目，不用受沙箱限制的 kill(pid, 0)。"""
    uid = os.getuid() if hasattr(os, "getuid") else 0
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/com.codex-autopilot.daemon"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, None, None
    if result.returncode != 0:
        return False, None, None
    text = result.stdout or ""
    state = re.search(r"(?m)^\s*state = (\w+)\s*$", text)
    pid_match = re.search(r"(?m)^\s*pid = (\d+)\s*$", text)
    repo_match = re.search(r"(?m)^\s*KG_REPO => (.+?)\s*$", text)
    pid = int(pid_match.group(1)) if pid_match else None
    bound_repo = str(Path(repo_match.group(1)).expanduser().resolve()) if repo_match else None
    return bool(state and state.group(1) == "running" and pid), pid, bound_repo


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


def _open_ledger_count():
    """还没关闭的问题数（bug/视觉/体验/架构），按 id 取最新状态。"""
    repo = os.environ.get("KG_REPO")
    if not repo:
        return 0
    ledger = Path(repo) / "ops" / "truth" / "ledger.jsonl"
    latest = {}
    try:
        lines = ledger.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("id"):
            latest[rec["id"]] = rec
    return sum(1 for r in latest.values() if r.get("status") in ("open", "doing"))


def _truth_dir():
    repo = os.environ.get("KG_REPO") or str(REPO)
    return Path(repo) / "ops" / "truth"


def _roadmap():
    """mainline.json 的产品路线：每一步到哪了。只读、秒级。"""
    ml = _json_file(_truth_dir() / "mainline.json")
    rows = []
    for it in (ml.get("roadmap") or []):
        if isinstance(it, str):
            rows.append({"step": it, "expected": "", "status": "todo"})
        elif isinstance(it, dict):
            rows.append({"step": it.get("step", ""), "expected": it.get("expected", ""),
                         "status": it.get("status") or "todo"})
    return rows


def _project_folder_project_ids():
    """Resolve registry entries to the local project roots that own their truth."""
    raw = _json_file(_truth_dir() / "project_folders.json")
    folders = raw.get("folders") if isinstance(raw, dict) else []
    out = []
    if not isinstance(folders, list):
        return out
    for item in folders:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not path:
            continue
        try:
            root = Path(str(path)).expanduser().resolve()
        except (OSError, ValueError):
            continue
        out.append({
            "id": item.get("id") or root.name,
            "name": item.get("name") or root.name,
            "path": str(root),
            "active": bool(item.get("active")),
            "product": item.get("product") or root.name,
            "one_liner": item.get("one_liner") or "",
            "stage": item.get("stage") or "",
            "source_thread_id": item.get("source_thread_id"),
        })
    return out


def _read_project_truth(root):
    """Read one project folder without mutating either the current or foreign tree."""
    root = Path(root)
    truth = root / "ops" / "truth"
    latest = {}
    for record in _read_jsonl(truth / "ledger.jsonl"):
        if record.get("id"):
            latest[record["id"]] = record
    opens = [r for r in latest.values() if r.get("status") in ("open", "doing")]
    doing = [r for r in opens if r.get("status") == "doing"]
    todo = [r for r in opens if r.get("status") != "doing"]

    acc_latest = {}
    for record in _read_jsonl(truth / "acceptance.jsonl"):
        if record.get("id"):
            acc_latest[record["id"]] = record
    acc = list(acc_latest.values())
    human = [r for r in acc
             if r.get("needs_human") and r.get("status") in ("todo", "wip", "blocked")]
    machine = [r for r in acc if r.get("status") == "verified" and not r.get("needs_human")]
    route_latest = {}
    for row in _read_jsonl(truth / "native_routes.jsonl"):
        if row.get("ledger_id"):
            route_latest[row["ledger_id"]] = row
    route_tasks = list(route_latest.values())

    ml = _json_file(truth / "mainline.json")
    mainline_product = ml.get("product") or ""
    mainline_one_liner = ml.get("one_liner") or ""
    mainline_stage = ml.get("stage") or ""
    road = []
    for item in (ml.get("roadmap") or []):
        if isinstance(item, str):
            road.append({"step": item, "expected": "", "status": "todo"})
        elif isinstance(item, dict):
            road.append({"step": item.get("step", ""), "expected": item.get("expected", ""),
                         "status": item.get("status") or "todo"})
    current_step = next((r for r in road if r.get("status") == "doing"), None)
    if not current_step:
        current_step = next((r for r in road if r.get("status") == "next"), None)
    current_step = current_step or next((r for r in road if r.get("status") != "done"), None)
    return {
        "roadmap": road,
        "current_step": (current_step or {}).get("step") or "路线待拆解",
        "current_step_status": (current_step or {}).get("status") or "todo",
        "doing_items": doing,
        "todo_items": todo,
        "human_acceptance": human,
        "machine_verified": machine,
        "route_tasks": route_tasks,
        "product": mainline_product,
        "one_liner": mainline_one_liner,
        "stage": mainline_stage,
    }


def _project_folders(fast=True):
    """项目文件夹：把「项目 → 产品路线 → 工作项 → 验收」归一成面板能扫描的一层。

    当前绑定项目永远会出现；`ops/truth/project_folders.json` 里的其他项目只读它们
    自己的 ops/truth，绝不写回，也不把别的项目工作项混进当前项目。读不到某个项目
    账本时，该文件夹保留并明确标成无法读取，不静默显示 0。
    """
    current = str(Path(os.environ.get("KG_REPO") or REPO).expanduser().resolve())
    ml = _mainline_summary()
    board = _acceptance_board()
    road = _roadmap()
    current_meta = next((m for m in _project_folder_project_ids()
                         if m.get("path") == current), {})
    current_step = next((r for r in road if r.get("status") == "doing"), None)
    if not current_step:
        current_step = next((r for r in road if r.get("status") == "next"), None)
    current_step = current_step or next((r for r in road if r.get("status") != "done"), None)

    route_tasks = []
    for row in (_route_summary(timeout=0.0 if fast else None).get("tasks") or []):
        route_tasks.append({
            "id": row.get("id"),
            "title": row.get("title") or "",
            "status": row.get("status"),
            "lane": row.get("lane"),
            "branch": row.get("branch"),
            "worktree": row.get("worktree"),
            "thread_id": row.get("thread_id") or ((row.get("native_route") or {}).get("thread_id")),
        })

    def summarize_folder(meta, data):
        doing = data.get("doing_items") or []
        todo = data.get("todo_items") or []
        human = data.get("human_acceptance") or []
        machine = data.get("machine_verified") or []
        return {
            "id": meta.get("id") or Path(meta.get("path") or current).name,
            "name": meta.get("name") or Path(meta.get("path") or current).name,
            "kind": "project",
            "path": meta.get("path") or current,
            "active": bool(meta.get("active") or (meta.get("path") or current) == current),
            "product": data.get("product") or meta.get("product") or Path(meta.get("path") or current).name,
            "one_liner": data.get("one_liner") or meta.get("one_liner") or "",
            "stage": data.get("stage") or meta.get("stage") or "",
            "source_thread_id": meta.get("source_thread_id"),
            "current_step": data.get("current_step") or "路线待拆解",
            "current_step_status": data.get("current_step_status") or "todo",
            "roadmap": data.get("roadmap") or [],
            "open_items": doing + todo,
            "doing_items": doing,
            "todo_items": todo,
            "human_acceptance": human,
            "machine_verified": machine,
            "route_tasks": data.get("route_tasks") or [],
            "counts": {
                "doing": len(doing),
                "todo": len(todo),
                "human": len(human),
                "machine": len(machine),
            },
            "readable": data.get("readable", True),
            "read_error": data.get("read_error"),
        }

    current_folder = summarize_folder({
        "id": Path(current).name or "current",
        "name": Path(current).name or "当前项目",
        "path": current,
        "active": True,
        "product": ml.get("product") or Path(current).name,
        "one_liner": ml.get("one_liner") or "",
        "stage": ml.get("stage") or "",
        "source_thread_id": current_meta.get("source_thread_id"),
    }, {
        "roadmap": road,
        "current_step": (current_step or {}).get("step") or "路线待拆解",
        "current_step_status": (current_step or {}).get("status") or "todo",
        "doing_items": board.get("doing") or [],
        "todo_items": board.get("todo") or [],
        "human_acceptance": board.get("human") or [],
        "machine_verified": board.get("machine") or [],
        "route_tasks": route_tasks,
    })

    folders = [current_folder]
    seen = {current}
    for meta in _project_folder_project_ids():
        path = meta.get("path")
        if not path or path in seen:
            continue
        seen.add(path)
        truth_dir = Path(path) / "ops" / "truth"
        if not truth_dir.is_dir():
            data = {
                "readable": False,
                "read_error": "没有找到 ops/truth，无法读取项目账本",
                "roadmap": [],
                "current_step": "项目账本不可读",
                "current_step_status": "blocked",
                "doing_items": [],
                "todo_items": [],
                "human_acceptance": [],
                "machine_verified": [],
                "route_tasks": [],
            }
        else:
            try:
                data = _read_project_truth(path)
                data["readable"] = True
            except OSError as exc:
                data = {
                    "readable": False,
                    "read_error": str(exc),
                    "roadmap": [],
                    "current_step": "项目账本读取失败",
                    "current_step_status": "blocked",
                    "doing_items": [],
                    "todo_items": [],
                    "human_acceptance": [],
                    "machine_verified": [],
                    "route_tasks": [],
                }
        folders.append(summarize_folder(meta, data))

    return {
        "source": "ops/truth/project_folders.json + 当前绑定项目",
        "count": len(folders),
        "folders": folders,
    }


def _read_jsonl(path):
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _native_routes_path():
    return _truth_dir() / "native_routes.jsonl"


def _auto_section_name():
    """派活时自动归入的窗口集合名：按绑定项目分桶。

    用户要的是「窗口拉太多时自动给它们一个文件夹」。名字里带项目名，
    多个项目的子对话不会混进同一个集合；同名集合会被复用，不重复创建。
    """
    base = codex_native.DEFAULT_SECTION_NAME
    try:
        project = Path(os.environ.get("KG_REPO") or REPO).expanduser().resolve().name
    except (OSError, ValueError):
        project = ""
    return "%s · %s" % (base, project) if project else base


def _append_native_route(ledger_id, result, status):
    """Persist host linkage metadata without creating a second task ledger."""
    path = _native_routes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    turn = result.get("turn") or {}
    record = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ledger_id": ledger_id,
        "status": status,
        "thread_id": result.get("thread_id"),
        "turn_id": result.get("turn_id") or turn.get("id")
        or (turn.get("turn") or {}).get("id") or result.get("turnId"),
        "section_id": result.get("section_id"),
        "section_note": result.get("section_note"),
        "cwd": result.get("cwd"),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def _ledger_item(ledger_id):
    latest = {}
    for record in _read_jsonl(_truth_dir() / "ledger.jsonl"):
        if record.get("id"):
            latest[record["id"]] = record
    item = latest.get(ledger_id)
    if item and item.get("status") in ("open", "doing", "todo", "wip", "blocked"):
        return item
    return None


def _native_route_map():
    latest = {}
    for record in _read_jsonl(_native_routes_path()):
        if record.get("ledger_id"):
            latest[record["ledger_id"]] = record
    return latest


def _recent_done(landed, limit=5):
    """最近完成了什么：真相层里已收口的问题 + 守护进程刚合入的分支。

    以前这一行只显示「自动合入过的分支」，在没有常驻守护、只是走完台账闭环的项目里
    永远是空的；可用户问「最近完成了什么」，问的是这个项目最近真正收口了什么事。
    """
    rows = []
    latest = {}
    for r in _read_jsonl(_truth_dir() / "ledger.jsonl"):
        if r.get("id"):
            latest[r["id"]] = r
    for r in latest.values():
        if r.get("status") in ("done", "wontfix"):
            title = " ".join((r.get("title") or "").split())
            rows.append((r.get("ts") or "", f"{r.get('id')} 已收口：{title[:70]}"))
    for item in (landed or []):
        if isinstance(item, dict):
            name = item.get("branch") or item.get("target") or item.get("id") or ""
            when = item.get("ts") or item.get("at") or ""
        else:
            name, when = str(item), ""
        if name:
            rows.append((when, f"{name} 已合入"))
    rows.sort(key=lambda pair: pair[0], reverse=True)
    seen, out = set(), []
    for _, text in rows:
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _acceptance_board():
    """验收分两堆：需要人拍板的、机器已自证的。顺带分出未决问题的 doing/todo。"""
    latest = {}
    for r in _read_jsonl(_truth_dir() / "ledger.jsonl"):
        if r.get("id"):
            latest[r["id"]] = r
    opens = [r for r in latest.values() if r.get("status") in ("open", "doing")]
    opens.sort(key=lambda r: (0 if r.get("kind") in ("bug", "gate") else 1, r.get("ts", "")))
    doing = [r for r in opens if r.get("status") == "doing"]
    todo = [r for r in opens if r.get("status") != "doing"]

    acc_latest = {}
    for r in _read_jsonl(_truth_dir() / "acceptance.jsonl"):
        if r.get("id"):
            acc_latest[r["id"]] = r
    acc = list(acc_latest.values())
    human = [r for r in acc
             if r.get("needs_human") and r.get("status") in ("todo", "wip", "blocked")]
    machine = [r for r in acc if r.get("status") == "verified" and not r.get("needs_human")]
    human_done = [r for r in acc if r.get("status") == "verified" and r.get("needs_human")]

    def brief(r):
        return {"id": r.get("id"), "kind": r.get("kind"), "title": r.get("title", ""),
                "status": r.get("status"), "lane": r.get("lane"),
                "project_id": r.get("project_id"), "route_id": r.get("route_id"),
                "work_item_id": r.get("work_item_id"),
                "source_thread_id": r.get("source_thread_id") or r.get("thread_id")}

    def acc_brief(r):
        return {"id": r.get("id"), "title": r.get("title", ""), "level": r.get("level"),
                "status": r.get("status"), "steps": r.get("steps") or "", "lane": r.get("lane"),
                "project_id": r.get("project_id"), "route_id": r.get("route_id"),
                "work_item_id": r.get("work_item_id"),
                "source_thread_id": r.get("source_thread_id") or r.get("thread_id")}

    return {
        "doing": [brief(r) for r in doing[:8]],
        "todo": [brief(r) for r in todo[:8]],
        "todo_count": len(todo),
        "human": [acc_brief(r) for r in human[:8]],
        "machine": [acc_brief(r) for r in machine[:8]],
        "human_done_count": len(human_done),
        "machine_count": len(machine),
    }


def _mainline_summary(limit=6):
    """产品主线摘要：方向来自 mainline.json，未决问题来自台账。只读、秒级。"""
    base = _truth_dir().parent.parent
    ml = _json_file(_truth_dir() / "mainline.json")
    latest = {}
    try:
        raw = (_truth_dir() / "ledger.jsonl").read_text(encoding="utf-8")
    except OSError:
        raw = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("id"):
            latest[rec["id"]] = rec
    opens = [r for r in latest.values() if r.get("status") in ("open", "doing")]
    opens.sort(key=lambda r: (0 if r.get("kind") in ("bug", "gate") else 1, r.get("ts", "")))
    doc = base / "docs" / "PRODUCT_MAINLINE.md"
    return {
        "product": ml.get("product"),
        "one_liner": ml.get("one_liner"),
        "stage": ml.get("stage"),
        "north_star": ml.get("north_star"),
        "focus": (ml.get("current_focus") or [])[:4],
        "path": ml.get("path") or [],
        "roadmap": _roadmap(),
        "doc": str(doc.relative_to(base)) if doc.exists() else None,
        "open_count": len(opens),
        "open_items": [{"id": r.get("id"), "kind": r.get("kind"),
                        "title": r.get("title", ""), "status": r.get("status")}
                       for r in opens[:limit]],
    }


def _route_summary(limit=20, timeout=None):
    """Merge the ledger route with the host-owned Codex sections and threads.

    timeout 控制在宿主上查会话的预算：面板/开关那条路给很短的预算，
    免得桌面宿主不回话时，点一下开关要等上一整段超时。
    """
    latest = {}
    for rec in _read_jsonl(_truth_dir() / "ledger.jsonl"):
        if rec.get("id"):
            latest[rec["id"]] = rec
    bindings = _native_route_map()
    rows = []
    for rec in latest.values():
        if rec.get("status") not in ("open", "doing", "todo", "wip", "blocked"):
            continue
        binding = bindings.get(rec.get("id"), {})
        rows.append({
            "id": rec.get("id"),
            "title": rec.get("title") or rec.get("detail") or "",
            "status": rec.get("status"),
            "lane": rec.get("lane"),
            "branch": rec.get("branch"),
            "worktree": rec.get("worktree"),
            "parent_thread_id": rec.get("parent_thread_id"),
            "thread_id": rec.get("thread_id") or binding.get("thread_id"),
            "thread_group_id": rec.get("thread_group_id"),
            "route_key": rec.get("route_key"),
            "native_route": binding or None,
        })
    rows.sort(key=lambda r: (0 if r.get("status") == "doing" else 1, r.get("id") or ""))
    native = codex_native.snapshot(timeout=timeout)
    thread_by_id = {t.get("id"): t for t in native.get("threads", []) if t.get("id")}
    for row in rows:
        host_thread = thread_by_id.get(row.get("thread_id"))
        if host_thread:
            row["native_thread"] = host_thread
    return {
        "commander": {"kind": "autopilot", "label": "主司令窗"},
        "source": "ops/truth/ledger.jsonl",
        "thread_api": "available" if native.get("available") else "unavailable",
        "native": native,
        "thread_note": (
            "数据来自独立 Codex app-server；不代表当前桌面宿主窗口的实时状态。自动挡只保存已有真实关联。"
            if native.get("available") else
            "Codex 原生 app-server 当前不可用；窗口仅从宿主历史只读展示，不虚构实时窗口。"
        ),
        "tasks": rows[:limit],
    }


def _product_state(limit=25):
    """产品今天是怎么工作的：按模块读 product_state.json。"""
    data = _json_file(_truth_dir() / "product_state.json")
    modules = data.get("modules") if isinstance(data.get("modules"), list) else []
    return {"updated": data.get("updated"), "count": len(modules), "modules": modules[:limit]}


def _active_judgments(limit=12):
    """判断逻辑台账（只取 active），供面板展示「同一个判断人只做一次」。"""
    out = []
    try:
        raw = (_truth_dir() / "judgments.jsonl").read_text(encoding="utf-8")
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("status") == "active":
            out.append({"id": rec.get("id"), "scope": rec.get("scope"),
                        "statement": rec.get("statement") or rec.get("title", "")})
    return out[-limit:]


def _last_verification():
    """最近一次落盘的真实验收证据：acceptance.jsonl 里最后一条带 evidence 的记录。"""
    last = None
    try:
        raw = (_truth_dir() / "acceptance.jsonl").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("evidence"):
            last = rec
    if not last:
        return None
    return {"id": last.get("id"), "title": last.get("title"), "status": last.get("status"),
            "lane": last.get("lane"), "ts": last.get("ts"), "evidence": last.get("evidence")}


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


def _running_exec():
    """此刻 auto_exec 是否真的派了一个独立 codex 会话在改代码；没有则 None。

    常驻进程同步等 exec 结束，这期间 state.json 不动，面板会误报「没有分支可合」。
    这里从 daemon.log 的派发/退出配对里读出「正在实现哪条需求」，并补上它的标题。
    """
    if exec_status is None or not control_state.load().get("auto_exec", False):
        return None
    try:
        info = exec_status.running()
    except Exception:
        return None
    if not info:
        return None
    rid = info.get("id")
    for r in _read_jsonl(_truth_dir() / "ledger.jsonl"):
        if r.get("id") == rid:
            _t = (r.get("title") or "").strip()
            if len(_t) > 36:
                _t = _t[:36] + "…"
            info["title"] = _t
            info["kind"] = r.get("kind") or ""
            break
    info.setdefault("title", "")
    info.setdefault("kind", "")
    return info


def _native_status():
    """原生直连状态：socket 在不在是即时事实；能不能派活只看已跑过的自检结果。

    没跑过自检就说没跑过，不去猜「应该连得上」——这个区分是为了让「已连通」
    永远是一条实测结论，而不是推断。
    """
    state = dict(codex_native.socket_state())
    probe = codex_native.last_probe()
    state["last_probe"] = probe
    if probe is None:
        state["label"] = ("原生直连：未自检（点「原生自检」跑一次）"
                          if state.get("socket_present") else "原生直连：Codex 未运行")
    elif probe.get("connected"):
        state["label"] = "原生直连：已连通，可派活到 Codex 窗口"
    else:
        state["label"] = "原生直连：不可派活，只能只读历史"
    return state


def _status_payload(fast=True):
    """把控制开关 + 缓存状态 + 项目计数，拼成面板和模型都能用的一个 JSON。"""
    from datetime import datetime, timezone
    ctrl = control_state.load()
    job_running, pid, daemon_project = _daemon_live()
    current_project = str(Path(os.environ.get("KG_REPO") or REPO).expanduser().resolve())
    live = bool(job_running and daemon_project == current_project)
    st = _daemon_state() if live else {}
    summary = st.get("last_summary") or {}
    board = _acceptance_board()
    failures = [{"target": t, "kind": k} for t, k in (st.get("last_failures") or [])]
    rtasks = _active_resolve_tasks()
    blocked = st.get("resolve_blocked") or {}
    port_blocked = st.get("port_blocked") or {}
    paused = bool(ctrl.get("paused"))
    enabled = bool(ctrl.get("enabled", True))
    lifecycle = control_state.lifecycle_status(ctrl)

    exec_now = _running_exec() if live else None
    if ctrl.get("error"):
        state_key, state_label = "error", "状态异常，已安全暂停"
    elif not enabled:
        state_key, state_label = "off", "自动挡已关闭，Codex 已恢复原生状态"
    elif paused:
        state_key, state_label = "paused", "已暂停"
    elif exec_now:
        state_key, state_label = "working", "正在自动实现一条需求"
    elif failures:
        state_key, state_label = "blocked", "有分支需要你拍板"
    elif board["human"]:
        state_key, state_label = "blocked", "需要你验收"
    elif not live:
        state_key, state_label = "session_only", (
            "当前项目未连接常驻守护"
            if job_running and daemon_project and daemon_project != current_project
            else "仅会话内运行（常驻未开）"
        )
    elif summary.get("clean") or summary.get("merged"):
        state_key, state_label = "working", "正在检查可合入的分支"
    else:
        state_key, state_label = "idle", "运行中，暂无待办"

    # 大白话的「正在做什么 / 为什么 / 最近做了什么 / 需要你做什么」。
    if not enabled:
        doing = "自动挡已关闭；Codex 原生会话、项目与工作区保持不变"
        why = "总开关关闭后不再接收或启动自动挡工作"
    elif paused:
        doing = "暂停中，没有在动任何分支"
        why = "你按了暂停。恢复后它才会继续检查、合入"
    elif exec_now:
        _mins = int((exec_now.get("elapsed_sec") or 0) // 60)
        _nm = exec_now.get("id") or ""
        _ti = exec_now.get("title") or ""
        doing = ("正在自动实现 " + _nm + ("（" + _ti + "）" if _ti else "")
                 + "，已跑 %d 分钟：在一个独立工作区里改代码，自测通过后自动合入 main" % _mins)
        why = "这条需求已经归好类、排在队首；自动实现开着，所以它自己动手，不用你盯"
    elif failures:
        tgt, kind = failures[0]["target"], failures[0]["kind"]
        doing = f"卡在 {tgt}（{kind}），已停下等你决定"
        why = "这类改动要么改了产品方向、要么门禁过不了，按约定不自动合"
    elif board["human"]:
        title = board["human"][0].get("title") or "一项已完成功能"
        doing = f"已完成 {title}，现在停下来等你验收"
        why = "这一步需要真实桌面操作或产品判断，自动挡不会替你假装通过"
    elif summary.get("clean"):
        doing = f"正在把 {summary.get('clean')} 个和主线无冲突的分支逐个合入"
        why = "它们和 main 无冲突、验证门禁已过，属于可以安全自动合入的那一类"
    elif rtasks:
        doing = f"正在处理 {len(rtasks)} 条冲突，拆成可执行任务后逐条收口"
        why = "冲突不丢给你逐条裁决，先自动拆成任务，解完再走同一道门禁"
    elif job_running and daemon_project and daemon_project != current_project:
        doing = "后台守护正在服务另一个项目；当前项目的任务与进度不会混入它的队列"
        why = "常驻守护按项目绑定，面板只读取当前项目自己的账本"
    else:
        doing = "已检查一轮，当前没有能安全自动合入的分支"
        why = "和 main 冲突的、或门禁没过的，不会被自动合，等你处理"

    last = st.get("last_landed") or []
    if last:
        recent = f"最近自动合入 {len(last)} 个分支：{', '.join(last[:3])}" + (
            " 等" if len(last) > 3 else "")
    else:
        recent = "还没有自动合入过分支"

    if not enabled:
        need_action = "需要继续使用自动挡时，重新开启总开关"
    elif failures:
        need_action = f"需要你拍板：{failures[0]['target']}（{failures[0]['kind']}）"
    elif board["human"]:
        need_action = f"需要你验收：{board['human'][0].get('title') or '请打开面板查看验收步骤'}"
    elif ctrl.get("error"):
        need_action = "控制文件异常，去高级面板看一眼或恢复默认"
    else:
        need_action = "不需要你做什么，继续照常提需求就行"

    payload = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "fast": bool(fast),
        "paused": paused,
        "enabled": enabled,
        "lifecycle": lifecycle,
        "error": ctrl.get("error"),
        "state": state_key,
        "state_label": state_label,
        "project": {
            "path": current_project,
            "name": Path(current_project).name,
        },
        "doing": doing,
        "why": why,
        "recent": recent,
        "need_action": need_action,
        "recent_completed": _recent_done(last, 5),
        "switches": {
            "auto_land": bool(ctrl.get("auto_land", True)),
            "auto_resolve": bool(ctrl.get("auto_resolve", True)),
            "auto_port": bool(ctrl.get("auto_port", False)),
            "auto_exec": bool(ctrl.get("auto_exec", False)),
            "daemon_enabled": bool(ctrl.get("daemon_enabled", True)),
        },
        "daemon": {
            "live": bool(live),
            "job_running": bool(job_running),
            "pid": pid,
            "project": daemon_project,
            "last_cycle": st.get("last_cycle_iso"),
            "interval_min": st.get("interval_min"),
        },
        "native": _native_status(),
        "counters": {
            "received": _ledger_count(),
            "open_items": _open_ledger_count(),
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
        "exec_now": exec_now,
        "blocked": {
            "failures": failures,
            "resolve": [{"branch": b, "reason": (v or {}).get("reason")}
                        for b, v in list(blocked.items())[:20]],
            "port": [{"branch": b, "reason": (v or {}).get("reason")}
                     for b, v in list(port_blocked.items())[:20]],
            "exec": [{"id": k, "reason": (v or {}).get("reason")}
                     for k, v in list((st.get("exec_blocked") or {}).items())[:20]],
        },
        "resolve_tasks": rtasks[:20],
        "events": control_state.read_events(30),
        "product_state": _product_state(),
        "judgments": _active_judgments(),
        "last_verification": _last_verification(),
        "closure_rate": _closure_rate_summary(),
        "mainline": _mainline_summary(),
        # fast=True 是面板/开关那条路：只读缓存，不连宿主，绝不因此变慢。
        "route": _route_summary(timeout=0.0 if fast else None),
        "roadmap": _roadmap(),
        "project_folders": _project_folders(),
        "doing_items": board["doing"],
        "todo_items": board["todo"],
        "todo_count": board["todo_count"],
        "human_acceptance": board["human"],
        "machine_verified": board["machine"],
        "human_accepted_count": board["human_done_count"],
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
        _closure_rate_line(payload.get("closure_rate")),
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
    en = payload.get("exec_now")
    if en:
        _m = int((en.get("elapsed_sec") or 0) // 60)
        lines.append("执行中：%s %s（已 %d 分钟，独立工作区改代码，自测通过后自动合入）"
                     % (en.get("id") or "", en.get("title") or "", _m))
    ps = payload.get("product_state") or {}
    if ps.get("count"):
        lines.append("产品状态：已记录 %s 个模块行为（最后更新 %s）"
                     % (ps["count"], ps.get("updated") or "—"))
    counters = payload.get("counters") or {}
    if counters.get("open_items") is not None:
        lines.append("未关闭问题：%s 条（修完会自动从台账里收口）" % counters.get("open_items"))
    jd = payload.get("judgments") or []
    if jd:
        lines.append("判断逻辑：已蒸馏 %s 条（同一个判断人只做一次）" % len(jd))
    lv = payload.get("last_verification")
    if lv:
        first = (lv.get("evidence") or "").splitlines()
        lines.append("最近验证：%s（%s）" % (lv.get("title", ""),
                                            first[0] if first else "无证据"))
    road = payload.get("roadmap") or []
    if road:
        mark = {"done": "\u2713", "doing": "\u25cf", "next": "\u25cf", "todo": "\u25cb"}
        seg = " ".join("%s%s" % (mark.get(r.get("status"), "\u25cb"), r.get("step", ""))
                       for r in road[:6])
        lines.append("产品路线：" + seg)
    hum = payload.get("human_acceptance") or []
    if hum:
        lines.append("需要你验收：%s 项（如 %s）"
                     % (len(hum), hum[0].get("title", "")))
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
    if action == "disable":
        control_state.set_keys(enabled=False)
        control_state.append_event("disable")
        return _with_status("自动挡已关闭；Codex 原生功能和现有工作区未改动。"), 0
    if action == "enable":
        control_state.set_keys(enabled=True)
        control_state.append_event("enable")
        return _with_status("自动挡已开启；保留的任务与工作区可继续处理。"), 0
    if action == "pause":
        if not control_state.is_enabled():
            return control_state.mutation_guard(), 2
        control_state.set_keys(paused=True)
        control_state.append_event("pause")
        return _with_status("已暂停：不再启动新工作；当前 Git 操作会安全收尾。"), 0
    if action == "resume":
        if not control_state.is_enabled():
            return control_state.mutation_guard(), 2
        control_state.set_keys(paused=False)
        control_state.append_event("resume")
        return _with_status("已恢复运行：下一轮会继续检查、合入。"), 0
    if action == "run_once":
        if not control_state.is_enabled():
            return control_state.mutation_guard(), 2
        if control_state.is_paused():
            return "当前处于暂停中，不会偷偷跑一轮。先恢复运行再试。", 2
        daemon = OPS_DIR / "central_daemon.py"
        control_state.append_event("run_once")
        out, rc = _run([str(daemon), "--once"], timeout=2400)
        return _with_status("已跑完一轮：\n" + out), rc
    if action.startswith("set_") and not action.startswith("set_auto_"):
        return "未知 action：%s" % action, 2
    if action in ("set_auto_land", "set_auto_resolve", "set_auto_port",
                   "set_auto_exec"):
        if not control_state.is_enabled():
            return control_state.mutation_guard(), 2
        if not isinstance(enabled, bool):
            return "%s 需要 enabled（true/false）" % action, 2
        key = action[len("set_"):]
        control_state.set_keys(**{key: enabled})
        control_state.append_event("switch", name=key, value=enabled)
        return _with_status("%s 已%s。" % (key, "打开" if enabled else "关闭")), 0
    if action in ("enable_daemon", "disable_daemon"):
        if not control_state.is_enabled():
            return control_state.mutation_guard(), 2
        daemon = OPS_DIR / "central_daemon.py"
        want = action == "enable_daemon"
        out, rc = _run([str(daemon), "--install" if want else "--uninstall"], timeout=120)
        if rc == 0:
            control_state.set_keys(daemon_enabled=want)
            control_state.append_event("daemon", enabled=want)
        return _with_status(out or ("已%s常驻" % ("开启" if want else "关闭"))), rc
    return "未知 action：%s" % action, 2


def _settings_values():
    """当前开关值（只看 6 个 boolean，滤掉 load() 附带的状态字段）。"""
    cfg = control_state.load()
    return {k: bool(cfg.get(k, control_state.DEFAULTS[k]))
            for k in control_state.DEFAULTS}


def _settings_read_payload():
    """宿主插件设置页用原生控件渲染：schema + 当前值 + 分组布局。"""
    values = _settings_values()
    return {
        "schema": SETTINGS_SCHEMA,
        "values": values,
        "layout": SETTINGS_LAYOUT,
        "__text__": "自动挡设置：\n" + "\n".join(
            "%s = %s" % (k, "开" if v else "关") for k, v in values.items()),
    }


def _settings_update(set_values):
    """把宿主送来的变更落到用户级 control.json，并回读权威值。"""
    if not isinstance(set_values, dict) or not set_values:
        return "autopilot_settings_update 需要 set（至少一项）", 2
    if "enabled" in set_values and set_values.get("enabled") is False:
        control_state.set_keys(enabled=False)
        control_state.append_event("disable")
        return {"values": _settings_values(),
                "__text__": "自动挡已关闭；Codex 原生功能和现有工作区未改动。"}, 0
    guard = control_state.mutation_guard()
    if guard:
        return guard, 2
    changed = {k: v for k, v in set_values.items()
               if k in control_state.DEFAULTS and isinstance(v, bool)}
    if "enabled" in changed:
        control_state.set_keys(**changed)
        control_state.append_event("settings", **changed)
        return {"values": _settings_values(),
                "__text__": "设置已更新。"}, 0
    if not changed:
        return "set 里没有可识别的开关项", 2
    control_state.set_keys(**changed)
    control_state.append_event("settings", **changed)
    values = _settings_values()
    desc = "、".join("%s→%s" % (k, "开" if v else "关") for k, v in changed.items())
    return {"values": values, "__text__": "设置已更新：" + desc}, 0


def call_tool(name, args):
    args = args or {}
    read_only_tools = {
        "project_status", "next_actions", "product_state", "distill_review",
        "suggest_branch", "resolve_list", "resolve_plan", "generate_context",
        "dashboard", "autopilot_status", "render_autopilot_panel",
        "autopilot_settings_read", "codex_native_threads", "codex_native_probe",
    }
    control_tools = {
        "autopilot_control", "autopilot_run_once",
        "autopilot_settings_update",
    }
    native_mutation_tools = {
        "codex_native_create_section", "codex_native_start_thread",
        "codex_native_move_thread", "codex_native_dispatch_task",
        "codex_native_follow_up",
    }
    if name in native_mutation_tools:
        guard = control_state.mutation_guard()
        if guard:
            return guard, 2
        if name == "codex_native_create_section":
            result, error = codex_native.create_section((args.get("name") or "").strip())
        elif name == "codex_native_start_thread":
            cwd = (args.get("cwd") or "").strip()
            result, error = codex_native.start_thread(cwd, args.get("section_id"))
        elif name == "codex_native_dispatch_task":
            ledger_id = (args.get("ledger_id") or "").strip()
            if not _ledger_item(ledger_id):
                return {
                    "code": "AUTOPILOT_LEDGER_ITEM_NOT_DISPATCHABLE",
                    "message": "只能给自动挡台账中仍未收口的问题派活。",
                }, 2
            result, error = codex_native.dispatch_task(
                (args.get("cwd") or "").strip(),
                args.get("prompt"),
                args.get("section_id"),
                auto_section=_auto_section_name(),
            )
            if result:
                _append_native_route(
                    ledger_id,
                    result,
                    status="dispatch_failed" if error else "dispatched",
                )
        elif name == "codex_native_follow_up":
            thread_id = (args.get("thread_id") or "").strip()
            result, error = codex_native.follow_up(
                thread_id,
                args.get("prompt"),
            )
            if not error:
                binding = next(
                    (row for row in _native_route_map().values()
                     if row.get("thread_id") == thread_id),
                    None,
                )
                if binding:
                    _append_native_route(
                        binding["ledger_id"],
                        {"thread_id": thread_id, "turn": result or {},
                         "section_id": binding.get("section_id"),
                         "cwd": binding.get("cwd")},
                        status="follow_up_started",
                    )
        else:
            result, error = codex_native.move_thread(
                (args.get("thread_id") or "").strip(),
                args.get("section_id"),
                (args.get("before_thread_id") or "").strip() or None,
            )
        if error:
            return {"code": "CODEX_NATIVE_UNAVAILABLE", "message": error}, 2
        control_state.append_event("codex_native", action=name, result=result)
        return result, 0
    if name not in read_only_tools | control_tools:
        guard = control_state.mutation_guard()
        if guard:
            return guard, 2
    if name == "project_status":
        return kgctl("status")
    if name == "codex_native_threads":
        return codex_native.snapshot()
    if name == "codex_native_probe":
        # 自检是诊断动作：不碰任务、不碰账本，所以即使总开关关着也允许跑，
        # 否则用户没法在「关着」的时候判断到底是不是权限/环境的问题。
        result = codex_native.probe()
        codex_native.record_probe(result)
        return result
    if name == "capture_request":
        guard = control_state.mutation_guard()
        if guard:
            return guard, 2
        text = (args.get("text") or "").strip()
        if not text:
            return "capture_request 需要 text", 2
        files = args.get("files") or []
        env = {"KG_INBOX_FILES": ":".join(files) if files else None,
               "KG_INBOX_KIND": args.get("kind") or "unknown"}
        return kgctl("inbox", text, extra_env=env)
    if name == "sync_control_plane":
        guard = control_state.mutation_guard()
        if guard:
            return guard, 2
        return kgctl("tick")
    if name == "route_inbox":
        guard = control_state.mutation_guard()
        if guard:
            return guard, 2
        return kgctl("route", *(["--go"] if args.get("confirm") else []))
    if name == "next_actions":
        return truthctl("next")
    if name == "product_state":
        return truthctl("product")
    if name == "product_update":
        area = (args.get("area") or "").strip()
        behavior = (args.get("behavior") or "").strip()
        if not area or not behavior:
            return "product_update 需要 area 和 behavior", 2
        env = {"KG_PS_WHY": args.get("why") or None,
               "KG_PS_FILES": ":".join(args.get("files") or []) or None}
        return truthctl("product", "set", area, behavior, extra_env=env)
    if name == "distill_review":
        return truthctl("distill")
    if name == "distill_add":
        statement = (args.get("statement") or "").strip()
        if not statement:
            return "distill_add 需要 statement", 2
        env = {"KG_TRUTH_WHY": args.get("why") or None,
               "KG_TRUTH_SCOPE": args.get("scope") or None}
        return truthctl("distill", "add", statement, extra_env=env)
    if name == "synthesize_mainline":
        return truthctl("synthesize")
    if name == "close_item":
        rid = (args.get("id") or "").strip()
        if not rid:
            return "close_item 需要 id（如 L-0003）", 2
        env = {"KG_TRUTH_COMMIT": args.get("commit") or None,
               "KG_TRUTH_EVIDENCE": args.get("evidence") or None}
        return truthctl("close", rid, args.get("note") or "", extra_env=env)
    if name in ("register_acceptance", "accept_item"):
        title = (args.get("title") or "").strip()
        if not title:
            return "register_acceptance 需要 title", 2
        argv = ["accept", title]
        if args.get("needs_human"):
            argv.append("--human")
        env = {"KG_TRUTH_STEPS": args.get("steps") or None,
               "KG_TRUTH_LEVEL": args.get("level") or None,
               "KG_TRUTH_LANE": args.get("lane") or None,
               "KG_TRUTH_STATUS": args.get("status") or None,
               "KG_TRUTH_EVIDENCE": args.get("evidence") or None}
        return truthctl(*argv, extra_env=env)
    if name == "reopen_item":
        rid = (args.get("id") or "").strip()
        if not rid:
            return "reopen_item 需要 id（如 L-0003）", 2
        return truthctl("open", rid, args.get("reason") or "")
    if name == "suggest_branch":
        return kgctl("suggest")
    if name == "intake_request":
        guard = control_state.mutation_guard()
        if guard:
            return guard, 2
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
    if name == "autopilot_run_once":
        return control_action("run_once")
    if name == "autopilot_settings_read":
        return _settings_read_payload(), 0
    if name == "autopilot_settings_update":
        return _settings_update(args.get("set") or {})
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
                # 插件设置页：宿主用原生控件渲染，读值走 readTool、写值走 updateTool。
                # 挂错位置或两个工具同名/不存在，宿主会直接丢掉整页设置。
                "experimental": {
                    "openai/settings": {
                        "readTool": SETTINGS_READ_TOOL,
                        "updateTool": SETTINGS_UPDATE_TOOL,
                    },
                },
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
                "改完产品行为的会话要用 product_update 把「产品今天实际怎么表现」写回；"
                "收口一轮后用 distill_review 回看、distill_add 把「为什么这么判断」写回，"
                "让同一个判断人只做一次；每轮收口用 synthesize_mainline 把决策/进展/问题/"
                "验收综合成一页产品主线（docs/PRODUCT_MAINLINE.md）。用户报的 bug 修完、"
                "验收过后用 close_item 关闭台账条目（带 commit/证据），别让它一直挂在 open。"
                "落盘证据看 last_verification（真实命令 + cwd + 结果）。"
                "改完一个功能、准备让人确认时用 register_acceptance 登记验收项：机器能"
                "自证的给证据，需要人拍板的（审美/手感/真实产品里点一下）用 "
                "needs_human=true 并把「人要怎么复核」写进 steps，面板会把人工项原样"
                "列成「需要你验收」——用户不用读代码、不用猜怎么看。产品路线看 "
                "mainline.json 的 roadmap（每步 done/doing/next/todo），autopilot_status "
                "会把它和「当前在做什么 / 还差什么 / 可验收项」一起返回。用户只想提一次"
                "：收到零散需求就用 intake_request 收口，系统自己负责归类、落账、推进到"
                "验收，不要把「该放哪个对话/分支/角色」反过来问用户。"
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
            # 显示模式声明在资源侧（openai/ui.availableDisplayModes），
            # 让宿主在 app 初始化前就能选 inline / fullscreen。
            "_meta": {
                "ui": {"prefersBorder": True},
                "openai/ui": {"availableDisplayModes": ["inline", "fullscreen"]},
            },
        }]})
        return
    if method == "tools/list":
        _result(rid, {"tools": TOOLS})
        return
    if method == "tools/call":
        params = msg.get("params") or {}
        out, code = call_tool(params.get("name"), params.get("arguments"))
        if isinstance(out, dict):
            text = out.pop("__text__", None) or "已更新。"
            # structuredContent 只放真正的结构化结果；__text__ 等内部键不外泄，
            # 避免污染 openai/settings 这类带 outputSchema 的宿主校验。
            structured = {k: v for k, v in out.items() if not k.startswith("__")}
            result = {"content": [{"type": "text", "text": text}],
                      "structuredContent": structured, "isError": code != 0}
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
