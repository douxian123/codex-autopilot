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
from datetime import datetime, timezone

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "codex-autopilot"

# 本进程启动时所在的 ops/，也就是**正在跑的这份代码**。
# 注意它不等于「现在装的是哪一份」：Codex 升级插件会删掉旧的版本目录，而这个
# 已经在跑的进程不会重启（见 2026-11-16 的陈旧进程事故）。所以凡是去调
# kgctl / truthctl 的地方，路径都必须在那一次调用的当下解析，不能在这里算一次就
# 锁死 —— 那样等于把「目录被删」这件事固化进进程内存，原故障照旧复现。
RUNNING_OPS = Path(__file__).resolve().parent
REPO = RUNNING_OPS.parent

from plugin_binding import (apply_plugin_env, binding_health, binding_line,  # noqa: E402
                            current_ops, stale_code_problem)


def _live_ops() -> Path:
    """这一次调用当下「真正装好的那份 ops/」。

    为什么必须是函数、而不是模块级常量（2026-11-16 实测）：升级删掉旧版本目录的
    动作发生在进程启动**之后**。若在 import 时把路径算一次，那时候旧目录还在，
    `current_ops()` 会合法地判定「本目录能用」并返回它，路径就被锁死在一个很快
    会被删掉的目录上 —— 之后每次调用照样 `can't open file`，原故障一字未改。
    """
    return current_ops()


def _fallback_repo() -> Path:
    """没有显式 KG_REPO 时，子进程该把 cwd / 状态根落在哪。"""
    return _live_ops().parent


def _closure_rate_summary():
    """北极星「首问闭环率」的三态摘要。

    取不到就说取不到：这里绝不把失败降级成 0% 或「正常」，否则面板会用一个
    假绿灯掩盖「根本没在测」这件事（本项目自己的硬规则 10）。
    """
    try:
        import closure_rate
        res = closure_rate.compute(os.environ.get("KG_REPO") or _fallback_repo())
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


def _harness_health():
    """状态管家：当前 harness 的上下文健康（压缩频率 / 峰值 input / 用量）。

    真相只有一份：读 `budget_gate` 每拍已经算好的判定（`~/.codex-autopilot/
    budget-gate.json` 的 `last_report`），不在这里重算一遍——否则面板和守护会
    各说各话。取不到就说取不到，绝不用「正常」冒充（本项目硬规则 10）。

    只读，不触发换代、不改任何 harness 配置；真正调优由 budget_gate 在越线时按
    自己的保护条件（窗口空闲 + cooldown）执行，面板只负责让人看得见。
    """
    try:
        base = Path(os.environ.get("KG_DAEMON_HOME")
                    or (Path.home() / ".codex-autopilot")).expanduser()
        data = json.loads((base / "budget-gate.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "unknown",
                "reason": "还没跑过状态体检（守护下一拍会写）", "threads": []}
    if not isinstance(data, dict):
        return {"status": "unknown", "reason": "状态体检记录格式不对", "threads": []}
    report = data.get("last_report") or {}
    rows = report.get("threads") or []
    if not rows:
        return {"status": "no_data", "updated": data.get("last_run"),
                "reason": "状态体检还没测到长驻窗口", "threads": []}
    order = {"ok": 0, "warn": 1, "over": 2}
    worst = max(rows, key=lambda r: order.get(r.get("level"), -1))
    level = worst.get("level") or "unknown"
    return {
        "status": "ok",
        "level": level,
        "updated": data.get("last_run"),
        "headline": _harness_health_line(level, worst),
        "reasons": worst.get("reasons") or [],
        "usage": report.get("usage") or {},
        # 只留面板描述健康所需的最小字段，不把整份 rollout 读数搬上屏。
        "threads": [{
            "automation": r.get("automation"),
            "generation": r.get("generation"),
            "level": r.get("level"),
            "peak_input_recent": r.get("peak_input_recent"),
            "compactions_recent": r.get("compactions_recent"),
            "tokens_used": r.get("tokens_used"),
            "reasons": r.get("reasons") or [],
        } for r in rows],
        "dispatch_paused": bool(data.get("dispatch_paused")),
        "dispatch_paused_reason": data.get("dispatch_paused_reason") or "",
    }


def _harness_health_line(level, worst):
    name = worst.get("automation") or "长驻窗口"
    if level == "ok":
        return "上下文健康：正常"
    if level == "warn":
        return "上下文偏紧：建议空闲时整理"
    if level == "over":
        return "上下文已饱和：建议空闲时换新窗口"
    return "上下文健康：读不到判定"


def _harness_tune_summary():
    """状态管家：待用户点头的 harness 调整卡（只读，绝不在这里改 config）。

    卡片由守护每拍按真实体检结论提出（见 harness_tune.py）；这里只把「有几张、
    调什么、为什么」拼给面板。取不到就说取不到，不用「0 张」冒充（硬规则 10）。
    """
    try:
        import harness_tune
        rows = harness_tune.pending()
    except Exception as exc:
        return {"status": "unknown", "reason": f"读不到调整卡（{exc!r}）", "items": []}
    items = [{
        "id": r.get("id"),
        "key": r.get("key"),
        "label": _HARNESS_KEY_LABEL.get(r.get("key"), r.get("key")),
        "old": r.get("old"),
        "new": r.get("new"),
        "why": r.get("why"),
    } for r in rows]
    return {"status": "ok", "count": len(items), "items": items}


# 只给面板显示用的人话标签（键名本身照旧用于 apply，不翻译）。
_HARNESS_KEY_LABEL = {
    "model_auto_compact_token_limit": "上下文压缩线",
    "tool_output_token_limit": "单条工具输出上限",
}


def _constraint_candidates_summary():
    """上下文管家：待用户点头的「约束候选」（只读，绝不在这里写生效约束）。

    候选由守护每拍扫已路由的入口原话提出（见 constraint_candidates.py）；这里只把
    「有几条、原话是什么、命中什么信号」拼给面板。候选**不参与落盘门禁**，只有人
    显式点头 promote 才升成生效约束。取不到就说取不到，不用「0 条」冒充。
    """
    try:
        import constraint_candidates
        rows = constraint_candidates.pending()
    except Exception as exc:
        return {"status": "unknown", "reason": f"读不到约束候选（{exc!r}）", "items": []}
    learning = {"status": "unknown", "themes": []}
    try:
        import context_learning
        observed = context_learning.observe()
        learning = {"status": observed.get("status"),
                    "themes": observed.get("themes") or [],
                    "reason": observed.get("reason")}
    except Exception as exc:
        learning = {"status": "unknown", "reason": f"读不到重复主题（{exc!r}）", "themes": []}
    items = [{
        "id": r.get("id"),
        "text": r.get("text"),
        "title": r.get("title"),
        "kind": r.get("kind"),
        "scope": r.get("scope") or [],
        "signals": r.get("signals") or [],
        "source": r.get("source") or "intake",
        "mentions": r.get("mentions"),
        "threads": r.get("threads"),
        "priority": r.get("priority"),
        "evidence": (r.get("evidence") or [])[:2],
    } for r in rows]
    return {"status": "ok", "count": len(items), "items": items, "learning": learning}


def _context_archive_summary():
    """上下文管家：只读档案投影，回答自动收了什么、来源是什么、现在停在哪。"""
    try:
        import context_archive
        return context_archive.collect()
    except Exception as exc:
        return {
            "status": "unknown",
            "reason": f"上下文档案模块不可用（{exc!r}）",
            "counts": {},
            "items": [],
            "unknown": [f"上下文档案模块不可用（{exc!r}）"],
            "note": "取不到不等于 0 条。",
        }


def _acceptance_criteria_summary():
    """验收管家：待用户点头的「可判定验收标准候选」（只读）。

    候选从已路由原话里提名，确认前不进 acceptance.jsonl；确认后也只是登记
    验收项，机器自证项仍需真门禁证据。取不到就说取不到，不用 0 冒充正常。
    """
    try:
        import acceptance_criteria
        rows = acceptance_criteria.pending()
    except Exception as exc:
        return {"status": "unknown", "reason": f"读不到验收标准候选（{exc!r}）",
                "items": []}
    items = [{
        "id": r.get("id"),
        "text": r.get("text"),
        "title": r.get("title"),
        "needs_human": bool(r.get("needs_human")),
        "machine_gate_suggested": bool(r.get("machine_gate_suggested")),
        "steps": r.get("steps") or "",
        "signals": r.get("signals") or [],
        "files": r.get("files") or [],
    } for r in rows]
    return {"status": "ok", "count": len(items), "items": items}


def _pre_acceptance_summary():
    """验收管家的产品级预验收闸：未 ready 不得进入用户验收台。"""
    try:
        import pre_acceptance
        return pre_acceptance.evaluate(Path(_truth_dir()).parent.parent)
    except Exception as exc:
        return {
            "status": "unknown",
            "label": "验收管家：交付前自检取不到，不通知你验收",
            "checks": [],
            "passed": 0,
            "total": 4,
            "notify_user": False,
            "reason": repr(exc),
        }


def _route_health_summary(payload, state=None):
    """状态管家内部的路线/效能只读检测：不是第八个管家，不自动纠偏。

    判据只来自现有现场：worker 生命周期声明的 scope、当前工作区改动、
    生命周期世代/推进事件、已知 exec 卡点。拿不到就交给检测器输出 unknown；
    没有在跑的实现时是明确的空闲 ok，不拿「没有对象」冒充「检测通过」。
    现场组装逻辑在 `route_health_runtime`，与 daemon 每拍巡检共用，避免两套定义。
    """
    try:
        import route_health_runtime
    except Exception as exc:
        return {"status": "ok", "level": "unknown",
                "headline": "路线与效能判据取不到：检测器不可用（%s）" % exc,
                "unknown": ["检测器不可用"], "reasons": [], "action": "none"}
    summary = route_health_runtime.summarize(payload, state=state, runtime=RUNTIME)
    try:
        import route_review
        summary["review"] = route_review.current(RUNTIME)
    except Exception as exc:
        summary["review"] = {"status": "error", "level": "unknown", "count": None,
                             "items": [], "headline": "路线复核状态取不到（%s）" % exc}
    return summary


_RUNTIME_FRESHNESS_UNSET = object()


def _autopilot_source_repo_path():
    """Find the plugin source repo when this machine actually has one.

    KG_REPO points at the *target project*, which is not always the autopilot
    source. Only treat it as source when it visibly contains the plugin
    manifest and this server, otherwise source-vs-installed comparison stays
    unavailable instead of guessing.
    """
    candidates = (
        os.environ.get("KG_AUTOPILOT_SOURCE_REPO"),
        os.environ.get("KG_REPO"),
    )
    for raw in candidates:
        if not raw:
            continue
        repo = Path(raw).expanduser()
        if ((repo / "plugin/.codex-plugin/plugin.json").is_file()
                and (repo / "ops/central_mcp.py").is_file()):
            return repo
    return None


def _autopilot_source_commit():
    """Read the local source main commit without fetching or changing refs."""
    repo = _autopilot_source_repo_path()
    if repo is None:
        return None
    for ref in ("refs/remotes/origin/main", "HEAD"):
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", ref],
                capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        commit = (proc.stdout or "").strip() if proc.returncode == 0 else ""
        if commit:
            return commit
    return None


def _daemon_freshness_summary():
    """只读采集 daemon 代际：plist 指向、共享存活判据绑定、daemon 源码 commit。"""
    result = {"installed": None, "script": None, "live": None,
              "source_commit": None, "repo_matches": None,
              "bound_repo": None, "target_repo": None}
    try:
        import central_daemon
        installed = central_daemon.PLIST_PATH.is_file()
        result["installed"] = installed
        result["script"] = central_daemon.plist_script_path()
        if installed and result["script"]:
            script = Path(result["script"])
            metadata = script.parent.parent / ".codex-autopilot-source.json"
            try:
                import json
                result["source_commit"] = str(
                    json.loads(metadata.read_text(encoding="utf-8")).get("commit") or "").strip() or None
            except (OSError, ValueError):
                result["source_commit"] = None
    except Exception:
        return {k: None for k in result}

    try:
        import daemon_liveness
        target = os.environ.get("KG_REPO") or _fallback_repo()
        state = daemon_liveness.daemon_liveness(str(target))
        result["live"] = state.get("live")
        result["repo_matches"] = state.get("matches_repo")
        result["bound_repo"] = state.get("bound_repo")
        result["target_repo"] = state.get("repo")
    except Exception:
        result["live"] = None
        result["repo_matches"] = None
    return result


def _runtime_freshness_summary(binding=None, source_commit=_RUNTIME_FRESHNESS_UNSET,
                               daemon=_RUNTIME_FRESHNESS_UNSET):
    """状态管家内部判据：源码、已装版本、运行代码、daemon 是否同一代。只读。"""
    try:
        import plugin_binding
        import runtime_freshness
    except Exception as exc:
        return {"status": "ok", "level": "unknown",
                "headline": "运行态判据取不到：检测器不可用（%s）" % exc,
                "unknown": ["检测器不可用"], "reasons": [], "action": "none"}
    if source_commit is _RUNTIME_FRESHNESS_UNSET:
        source_commit = _autopilot_source_commit()
    if daemon is _RUNTIME_FRESHNESS_UNSET:
        daemon = _daemon_freshness_summary()
    return runtime_freshness.evaluate(
        binding or plugin_binding.install_consistency(),
        source_commit=source_commit, daemon=daemon)


def _project_health_summary(payload):
    """全局健康层：项目真相 / 执行流动 / 验收三层，只读聚合，不造第二套台账。

    与 `harness_health` 分开：那个回答「这台 harness 累不累」，这个回答
    「这个项目还健康吗」。判据全部来自已有台账与面板已算好的运行面字段，
    取不到就 unknown，绝不降级成「正常」。
    """
    try:
        import project_health
        blocked = payload.get("blocked") or {}
        return project_health.compute(
            os.environ.get("KG_REPO") or _fallback_repo(),
            runtime={
                "auto_exec": bool((payload.get("switches") or {}).get("auto_exec")),
                "exec_stuck": len(blocked.get("exec") or []),
                "land_failures": len(blocked.get("failures") or []),
                "resolve": len(blocked.get("resolve") or []),
                "overlap_waiting": len(blocked.get("exec_waiting") or []),
            },
        )
    except Exception as exc:
        # 模块坏了本身就是要上报的问题，不能静默成「没问题」。
        return {"status": "unknown", "level": "unknown",
                "headline": "项目健康算不出来（%s）" % exc,
                "layers": [], "signals": {}}


def _pm_briefing_summary(runtime=None):
    """PM 简报：只读聚合预期、施工路径、让路与追问候选，不新增台账、不派活。"""
    try:
        import pm_briefing
        return pm_briefing.compute(
            os.environ.get("KG_REPO") or _fallback_repo(),
            runtime=runtime or {},
        )
    except Exception as exc:
        return {"status": "unknown",
                "expectations": {"status": "unknown", "items": [], "missing": [str(exc)]},
                "overlap_holds": {"status": "unknown", "items": [],
                                  "note": "PM 简报算不出来（%s）" % exc},
                "follow_up_candidates": {"status": "unknown", "items": [],
                                         "note": "PM 简报算不出来（%s）" % exc},
                "source": "pm_briefing 模块不可用"}


def _authorized_followup_summary(root=None):
    """追问授权账本的只读投影：候选不是动作，只有逐项 explicit 授权才可发送。"""
    try:
        import authorized_followup
        return authorized_followup.summary(
            Path(root) if root else Path(os.environ.get("KG_REPO") or _fallback_repo()))
    except Exception as exc:
        return {"status": "unknown", "items": [],
                "counts": {"approved": None, "sent": None,
                           "cancelled": None, "failed": None},
                "note": "追问授权账本取不到（%s）；不把不知道当成 0。" % exc}


def _relationship_goals_summary():
    """关系层只读摘要：跨项目长期目标、需要人的时机，不派活、不替人定优先级。"""
    try:
        import relationship_ledger
        return relationship_ledger.summary()
    except Exception as exc:
        return {"status": "unknown", "level": "unknown",
                "headline": "跨项目目标取不到（%s）" % exc,
                "note": "取不到不等于 0 条；本层不自动派活，也不替你决定目标。",
                "count": None, "needs_human_count": None, "items": []}


def _outcome_score_summary():
    """结果代理分：只读原始台账，命令绿灯不冒充用户结果。"""
    try:
        import outcome_score
        return outcome_score.compute(os.environ.get("KG_REPO") or _fallback_repo())
    except Exception as exc:
        return {"status": "unknown", "score": None, "grade": "未知",
                "coverage": 0.0,
                "headline": "结果代理分取不到（%s）" % exc,
                "note": "取不到不等于 0 分；机器命令通过不等于用户结果变好。",
                "components": [], "missing": ["结果代理分"], "shortfalls": []}


def _screenshots_env(value):
    """把 MCP 的 screenshots 数组归一成 truthctl 的 ':' 分隔 env。"""
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        items = [str(v).strip() for v in value if str(v or "").strip()]
    else:
        items = [x.strip() for x in str(value).replace(",", ":").split(":") if x.strip()]
    return ":".join(items) or None


def _acceptance_record(acceptance_id):
    """按 id 取验收台账的最新一条；取不到就返回 None，不猜。"""
    aid = str(acceptance_id or "").strip()
    latest = {}
    for row in _read_jsonl(_truth_dir() / "acceptance.jsonl"):
        if row.get("id"):
            latest[row["id"]] = row
    return latest.get(aid)


def _capture_ready_acceptance(record):
    """只有已自验、仍待人工复核的项才允许补截图。

    这把「先自验，再打扰用户」的顺序写进调用口：截图是复核附件，不能把
    没有 evidence 的项伪装成可以给人看，也不能顺手把状态改成 verified。
    """
    rec = record or {}
    return (bool(rec.get("needs_human"))
            and rec.get("status") in ("todo", "wip", "blocked")
            and bool(str(rec.get("evidence") or "").strip()))


def _n(value):
    """把可能为 None 的计数渲染成面板能读的字样。

    实测缺陷（2026-11-17）：健康条直接把 `queue["clean"]` 之类塞进 `%s`，
    守护没跑时那些字段是 None，于是首屏印出「可合 None · 冲突 None」——
    内部空值泄漏到读者面。取不到就说「—」，不印 Python 字面量。
    """
    return "—" if value is None else str(value)


def _steward_health(payload):
    """七管家健康条：把七个管家的真实状态收敛成一份面板可读的清单。

    用户问过「面板要怎么做」，也给过「万法归一入口 + 背后许多管家」的完整设想。
    面板此前只散着显示路由/合并/实现/项目经理的一部分，状态与上下文管家按到最近
    才有卡 —— 没有一个地方能一眼看出**哪个管家断了**。这里只做**聚合**，判据全部
    来自各处已经算好的真实字段，不重算、不新增第二套真相。

    每个管家给三样：`key`（稳定标识）、`state`（ok / attention / off / unknown）、
    `label`（人话一行）。`state` 只有这四态：`attention` = 需要人或需要修，
    `off` = 用户自己关的（不是故障），`unknown` = 取不到（不冒充正常）。

    末尾另有一行 `health`，它不是第八个管家，而是只读的项目级全局健康结论。
    """
    sw = payload.get("switches") or {}
    daemon = payload.get("daemon") or {}
    tune = payload.get("harness_tune") or {}
    cand = payload.get("constraint_candidates") or {}
    learning = cand.get("learning") or {}
    acc_criteria = payload.get("acceptance_criteria") or {}
    harness_health = payload.get("harness_health") or {}
    route_health = payload.get("route_health") or {}
    route_review = route_health.get("review") or {}
    route_review_count = route_review.get("count")
    project_health = payload.get("project_health") or {}
    runtime_freshness = payload.get("runtime_freshness") or {}
    queue = payload.get("queue") or {}
    counters = payload.get("counters") or {}
    blocked = payload.get("blocked") or {}

    rows = []

    # 1. 上下文管家：自动收录候选 + 生效约束条数。候选待确认 = 需要人，不是故障。
    try:
        import constraints as constraints_mod
        active = len(constraints_mod.active_constraints(_truth_dir() / "constraints.jsonl"))
    except Exception:
        active = None
    pending_c = cand.get("count")
    if cand.get("status") != "ok" or active is None:
        rows.append({"key": "context", "state": "unknown",
                     "label": "上下文管家：约束台账读不到"})
    elif pending_c:
        label = "上下文管家：%s 条候选等你确认 · 已生效 %s 条" % (pending_c, active)
        if learning.get("status") == "ok" and learning.get("themes"):
            label += " · 发现 %s 个重复主题" % len(learning["themes"])
        rows.append({"key": "context", "state": "attention", "label": label})
    else:
        rows.append({"key": "context", "state": "ok",
                     "label": "上下文管家：已生效 %s 条约束，无待确认" % active})

    # 2. 路由管家：跨项目收口是不是真在跑。真实判据是全局待路由箱里还没归到项目的
    # 条数（`_answer_needed_count`），不是 `_route_summary` 里的字段——它根本没有
    # unrouted，写成那样会永远走「已归类」的假绿。
    unrouted = _answer_needed_count()
    if not daemon.get("live"):
        rows.append({"key": "route", "state": "unknown",
                     "label": "路由管家：守护没在跑，收口状态取不到"})
    elif unrouted:
        rows.append({"key": "route", "state": "attention",
                     "label": "路由管家：%s 条还在等归属，没进任何项目" % unrouted})
    else:
        rows.append({"key": "route", "state": "ok",
                     "label": "路由管家：新收需求都已归到项目"})

    # 3. 合并管家：自动合入 / 拆冲突的开关与卡点。
    if not sw.get("auto_land"):
        rows.append({"key": "merge", "state": "off",
                     "label": "合并管家：自动合入已关"})
    elif blocked.get("failures") or blocked.get("resolve"):
        rows.append({"key": "merge", "state": "attention",
                     "label": "合并管家：%s 条卡在门禁 / %s 条待拆冲突"
                     % (len(blocked.get("failures") or []), len(blocked.get("resolve") or []))})
    else:
        rows.append({"key": "merge", "state": "ok",
                     "label": "合并管家：可合 %s · 已合 %s · 冲突 %s"
                     % (_n(queue.get("clean")), _n(counters.get("landed")),
                        _n(queue.get("conflict")))})

    # 4. 状态管家：待点头的 harness 调整卡 + 上下文健康 + 路线/效能复核 + 运行态新鲜度。
    # 这些信号都是同一个管家内部的只读检测器；不新增一行，也不生成动作。
    route_level = route_health.get("level")
    freshness_level = runtime_freshness.get("level")
    if tune.get("status") != "ok":
        rows.append({"key": "tune", "state": "unknown",
                     "label": "状态管家：调整卡读不到"})
    elif freshness_level == "attention":
        rows.append({"key": "tune", "state": "attention",
                     "label": "状态管家：%s（需受控换代或重载，不会自动重启）"
                     % (runtime_freshness.get("headline") or "运行态需要复核")})
    elif route_level == "unknown":
        rows.append({"key": "tune", "state": "unknown",
                     "label": "状态管家：%s" % (route_health.get("headline")
                                              or "路线与效能判据取不到")})
    elif route_review.get("status") == "error":
        rows.append({"key": "tune", "state": "unknown",
                     "label": "状态管家：%s" % (route_review.get("headline")
                                              or "路线复核状态取不到")})
    elif route_review_count:
        rows.append({"key": "tune", "state": "attention",
                     "label": "状态管家：%s 项路线与效能待复核（只提复核，不自动纠偏）"
                              % route_review_count})
    elif tune.get("count"):
        rows.append({"key": "tune", "state": "attention",
                     "label": "状态管家：%s 张调整卡等你点头（config 未动）" % tune.get("count")})
    elif harness_health.get("status") == "ok" and harness_health.get("level") in ("warn", "over"):
        rows.append({"key": "tune", "state": "attention",
                     "label": "状态管家：%s" % (harness_health.get("headline") or "上下文偏紧")})
    elif route_level == "attention":
        rows.append({"key": "tune", "state": "attention",
                     "label": "状态管家：路线与效能需复核（只提复核，不自动纠偏）"})
    elif freshness_level == "unknown":
        # 开发检出没有安装锚点只是「判据不足」，不能盖住真正需要人看的路线
        # attention。放在最后：有可执行结论先报，确实取不到才报取不到。
        rows.append({"key": "tune", "state": "unknown",
                     "label": "状态管家：%s" % (runtime_freshness.get("headline")
                                              or "运行态判据取不到")})
    else:
        rows.append({"key": "tune", "state": "ok",
                     "label": "状态管家：%s" % (harness_health.get("headline") or "当前无待调整项")})

    # 5. 实现管家：auto_exec 开关。关着是用户的选择（off），不是故障。
    if not sw.get("auto_exec"):
        rows.append({"key": "exec", "state": "off",
                     "label": "实现管家：自动实现已关（只路由和合并，不自动写码）"})
    elif blocked.get("exec"):
        rows.append({"key": "exec", "state": "attention",
                     "label": "实现管家：%s 条实现卡住" % len(blocked.get("exec") or [])})
    else:
        rows.append({"key": "exec", "state": "ok",
                     "label": "实现管家：已开，会按槽位自动派活"})

    # 6. 项目经理：对外可视的三问面板本身是否在产出。取不到就说取不到。
    if payload.get("project", {}).get("path"):
        rows.append({"key": "pm", "state": "ok",
                     "label": "项目经理：已绑定 %s，三问面板在产出" % payload["project"]["name"]})
    else:
        rows.append({"key": "pm", "state": "unknown",
                     "label": "项目经理：还没绑定项目"})

    # 7. 验收管家：先把活自己跑一遍，验完了才来提醒人。顺序是语义，不是礼貌：
    # 没人自验的人工项如果直接进「等你验收」，用户就成了第一道测试。
    self_check_n = payload.get("self_check_count")
    human_ready = payload.get("human_acceptance") or []
    if self_check_n is None:
        rows.append({"key": "accept", "state": "unknown",
                     "label": "验收管家：待自验项读不到"})
    elif acc_criteria.get("status") != "ok":
        rows.append({"key": "accept", "state": "unknown",
                     "label": "验收管家：验收标准候选取不到"})
    elif acc_criteria.get("count"):
        rows.append({"key": "accept", "state": "attention",
                     "label": "验收管家：%s 条验收标准候选等你确认（未登记）"
                     % acc_criteria.get("count")})
    elif (payload.get("pre_acceptance_blocked_count") or 0) or (
            payload.get("pre_acceptance", {}).get("status") in ("blocked", "unknown")):
        pre = payload.get("pre_acceptance") or {}
        pre_checks = pre.get("checks") or []
        failed = [r.get("title") for r in pre_checks if r.get("state") != "pass"]
        label = "、".join([x for x in failed if x][:2]) or "交付前自检"
        rows_text = "；".join(
            "%s：%s" % (r.get("title"), r.get("summary"))
            for r in pre_checks if r.get("state") != "pass"
        )
        rows_text = rows_text or "证据不完整"
        rows.append({"key": "accept", "state": "attention",
                     "label": "验收管家：%s 未通过，先返修，不通知你验收（%s）"
                     % (label, rows_text)})
    elif self_check_n and not sw.get("auto_exec"):
        # 待自验却没人会去跑，是真实卡点，不是「正在处理」。
        rows.append({"key": "accept", "state": "attention",
                     "label": "验收管家：%s 条还没自验，但自动实现已关，没人会去跑" % self_check_n})
    elif self_check_n:
        rows.append({"key": "accept", "state": "ok",
                     "label": "验收管家：%s 条正在自验，验完才提醒你" % self_check_n})
    elif human_ready:
        rows.append({"key": "accept", "state": "attention",
                     "label": "验收管家：%s 条已自验，等你确认" % len(human_ready)})
    else:
        rows.append({"key": "accept", "state": "ok",
                     "label": "验收管家：没有待你验收的项"})

    # 8. 全局健康：不只 harness，而是项目真相 / 执行流动 / 验收三层。
    # 与上面七行不同，它不为某一道工序负责，专门回答「这个项目还健康吗」。
    p_state = project_health.get("level")
    if project_health.get("status") != "ok" or p_state not in ("ok", "attention", "unknown"):
        rows.append({"key": "health", "state": "unknown",
                     "label": "全局健康：%s" % (project_health.get("headline")
                                               or "结论取不到")})
    else:
        rows.append({"key": "health", "state": p_state,
                     "label": "全局健康：%s" % (project_health.get("headline")
                                               or "暂无异常")})

    order = {"attention": 0, "unknown": 1, "off": 2, "ok": 3}
    rows.sort(key=lambda r: order.get(r["state"], 9))
    counts = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    return {"rows": rows, "counts": counts}


def _autonomy_summary(state):
    """授权情况摘要：这一拍有多少工序是明确授权执行的、多少还在观察、多少等你转正。

    判据只读守护写进 `state.json` 的 `last_passes`，不重算注册表、不维护第二套真相。
    取不到时返回 `unknown` 并如实写「取不到」——不能把「没有记录」显示成
    「0 条在观察」，那等于假装一切正常。
    """
    passes = (state or {}).get("last_passes")
    if not isinstance(passes, list) or not passes:
        return {
            "status": "unknown",
            "label": "授权情况：本轮记录取不到",
            "explicit": None,
            "observing": None,
            "proposed": None,
            "passes": [],
        }

    def _detail(pass_row):
        row = pass_row if isinstance(pass_row, dict) else {}

        def _value(key):
            value = row.get(key)
            return str(value).strip() if value not in (None, "") else "取不到"

        return {
            "name": _value("name"),
            "mode": _value("mode"),
            "status": _value("status"),
            "authorization": _value("authorization"),
            "effect": _value("effect"),
        }

    explicit = sum(1 for p in passes if (p or {}).get("authorization") == "explicit")
    observing = sum(1 for p in passes if (p or {}).get("mode") == "observe")
    proposed = sum(1 for p in passes if (p or {}).get("status") == "proposed")
    errored = sum(1 for p in passes if (p or {}).get("status") == "errored")
    bits = ["已明确授权 %d 条" % explicit, "观察中 %d 条" % observing]
    if proposed:
        bits.append("%d 条等你转正" % proposed)
    if errored:
        bits.append("%d 条出错" % errored)
    return {
        "status": "ok",
        "label": "授权情况：" + " · ".join(bits),
        "explicit": explicit,
        "observing": observing,
        "proposed": proposed,
        "errored": errored,
        "passes": [_detail(p) for p in passes],
    }


def _server_version() -> str:
    """版本以插件清单为准，避免 MCP 里再写死一份导致漂移。

    读的是**当前装好的那一份**清单，不是本进程启动时的旧目录 —— 旧目录在升级后
    已经不存在，读它只会退回 "0.0.0"，让面板看不出自己到底跑的是哪一代。
    """
    for rel in (".codex-plugin/plugin.json", "plugin/.codex-plugin/plugin.json"):
        manifest = _live_ops().parent / rel
        try:
            return json.loads(manifest.read_text(encoding="utf-8")).get("version", "0.0.0")
        except (OSError, ValueError):
            continue
    return "0.0.0"


SERVER_VERSION = _server_version()

import control_state  # noqa: E402
import project_autonomy  # noqa: E402
import codex_native  # noqa: E402
import acceptance_criteria  # noqa: E402
from ledger_state import latest_ledger_by_id  # noqa: E402

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
    root = _live_ops().parent
    for cand in (root / "ui" / "panel.html",
                 root / "plugin" / "ui" / "panel.html"):
        if cand.exists():
            return cand
    return None


def _run(argv, extra_env=None, timeout=900):
    env = os.environ.copy()
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items() if v is not None})
    # cwd 用状态根：kgctl 的 git 回退路径（worktree list / rev-parse）要落在目标项目里，
    # 而不是插件自己所在的目录（那里通常不是一个 Git 仓库）。
    cwd = os.environ.get("KG_REPO") or str(_fallback_repo())
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
    return _run([str(_live_ops() / "kgctl.py"), *args], extra_env)


def _json_line(text):
    """从 kgctl 输出里取最后一行 JSON；人类可读行仍在，机器只读结构化尾行。"""
    for line in reversed(str(text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            got = json.loads(line)
        except ValueError:
            continue
        if isinstance(got, dict):
            return got
    return None


def _registered_projects():
    try:
        import project_router
        return [{"name": str(p.get("name") or ""),
                 "root": str(p.get("root") or "")}
                for p in project_router.load_registry()
                if p.get("name")]
    except Exception:
        return []


def _global_routing_summary():
    """全局待路由箱的三态摘要：ok 有明确计数，unknown 绝不冒充 0。"""
    projects = _registered_projects()
    try:
        import project_router
        path = project_router.routing_inbox_path()
        if not path.exists():
            return {"status": "ok", "count": 0, "items": [],
                    "projects": projects, "path": str(path),
                    "text": "全局待路由箱为空"}
        pending = project_router.pending_routing_records(path)
        items = []
        for item in pending:
            row = item.get("record") or {}
            items.append({
                "index": item.get("index"),
                "fingerprint": item.get("fingerprint"),
                "ts": row.get("ts"),
                "text": str(row.get("text") or ""),
                "candidates": [c.get("name") for c in (row.get("candidates") or [])
                               if isinstance(c, dict) and c.get("name")],
            })
        count = len(items)
        return {"status": "ok", "count": count, "items": items,
                "projects": projects, "path": str(path),
                "text": ("全局待路由 %d 条待复核" % count) if count
                        else "全局待路由箱为空"}
    except Exception as exc:
        return {"status": "unknown", "count": None, "items": [],
                "projects": projects, "error": str(exc),
                "text": "全局待路由箱取不到；不会当成 0 条"}


def truthctl(*args, extra_env=None):
    return _run([str(_live_ops() / "truthctl.py"), *args], extra_env)


def _request_env(args, *, required=False):
    """Resolve per-call project context without guessing across projects."""
    args = args or {}
    if not args.get("project") and not args.get("cwd"):
        if required:
            raise RequestContextError(
                "当前入口缺少项目归属；请传入 project 或 cwd，未写入任何项目账本")
        return {}
    try:
        repo = resolve_project(project=args.get("project"), cwd=args.get("cwd"),
                               bound=os.environ.get("KG_REPO"))
    except RequestContextError as exc:
        raise exc
    return {"KG_REPO": str(repo)}


TOOLS = [
    {
        "name": "project_status",
        "description": "一屏看清项目到哪了：状态根、主干、待路由输入、分支可合并/冲突数、共享检出脏度。用户问“现在到哪了 / 还有什么没做”时先用它。",
        "inputSchema": {"type": "object", "properties": {
            "project": {"type": "string", "description": "当前项目状态根绝对路径"},
            "cwd": {"type": "string", "description": "当前 Codex 工作目录"},
        }, "additionalProperties": False},
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
                "cwd": {"type": "string",
                        "description": "当前会话所在目录（强烈建议带上）。归属判定靠它区分不同项目；不带就可能判不清，记录会进全局待路由箱而不是某个项目账本"},
                "project": {"type": "string",
                            "description": "明确指定项目名或状态根（用户说了是哪个项目时带上）"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "route_preview",
        "description": "只读预演当前这句话会落到哪个项目：不改账本、不注册项目。判不清就明说，默认落点也明说，不猜。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "用户原话，别重写、别归类"},
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "相关文件路径（可选，帮助判断落点）"},
                "cwd": {"type": "string",
                        "description": "当前会话所在目录（可选；调用方知道时带上）"},
                "project": {"type": "string",
                            "description": "明确指定的项目名或状态根（可选）"},
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
        "name": "routing_review_list",
        "description": "只读列出全局待路由箱里仍未复核指派的投稿。它不等于 route_inbox：后者是把当前项目 inbox 落到泳道，这里只处理跨项目判不清归属的原始记录。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "routing_review_assign",
        "description": "把一条全局待路由投稿显式指派给某个项目。默认预演不写账本；只有同时给出 fingerprint、project、confirm=true 才追加目标项目 inbox 和复核凭证，原始待路由记录不删不改。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "fingerprint": {"type": "string", "description": "routing_review_list 返回的稳定指纹"},
                "project": {"type": "string", "description": "注册项目名、别名或状态根路径；歧义会拒绝，绝不取第一个"},
                "reason": {"type": "string", "description": "复核依据，写进 append-only 凭证"},
                "confirm": {"type": "boolean", "description": "true 才真正追加；缺省或 false 只预演"},
            },
            "required": ["fingerprint", "project"],
            "additionalProperties": False,
        },
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
        "name": "constraints_add",
        "description": "把用户说过的一句预期/返修/决策/禁忌收成一条带 scope 的项目约束。目的是“说过一次就不再犯”：scope 写清管哪些路径/模块，实现那块改动时系统会自动把相关约束拼给执行者。能写成断言的门禁填 gate，审美/手感填 checklist，老实标 none。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "statement": {"type": "string", "description": "约束原话（用户怎么说就怎么记，别改写）"},
                "kind": {"type": "string", "enum": ["expectation", "rework", "decision", "taboo"],
                         "description": "预期 / 返修 / 决策 / 禁忌，默认 expectation"},
                "scope": {"type": "string",
                          "description": "管哪些路径或模块，冒号分隔的 glob，如 “backend:plugin/ui/*.html”；留空=全局约束（恒命中）"},
                "enforceable": {"type": "string", "enum": ["none", "gate", "checklist"],
                                "description": "none=纯记录 / gate=有机器门禁 / checklist=人验收项，默认 none"},
                "cmd": {"type": "string",
                        "description": "enforceable=gate 时必填：落盘那一刻真跑的验证命令（如 “python3 ops/test-x.py”）。写不出命令就别标 gate。"},
                "cwd": {"type": "string",
                        "description": "命令的工作目录，相对仓库根，默认仓库根"},
                "policy_version": {"type": "string", "description": "约束版本标识，可选（用于改动传播）"},
                "supersedes": {"type": "string", "description": "被这条取代的旧约束 id，可选"},
            },
            "required": ["statement"],
            "additionalProperties": False,
        },
    },
    {
        "name": "constraints_list",
        "description": "列出项目约束台账；带 files 时只列命中这些路径的（这就是“精准调取”：不是把全部上下文喂进去）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "改动文件路径，可选；给了就只返回命中的约束"},
            },
            "additionalProperties": False,
        },
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
                "confirm_human": {"type": "boolean", "description": "仅当用户亲口说过「看过/可以了」时传 true。视觉、体验类条目默认停在「等你确认」，不要自己替用户签收。"},
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
                "screenshots": {"type": "array", "items": {"type": "string"},
                                "description": "待用户复核的截图/录屏路径，可选；不参与机器自验判定"},
                "level": {"type": "string", "description": "优先级，默认 P1"},
                "lane": {"type": "string", "description": "归属分支，可选"},
                "status": {"type": "string", "description": "todo / wip / blocked / verified / void，默认 todo"},
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
        "name": "resolve_acceptance",
        "description": "收敛一条已登记的验收项：人工拍板（用户看过、点过、说可以）或机器复核完之后，把对应 A-id 从 todo/wip/blocked 翻成 verified；误登记项翻成 void，从卡点与验收统计里消失。传 id 必带 status；人工拍板不需要 evidence，机器复核建议带 evidence。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "验收台账 id，如 A-0030"},
                "status": {"type": "string", "enum": ["todo", "wip", "blocked", "verified", "void"],
                           "description": "目标状态；人工验收通过用 verified"},
                "note": {"type": "string", "description": "为什么翻成这个状态，一句话，可选"},
                "evidence": {"type": "string", "description": "机器复核的命令+结果，可选"},
                "screenshots": {"type": "array", "items": {"type": "string"},
                                "description": "补充待复核截图路径，可选；只追加，不覆盖已有附件"},
            },
            "required": ["id", "status"],
            "additionalProperties": False,
        },
    },
    {
        "name": "capture_acceptance_screenshot",
        "description": "用户在面板上主动给一条待人工复核的验收项补截图：本机交互式框选，图片写到用户级目录，只把路径追加到对应 A-id 的 screenshots，绝不自动把验收项标成通过。必须显式 confirm=true。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "验收台账 id，如 A-0030"},
                "confirm": {"type": "boolean",
                            "description": "用户已点击「补一张复核截图」并明确同意框选；false 一律拒绝"},
            },
            "required": ["id", "confirm"],
            "additionalProperties": False,
        },
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
                "cwd": {"type": "string",
                        "description": "当前会话所在目录（强烈建议带上）。归属判定靠它区分不同项目；不带就可能判不清，记录会进全局待路由箱而不是某个项目账本"},
                "project": {"type": "string",
                            "description": "明确指定项目名或状态根（用户说了是哪个项目时带上）"},
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
        "name": "project_autonomy_status",
        "description": "查看当前项目的自主运行状态、任务队列、运行中的 worker、等待用户事项和过期租约。它只读，不启动任务。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "project_autonomy_control",
        "description": "设置当前项目的自主运行模式：enable 只登记项目、run 开始持续推进、pause 暂停新派活、wait 等待用户、disable 关闭项目自主运行。项目级状态不影响其它项目。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["enable", "run", "pause", "wait", "disable"]},
                "reason": {"type": "string", "description": "暂停或等待的原因，可选"},
            },
            "required": ["action"],
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
    {
        "name": "harness_tune_list",
        "description": "状态管家：列出待你点头的 harness 调整卡（压缩线 / 单条工具输出上限）。这些卡由守护按长驻窗口的真实体检结论提出；不点头 config 一个字都不会变。用户问“上下文健康 / 该不该调配置 / 状态管家提了什么”时用它。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "harness_tune_apply",
        "description": "状态管家：应用一张调整卡。只有用户明确点头才能调用（confirm=true 是「人已同意」的语义）。应用前按字节备份 config.toml，只替换白名单里那一个键的那一行，其余字节不动；出卡后 config 被人改过则整张卡作废，绝不覆盖人的编辑。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "调整卡 id，如 T-0001"},
                "confirm": {"type": "boolean",
                            "description": "必须为 true：代表用户已经明确同意这次改动"},
            },
            "required": ["id", "confirm"],
            "additionalProperties": False,
        },
    },
    {
        "name": "harness_tune_rollback",
        "description": "状态管家：把一张已应用的调整卡按字节回滚到应用前的 config.toml。改了不舒服可以一键退回，这个动作只碰被这张卡改过的那份文件。",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "调整卡 id，如 T-0001"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "constraint_candidates_list",
        "description": "上下文管家：列出待你点头的「约束候选」。这些候选由守护扫已路由的入口原话自动提出（命中「绝不/必须/不许/以后都/记住」等规范性措辞），附上你的原话与命中信号。候选**不参与落盘门禁**，不点头就什么都不生效。用户问“上下文管家收了我哪些说法 / 有没有待确认的约束”时用它。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "context_archive",
        "description": "上下文管家：只读查看自动收录档案，区分待确认、已确认、已驳回和生效约束，保留来源、重复次数、会话数与用户原话。这里不提供第二个审批入口：要确认或驳回仍走 constraint_candidate_promote / constraint_candidate_dismiss。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "constraint_candidate_promote",
        "description": "上下文管家：把一条候选升成真正生效的项目约束（走 truthctl constraints add）。只有用户明确点头才能调用（confirm=true 是「人已同意」的语义）。生效后这条会参与落盘门禁与执行者 prompt，所以必须由人确认。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "候选 id，如 CC-0001"},
                "confirm": {"type": "boolean",
                            "description": "必须为 true：代表用户已经明确同意这条约束"},
            },
            "required": ["id", "confirm"],
            "additionalProperties": False,
        },
    },
    {
        "name": "constraint_candidate_dismiss",
        "description": "上下文管家：把一条候选标记为「不算」——它既不会生效，以后同一句原话也不会再弹回来。用户说“这条不算 / 我不是那个意思 / 别收这条”时用它。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "候选 id，如 CC-0001"},
                "reason": {"type": "string", "description": "为什么不算（可选，仅留档）"},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "acceptance_criteria_list",
        "description": "验收管家：列出待确认的「可判定验收标准候选」。守护从已路由原话里提名“怎么算完成”，候选阶段不写验收台账、不伪造自验证据，也不会自动催用户。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "acceptance_criteria_promote",
        "description": "验收管家：用户点头后，把一条验收标准候选登记成正式验收项（走 truthctl accept）。confirm=true 表示用户已明确同意；确认后仍需真门禁证据，机器自证项不会自动标 verified，人工项也不会自动催用户。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "候选 id，如 AC-0001"},
                "confirm": {"type": "boolean", "description": "必须为 true：代表用户已明确同意"},
                "needs_human": {"type": "boolean", "description": "是否必须人看；默认按候选建议"},
                "steps": {"type": "string", "description": "复核步骤（可选，默认按原话生成）"},
                "level": {"type": "string", "description": "优先级，如 P0 / P1"},
            },
            "required": ["id", "confirm"],
            "additionalProperties": False,
        },
    },
    {
        "name": "acceptance_criteria_dismiss",
        "description": "验收管家：把一条验收标准候选标记为「不算」——不登记验收项，同一句原话以后也不会再弹回来。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "候选 id，如 AC-0001"},
                "reason": {"type": "string", "description": "为什么不算（可选，仅留档）"},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "followup_authorize",
        "description": "PM 管家：逐项授权一条追问候选。候选本身不会自动发消息；只有 confirm=true 才先把 explicit 授权写入 append-only 授权账本，再由后台在发送前重新校验资格后投递一次。重复授权不会重复打扰。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string", "description": "工作项 id，如 L-0021"},
                "thread_id": {"type": "string", "description": "真实 Codex 原生窗口 id"},
                "prompt": {"type": "string", "description": "要发给该窗口的追问答"},
                "reason": {"type": "string", "description": "为什么现在授权追问；会留在账本里"},
                "confirm": {"type": "boolean",
                            "description": "必须为 true：代表用户已经明确同意这条追问"},
            },
            "required": ["ticket_id", "thread_id", "prompt", "reason", "confirm"],
            "additionalProperties": False,
        },
    },
    {
        "name": "followup_cancel",
        "description": "PM 管家：撤销一条尚未发送的追问授权。已经发出去的消息不能撤回；撤销只追加事件，不覆盖历史。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "description": "授权 id，如 FU-xxxxxxxx"},
                "reason": {"type": "string", "description": "为什么撤销；必填留痕"},
            },
            "required": ["request_id", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "followup_list",
        "description": "PM 管家：只读查看追问授权账本摘要，区分 approved/sent/cancelled/failed；取不到就说取不到，不把不知道当成 0。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "pm_research_status",
        "description": "PM 管家：查看竞品/开源研究与产品决策状态。只展示研究结论、等待确认和阻塞，不把没有证据误报成没有成熟方案。",
        "inputSchema": {
            "type": "object",
            "properties": {"research_id": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "pm_research_request",
        "description": "PM 管家：为一个产品诉求创建研究请求。研究由宿主或人工适配器提供证据；本工具不联网、不绑定搜索 provider。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "intent_id": {"type": "string"},
                "question": {"type": "string"},
                "scope": {"type": "array", "items": {"type": "string"}},
                "research_level": {"type": "string", "enum": ["L0", "L1", "L2", "L3"]},
                "risk_level": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
            },
            "required": ["intent_id", "question"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pm_research_evidence",
        "description": "PM 管家：写入一个研究来源和证据摘要。来源必须可追溯；许可证未知时不能自动 Adopt。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "research_id": {"type": "string"},
                "source_url": {"type": "string"},
                "source_kind": {"type": "string"},
                "title": {"type": "string"},
                "claims": {"type": "array", "items": {"type": "string"}},
                "license_status": {"type": "string"},
                "maintenance_signal": {"type": "string"},
                "relevance": {"type": "string"},
                "confidence": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["research_id", "source_url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pm_research_option",
        "description": "PM 管家：登记候选方案及 Adopt/Adapt/Build/Defer/Reject 判断。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "research_id": {"type": "string"},
                "name": {"type": "string"},
                "disposition": {"type": "string", "enum": ["adopt", "adapt", "build", "defer", "reject"]},
                "rationale": {"type": "string"},
                "capabilities": {"type": "array", "items": {"type": "string"}},
                "gaps": {"type": "array", "items": {"type": "string"}},
                "license_status": {"type": "string"},
                "confidence": {"type": "string"},
                "selected": {"type": "boolean"},
            },
            "required": ["research_id", "name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pm_product_decide",
        "description": "PM 管家：将研究候选写入既有决策台账。L3、高风险或许可证未知的 Adopt 必须 confirm=true；未批准不能施工。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "research_id": {"type": "string"},
                "choice": {"type": "string", "enum": ["adopt", "adapt", "build", "defer", "reject"]},
                "selected_option": {"type": "string"},
                "why": {"type": "string"},
                "alternatives": {"type": "array", "items": {"type": "string"}},
                "confirm": {"type": "boolean"},
            },
            "required": ["research_id", "choice"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pm_execution_gate",
        "description": "PM 管家：检查一个工作项是否已有可执行的产品决策；没有决策或高风险未批准时 fail-closed。",
        "inputSchema": {
            "type": "object",
            "properties": {"intent_id": {"type": "string"}, "research_id": {"type": "string"}},
            "additionalProperties": False,
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
    """从 launchd 读取存活状态和绑定项目，不用受沙箱限制的 kill(pid, 0)。

    判据只有一份（ops/daemon_liveness.py），与 kgctl status / doctor 共用；
    否则面板和 CLI 会各说各话：CLI 报「未安装」而面板仍显示「后台常驻=开」。
    """
    try:
        import daemon_liveness
    except Exception:
        return False, None, None
    st = daemon_liveness.daemon_liveness(os.environ.get("KG_REPO") or _fallback_repo())
    return bool(st.get("live")), st.get("pid"), st.get("bound_repo")


def _daemon_liveness_action(daemon_enabled=True):
    """一次采样同时产生 doctor 详情与首屏短结论，避免同一拍两份读数。"""
    try:
        import daemon_liveness
        st = daemon_liveness.daemon_liveness(os.environ.get("KG_REPO") or _fallback_repo())
        problem = daemon_liveness.liveness_problem(st, daemon_enabled)
        headline, detail = daemon_liveness.liveness_need_action(st, daemon_enabled)
        return problem, headline, detail
    except Exception:
        return None, None, None


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
    """还没关闭的问题数（bug/视觉/体验/架构），按事件时间取当前状态。"""
    repo = os.environ.get("KG_REPO")
    if not repo:
        return 0
    ledger = Path(repo) / "ops" / "truth" / "ledger.jsonl"
    rows = []
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
        if isinstance(rec, dict):
            rows.append(rec)
    latest = latest_ledger_by_id(rows)
    return sum(1 for r in latest.values() if r.get("status") in ("open", "doing"))


def _truth_dir():
    repo = os.environ.get("KG_REPO") or str(_fallback_repo())
    return Path(repo) / "ops" / "truth"


# 产品路线的状态别名。项目自己的 truthctl.py 已经把 in_progress 文档化为合法的
# 「进行中」（ROADMAP_LABEL 里与 doing 同义），但面板/文本/主线摘要只认 doing。
# 2026-11-16 实测后果：ThoughLoom 的 mainline.json 里 5 条路线全是 in_progress，
# 面板因此一条都匹配不上，只能一路退到 next / 首个未完成项 —— 用户看到的「当前阶段」
# 是错的。归一放在数据源头这一处，三个消费点一起修好，不各修一遍。
ROADMAP_STATUS_ALIASES = {
    "in_progress": "doing", "in-progress": "doing", "active": "doing",
    "ongoing": "doing", "wip": "doing", "current": "doing",
    "complete": "done", "completed": "done", "finished": "done", "shipped": "done",
    "todo": "todo", "pending": "todo", "not_started": "todo", "backlog": "todo",
    "next": "next", "planned": "next", "upcoming": "next",
}


def _normalize_roadmap_status(status):
    """把项目侧的状态词归一成面板统一的 done|doing|next|todo。"""
    if not isinstance(status, str):
        return "todo"
    key = status.strip().lower()
    if key in ("done", "doing", "next", "todo"):
        return key
    return ROADMAP_STATUS_ALIASES.get(key, "todo")


def _roadmap_rows(ml):
    """mainline.json 的 roadmap → [{step, expected, status}]，状态已归一。"""
    rows = []
    for it in (ml.get("roadmap") or []):
        if isinstance(it, str):
            rows.append({"step": it, "expected": "", "status": "todo"})
        elif isinstance(it, dict):
            rows.append({"step": it.get("step", ""), "expected": it.get("expected", ""),
                         "status": _normalize_roadmap_status(it.get("status"))})
    return rows


def _roadmap():
    """mainline.json 的产品路线：每一步到哪了。只读、秒级。"""
    return _roadmap_rows(_json_file(_truth_dir() / "mainline.json"))


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
            "quality_gate": item.get("quality_gate"),
        })
    return out


_QUALITY_GATE_STATUSES = {
    "PASS": "pass",
    "RED": "red",
    "HUMAN_PENDING": "human_pending",
    "UNVERIFIED": "unverified",
}


def _quality_gate_unverified(source, detail):
    return {
        "status": "unverified",
        "label": "质量门禁不可读",
        "detail": detail,
        "source": source,
    }


def _quality_gate_report(root, rel, label):
    rel = str(rel or "").strip()
    if not rel:
        raise ValueError(f"质量门禁配置缺少 {label}")
    path = (root / rel).resolve()
    if root.resolve() not in path.parents:
        raise ValueError(f"{label}必须位于项目目录内")
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError(f"{label}不是 JSON 对象")
    return report


def _quality_gate_value(data, path):
    value = data
    for part in str(path or "").split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return None
    return value


def _read_quality_gate(root):
    """Read a project-declared quality gate snapshot without mutating that project.

    Release quality has a different meaning from acceptance count. A project can
    have every engineering item implemented while its strict benchmark remains
    RED and its human review remains pending. Keep those states explicit so a
    downstream folder or panel cannot turn them into a green acceptance count.
    """
    root = Path(root)
    registry = _json_file(_truth_dir() / "project_folders.json")
    folder = next((
        item for item in (registry.get("folders") or [])
        if isinstance(item, dict)
        and Path(str(item.get("path") or "")).expanduser().resolve() == root.resolve()
    ), {})
    spec = folder.get("quality_gate")
    if not isinstance(spec, dict) or not spec.get("report"):
        return None

    try:
        default_rel = str(spec.get("report") or "").strip()
        if not default_rel:
            raise ValueError("质量门禁配置缺少 report")
        reports = {default_rel: _quality_gate_report(root, default_rel, "report")}
        raw_checks = spec.get("checks")
        if not isinstance(raw_checks, list) or not raw_checks:
            raise ValueError("质量门禁配置缺少 checks")
        checks = []
        strict_red = False
        human_pending = False
        for check in raw_checks:
            if not isinstance(check, dict):
                raise ValueError("质量门禁 checks 项不是 JSON 对象")
            label = str(check.get("label") or "").strip()
            path = str(check.get("path") or "").strip()
            if not label or not path:
                raise ValueError("质量门禁 checks 项缺少 label 或 path")
            report_rel = str(check.get("report") or default_rel).strip()
            if report_rel not in reports:
                reports[report_rel] = _quality_gate_report(
                    root, report_rel, "checks.report")
            expected = check.get("equals")
            observed = _quality_gate_value(reports[report_rel], path)
            aggregate = check.get("aggregate")
            if aggregate is not None:
                if not isinstance(aggregate, dict) or aggregate.get("mode") != "count":
                    raise ValueError("质量门禁 checks.aggregate 只支持 count")
                if not isinstance(observed, list) or not aggregate.get("field"):
                    raise ValueError("质量门禁 checks.aggregate 需要数组和 field")
                field = str(aggregate.get("field"))
                wanted = aggregate.get("equals")
                observed = sum(
                    1 for row in observed
                    if isinstance(row, dict)
                    and _quality_gate_value(row, field) == wanted
                )
            matched = observed == expected
            status = "pass" if matched else _QUALITY_GATE_STATUSES.get(
                str(check.get("fail_status") or "RED").upper(), "red")
            if status == "human_pending":
                human_pending = True
            elif status != "pass":
                strict_red = True
            checks.append({
                "label": label,
                "report": report_rel,
                "path": path,
                "expected": expected,
                "observed": observed,
                "status": status,
            })
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _quality_gate_unverified(spec.get("report"), str(exc))

    if strict_red:
        status = "red"
        label = "严格质量 RED"
    elif human_pending:
        status = "human_pending"
        label = "等待真人盲评"
    else:
        status = "pass"
        label = "严格质量通过"
    return {
        "status": status,
        "label": label,
        "source": spec.get("report"),
        "reports": list(reports),
        "checks": checks,
    }


def _read_engineering_radar(root):
    """Read a project's explicit 19-layer engineering snapshot without inventing scores."""
    root = Path(root)
    registry = _json_file(_truth_dir() / "project_folders.json")
    folder = next((
        item for item in (registry.get("folders") or [])
        if isinstance(item, dict)
        and Path(str(item.get("path") or "")).expanduser().resolve() == root.resolve()
    ), {})
    spec = folder.get("engineering_radar")
    if not isinstance(spec, dict) or not spec.get("report"):
        return None
    try:
        report_rel = str(spec.get("report") or "").strip()
        report = _quality_gate_report(root, report_rel, "engineering_radar.report")
        layers = report.get("layers")
        if not isinstance(layers, list) or not layers:
            raise ValueError("工程雷达没有 layers")
        rows = []
        for item in layers:
            if not isinstance(item, dict):
                raise ValueError("工程雷达 layers 项不是 JSON 对象")
            completion = item.get("completionPercent")
            if not isinstance(completion, (int, float)) or isinstance(completion, bool):
                raise ValueError("工程雷达层缺少数字 completionPercent")
            rows.append({
                "layer": str(item.get("layer") or "").strip() or "未命名层",
                "completion": completion,
                "blocker": str(item.get("blocker") or "").strip(),
            })
        verdict = report.get("verdict") if isinstance(report.get("verdict"), dict) else {}
        next_steps = [str(x).strip() for x in (report.get("p0Gaps") or []) if str(x).strip()]
        blockers = [str(x).strip() for x in (report.get("externalBlockers") or []) if str(x).strip()]
        return {
            "source": report_rel,
            "generated_at": report.get("generatedAt"),
            "main_sha": report.get("mainSha"),
            "overall": verdict.get("overallCompletionPercent"),
            "note": verdict.get("note"),
            "public_launch_ready": verdict.get("publicLaunchReady"),
            "layers": rows,
            "next_steps": next_steps[:3],
            "blockers": blockers[:3],
        }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "unverified",
            "label": "工程雷达不可读",
            "detail": str(exc),
            "source": spec.get("report"),
        }


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
    road = _roadmap_rows(ml)
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
        "quality_gate": _read_quality_gate(root),
        "engineering_radar": _read_engineering_radar(root),
    }


def _project_folders(fast=True):
    """项目文件夹：把「项目 → 产品路线 → 工作项 → 验收」归一成面板能扫描的一层。

    当前绑定项目永远会出现；`ops/truth/project_folders.json` 里的其他项目只读它们
    自己的 ops/truth，绝不写回，也不把别的项目工作项混进当前项目。读不到某个项目
    账本时，该文件夹保留并明确标成无法读取，不静默显示 0。
    """
    current = str(Path(os.environ.get("KG_REPO") or _fallback_repo()).expanduser().resolve())
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
            "quality_gate": data.get("quality_gate") or meta.get("quality_gate"),
            "engineering_radar": data.get("engineering_radar"),
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
        "quality_gate": current_meta.get("quality_gate"),
    }, {
        "roadmap": road,
        "current_step": (current_step or {}).get("step") or "路线待拆解",
        "current_step_status": (current_step or {}).get("status") or "todo",
        "doing_items": board.get("doing") or [],
        "todo_items": board.get("todo") or [],
        "human_acceptance": board.get("human") or [],
        "machine_verified": board.get("machine") or [],
        "route_tasks": route_tasks,
        "quality_gate": _read_quality_gate(Path(current)),
        "engineering_radar": _read_engineering_radar(Path(current)),
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
                "quality_gate": None,
                "engineering_radar": None,
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
                    "quality_gate": _read_quality_gate(Path(path)),
                    "engineering_radar": _read_engineering_radar(Path(path)),
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
        project = Path(os.environ.get("KG_REPO") or _fallback_repo()).expanduser().resolve().name
    except (OSError, ValueError):
        project = ""
    return "%s · %s" % (base, project) if project else base


_THREAD_TITLE_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


def _compact_project_label(item, cwd=""):
    """从账本/路径取一个短项目名，供原生对话标题使用。"""
    raw = str(item.get("project") or item.get("project_root") or cwd or "").strip()
    if not raw:
        return "global"
    name = Path(raw).name
    parts = [p for p in _THREAD_TITLE_RE.split(name.lower()) if p]
    if len(parts) > 1:
        short = "".join(p[0] for p in parts)
    else:
        short = name
    short = _THREAD_TITLE_RE.sub("", short).lower()[:12]
    return short or "global"


def _native_thread_prompt(item, prompt, cwd=""):
    """把原生子对话的首句做成可扫读的标题，同时保留完整任务正文。

    Codex 侧栏标题来自首条 user message；当前 app-server 契约没有独立的
    thread/rename，所以把约定格式放在首行是唯一不写宿主库的落地方式。
    """
    body = str(prompt or "").strip()
    if body.startswith("[AP]"):
        return body
    project = _compact_project_label(item, cwd)
    lane = str(item.get("lane") or "route").strip().lower()
    lane = _THREAD_TITLE_RE.sub("", lane)[:12] or "route"
    ledger_id = str(item.get("id") or "").strip()[:24] or "L-0000"
    # 没有账本题名时不用正文首句冒充标题：正文可能只是一句补充说明。
    title = str(item.get("title") or item.get("detail") or "自动挡任务").strip()
    title = " ".join(title.split())[:56] or "自动挡任务"
    return "[AP][%s][%s] %s · %s\n\n%s" % (project, lane, ledger_id, title, body)


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
    # 验收管家必须先自己跑完一遍，才有资格来敲用户的门。判据就是 evidence：
    # 没有机器自验证据的人工验收项，先留在「待自验」，不进「需要你验收」。
    # 否则登记一条 needs_human=true 就能把活直接推给用户，等于让用户当第一道测试。
    human_pending = [r for r in acc
                     if r.get("needs_human") and r.get("status") in ("todo", "wip", "blocked")]
    pre = _pre_acceptance_summary()
    # 有单项 evidence 仍不代表整项产品已经可交付。只有产品级预验收四类检查
    # 全部 ready，才把人工验收送进用户台；否则用户仍然是第一道测试。
    evidence_ready = [r for r in human_pending if str(r.get("evidence") or "").strip()]
    human = evidence_ready if pre.get("notify_user") else []
    self_check = [r for r in human_pending if not str(r.get("evidence") or "").strip()]
    pre_blocked = evidence_ready if not pre.get("notify_user") else []
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
                "evidence": r.get("evidence") or "",
                "screenshots": r.get("screenshots") or [],
                "project_id": r.get("project_id"), "route_id": r.get("route_id"),
                "work_item_id": r.get("work_item_id"),
                "source_thread_id": r.get("source_thread_id") or r.get("thread_id")}

    return {
        "doing": [brief(r) for r in doing[:8]],
        "todo": [brief(r) for r in todo[:8]],
        "todo_count": len(todo),
        "human": [acc_brief(r) for r in human[:8]],
        "self_check": [acc_brief(r) for r in self_check[:8]],
        "self_check_count": len(self_check),
        "pre_acceptance": pre,
        "pre_acceptance_blocked": [acc_brief(r) for r in pre_blocked[:8]],
        "pre_acceptance_blocked_count": len(pre_blocked),
        "machine": [acc_brief(r) for r in machine[:8]],
        "human_done_count": len(human_done),
        "machine_count": len(machine),
    }


def _work_rows(project_name, limit=40):
    """工单状态表：一行一个未决工作项，串起「来源 → 项目 → 泳道 → 分支 → stage → 证据」。

    stage 只由 `ops/stage_table.py` 那一张表算，面板照抄，不许再各算一套。
    没有的证据字段留空，不用编的字符串填空。
    """
    import stage_table  # noqa: E402  只读纯函数，无副作用
    import acceptance_criteria_compiler  # noqa: E402  只读纯函数，无副作用

    latest = {}
    for r in _read_jsonl(_truth_dir() / "ledger.jsonl"):
        if r.get("id"):
            latest[r["id"]] = r
    acc_latest = {}
    for r in _read_jsonl(_truth_dir() / "acceptance.jsonl"):
        if r.get("id"):
            acc_latest[r["id"]] = r
    bindings = _native_route_map()

    def evidence_text(rec, binding):
        ev = rec.get("evidence")
        if isinstance(ev, list) and ev:
            text = str(ev[0])
        else:
            text = ev if isinstance(ev, str) else ""
        text = " ".join(text.split())
        if not text and binding:
            tid = binding.get("thread_id")
            text = ("Codex window " + str(tid)) if tid else ""
        return text[:160]

    rows = []
    for rec in latest.values():
        if rec.get("status") not in ("open", "doing", "todo", "wip", "blocked"):
            continue
        stage = stage_table.item_stage(rec.get("status"))
        binding = bindings.get(rec.get("id"), {})
        rows.append({
            "id": rec.get("id"),
            "title": rec.get("title") or rec.get("detail") or "",
            "kind": rec.get("kind"),
            "source": rec.get("source") or "ledger",
            "project": rec.get("project_id") or project_name,
            "lane": rec.get("lane"),
            "branch": rec.get("branch") or rec.get("lane"),
            "stage": stage,
            "stage_label": stage_table.item_stage_label(stage),
            "evidence": evidence_text(rec, binding),
            "acceptance_summary": acceptance_criteria_compiler.summary_label(
                rec.get("acceptance_plan")),
        })
    for rec in acc_latest.values():
        if not (rec.get("needs_human") and rec.get("status") in ("todo", "wip", "blocked")):
            continue
        stage = stage_table.item_stage(rec.get("status"), needs_human=True)
        rows.append({
            "id": rec.get("id"),
            "title": rec.get("title") or "",
            "kind": "acceptance",
            "source": rec.get("source") or "acceptance",
            "project": rec.get("project_id") or project_name,
            "lane": rec.get("lane"),
            "branch": rec.get("lane"),
            "stage": stage,
            "stage_label": stage_table.item_stage_label(stage),
            "evidence": evidence_text(rec, None),
            "acceptance_summary": acceptance_criteria_compiler.summary_label(
                rec.get("acceptance_plan")),
        })
    # 「要人拍板」的排前面，其次进行中，再次待处理；同组按 id。
    prio = {"person/review": 0, stage_table.BUCKET_DOING: 1,
            stage_table.BUCKET_STUCK: 1, stage_table.BUCKET_TODO: 2}
    rows.sort(key=lambda r: (prio.get(r["stage"], 3), r.get("id") or ""))
    return rows[:limit]


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


def _answer_needed_count():
    """判不清归属、还没人工复核指派的投稿条数。

    这些需求没进任何项目账本，只在待路由箱里——面板必须让它们露面，否则等于丢了。
    已复核并写进目标项目的记录留原始凭证不删，但不再占用用户注意力；同一句诉求
    反复投递也只算一项。取不到回 None，绝不成 0：面板要显示「取不到」，
    不能把失明说成没有待复核项。
    """
    try:
        import project_router
        path = project_router.routing_inbox_path()
        if not path.exists():
            return 0
        return len(project_router.pending_routing_records(path))
    except Exception:
        return None


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
            info["title"] = (r.get("title") or "").strip()
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
        if probe.get("blocked_by") == "remote-control-chatgpt-pairing":
            state["label"] = "原生直连：需 ChatGPT 登录/配对后才能派活"
        else:
            state["label"] = "原生直连：不可派活，只能只读历史"
    return state


def _status_payload(fast=True):
    """把控制开关 + 缓存状态 + 项目计数，拼成面板和模型都能用的一个 JSON。"""
    from datetime import datetime, timezone
    ctrl = control_state.load()
    health = binding_health()
    if health.get("status") == "broken":
        line = binding_line(health)
        payload = {
            "ok": False,
            "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "fast": bool(fast),
            "paused": bool(ctrl.get("paused")),
            "enabled": False,
            "lifecycle": "error",
            "error": line,
            "state": "binding_broken",
            "state_label": "安装绑定失效，自动挡不可用",
            "doing": "没有在动任何项目：当前绑定的状态根不是一个可用的项目账本",
            "why": line,
            "recent": "需求不会被可靠落账；这不是「没有待办」，是自动挡没接上项目",
            "need_action": "重跑 ./install.sh <真实 git 仓库根目录>；先不要继续提需求",
            "need_action_detail": "安装绑定失效的详细路径与修复步骤见工程详情；不会自动改绑定。",
            "project": {
                "path": health.get("repo"),
                "name": Path(health.get("repo")).name if health.get("repo") else None,
            },
            "recent_completed": [],
            "switches": {
                "auto_land": bool(ctrl.get("auto_land", True)),
                "auto_resolve": bool(ctrl.get("auto_resolve", True)),
                "auto_port": bool(ctrl.get("auto_port", False)),
                "auto_exec": bool(ctrl.get("auto_exec", False)),
                "daemon_enabled": bool(ctrl.get("daemon_enabled", True)),
            },
            "daemon": {
                "live": False,
                "job_running": False,
                "pid": None,
                "project": None,
                "last_cycle": None,
                "interval_min": None,
            },
            "native": _native_status(),
            "counters": {
                "received": None,
                "open_items": None,
                "landed": None,
                "resolved": None,
                "superseded": None,
                "ported": None,
            },
            "queue": {
                "clean": None,
                "merged": None,
                "conflict": None,
                "unresolved": None,
            },
            "exec_now": None,
            "blocked": {"failures": [], "resolve": [], "port": [], "exec": []},
            "resolve_tasks": [],
            "events": control_state.read_events(30),
            "product_state": [],
            "judgments": [],
            "last_verification": None,
            "closure_rate": _closure_rate_summary(),
            "project_health": {
                "status": "unknown",
                "level": "unknown",
                "headline": "项目健康取不到：安装绑定失效",
                "layers": [],
                "signals": {},
            },
            "route_health": {
                "status": "ok",
                "level": "unknown",
                "headline": "路线与效能判据取不到：安装绑定失效",
                "unknown": ["安装绑定失效"],
                "reasons": [],
                "action": "none",
            },
            "global_routing": {
                "status": "unknown",
                "count": None,
                "items": [],
                "projects": [],
                "error": "安装绑定失效",
                "text": "全局待路由箱取不到：安装绑定失效",
            },
            "runtime_freshness": {
                "status": "ok",
                "level": "unknown",
                "headline": "运行态判据取不到：安装绑定失效",
                "unknown": ["安装绑定失效"],
                "reasons": [],
                "action": "none",
            },
            "mainline": {},
            "route": {},
            "roadmap": [],
            "project_folders": [],
            "doing_items": [],
            "todo_items": [],
            "todo_count": None,
            "human_acceptance": [],
            "pre_acceptance": {"status": "unknown", "label": "验收管家：安装绑定失效，不通知你验收",
                                "checks": [], "categories": [], "items": [], "passed": 0,
                                "failed": 0, "unknown": 92, "not_applicable": 0,
                                "total": 92, "notify_user": False},
            "pre_acceptance_blocked": [],
            "pre_acceptance_blocked_count": None,
            "machine_verified": [],
            "human_accepted_count": None,
            "binding": health,
            "stewards": _steward_health({}),
            "autonomy": _autonomy_summary({}),
            "pm_briefing": _pm_briefing_summary({"blocked": {}}),
            "authorized_followups": _authorized_followup_summary(),
            "context_archive": _context_archive_summary(),
            "relationship_goals": _relationship_goals_summary(),
            "outcome_score": _outcome_score_summary(),
        }
        payload["__text__"] = (
            "自动挡 · 安装绑定失效\n"
            "正在做：" + payload["doing"] + "\n"
            "为什么：" + line + "\n"
            + payload["recent"] + "\n"
            + payload["need_action"]
        )
        return payload
    job_running, pid, daemon_project = _daemon_live()
    current_project = str(Path(os.environ.get("KG_REPO") or _fallback_repo()).expanduser().resolve())
    live = bool(job_running and daemon_project == current_project)
    daemon_enabled = bool(ctrl.get("daemon_enabled", True))
    daemon_problem, daemon_headline, daemon_detail = _daemon_liveness_action(daemon_enabled)
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

    repo = os.environ.get("KG_REPO") or str(_fallback_repo())
    try:
        autonomy = project_autonomy.summary(repo)
    except Exception as exc:
        autonomy = {"configured": True, "mode": "paused",
                     "reason": "项目运行状态读取失败：%s" % exc}

    exec_now = _running_exec() if live else None

    # 「现在处在哪一步」只由 stage_table 里那一张 first-match-wins 表说了算。
    # 面板的 body.s-*、state_label、四个计数格子全部来自这一次判定；CLI / doctor
    # 想要同一句话也调同一个函数，不再各写一条 if 链。
    import stage_table  # noqa: E402  只读纯函数，无副作用
    # 「需要你拍板」= 真的合不进去、必须人定的那些（原判定只用 land failures）。
    # resolve / port 那两条队列是**机器自己在推**的，把它们也算成「要你拍板」会把
    # 正常现场抬成警报——这里保持原语义，不动。
    blocked_n = len(failures)
    # 面板第 4 格「卡住」沿用原口径：failures + resolve + exec（port 仍只进明细面板）。
    # `overlap_hold` 是排队让路，不是卡住：它在等前一条落地，下一拍就会重新判。
    # 把它算进「卡住」会让正常调度把首屏抬成报警，用户白看一次不需要他处理的事。
    exec_blocked_all = dict(st.get("exec_blocked") or {})
    overlap_holds = {k: v for k, v in exec_blocked_all.items()
                     if isinstance(v, dict) and v.get("reason") == "overlap_hold"}
    exec_blocked_stuck = {k: v for k, v in exec_blocked_all.items()
                          if k not in overlap_holds}
    stuck_n = len(failures) + len(blocked) + len(exec_blocked_stuck)
    review_n = len(board["human"])
    global_routing = _global_routing_summary()
    stage = stage_table.panel_stage({
        "error": ctrl.get("error"),
        "enabled": enabled,
        "paused": paused,
        "exec_running": bool(exec_now),
        "blocked_count": blocked_n,
        "review_count": review_n,
        "answer_count": global_routing.get("count"),
        "daemon_live": bool(live),
        "daemon_other_project": bool(
            job_running and daemon_project and daemon_project != current_project),
        "queued_count": (1 if (summary.get("clean") or summary.get("merged")) else 0),
    })
    state_key, state_label = stage["ui_state"], stage["label"]
    counts = stage_table.stage_counts(
        doing=len(board["doing"]), todo=board["todo_count"],
        review=review_n, stuck=stuck_n, exec_running=bool(exec_now))

    # 大白话的「正在做什么 / 为什么 / 最近做了什么 / 需要你做什么」。
    if not enabled:
        doing = "自动挡已关闭；Codex 原生会话、项目与工作区保持不变"
        why = "总开关关闭后不再接收或启动自动挡工作"
    elif autonomy.get("configured") and autonomy.get("mode") == "waiting_human":
        doing = "项目已停在需要你确认的位置，没有启动新的工作"
        why = autonomy.get("reason") or "这一步涉及产品方向、发布、删除数据或人工验收"
    elif autonomy.get("configured") and autonomy.get("mode") == "paused":
        doing = "项目暂停中，没有启动新的 worker"
        why = autonomy.get("reason") or "项目自主运行被暂停；已有现场会保留"
    elif autonomy.get("configured") and autonomy.get("mode") == "enabled":
        doing = "项目已登记，但还没有开始持续推进"
        why = "把项目切到“运行”后，自动挡才会持续规划、派活、验收和合并"
    elif autonomy.get("configured") and autonomy.get("mode") == "disabled":
        doing = "项目自主运行已关闭，没有启动新的 worker"
        why = autonomy.get("reason") or "重新启用后才会恢复项目级运行授权"
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
        doing = "后台正在服务别的项目；当前项目会等自己的轮次"
        why = "常驻守护按项目分开，当前项目只走自己的进度"
    else:
        doing = "已检查一轮，当前没有能安全自动合入的分支"
        why = "和 main 冲突的、或门禁没过的，不会被自动合，等你处理"

    last = st.get("last_landed") or []
    if last:
        recent = f"最近自动合入 {len(last)} 个分支：{', '.join(last[:3])}" + (
            " 等" if len(last) > 3 else "")
    else:
        recent = "还没有自动合入过分支"

    need_action_detail = ""
    if not enabled:
        need_action = "需要继续使用自动挡时，重新开启总开关"
    elif daemon_problem:
        # 常驻静默死亡排最前：开关全绿而进程不在，自动挡一步都不会自己走。
        # 它必须盖过「等验收」这类正常待办——那些事项在「需要你介入」卡里仍然列着，
        # 但用户首先该知道的是「自动」这部分根本没在跑。
        need_action = daemon_headline or daemon_problem
        need_action_detail = daemon_detail or daemon_problem
    elif failures:
        need_action = f"需要你拍板：{failures[0]['target']}（{failures[0]['kind']}）"
    elif board["human"]:
        need_action = f"需要你验收：{board['human'][0].get('title') or '请打开面板查看验收步骤'}"
    elif global_routing.get("status") == "ok" and int(global_routing.get("count") or 0) > 0:
        need_action = f"需要你复核：全局待路由 {global_routing['count']} 条判不清归属的需求"
        need_action_detail = (
            "这些原始投稿没有进任何项目账本。确认指派只会追加目标项目 inbox 和复核凭证，"
            "原始待路由记录不删不改；不确认就继续留在这条提醒里。"
        )
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
        "project_autonomy": autonomy,
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
        "need_action_detail": need_action_detail,
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
            "problem": daemon_problem,
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
                     for k, v in list(exec_blocked_stuck.items())[:20]],
            "exec_waiting": [{"id": k, "reason": (v or {}).get("reason"),
                              "holders": (v or {}).get("holders") or [],
                              "files": (v or {}).get("files") or []}
                             for k, v in list(overlap_holds.items())[:20]],
        },
        "resolve_tasks": rtasks[:20],
        "events": control_state.read_events(30),
        "product_state": _product_state(),
        "judgments": _active_judgments(),
        "last_verification": _last_verification(),
        "closure_rate": _closure_rate_summary(),
        "harness_health": _harness_health(),
        # 全局项目健康：项目真相 / 执行流动 / 验收三层。与 harness_health 分开。
        "project_health": _project_health_summary({
            "switches": {
                "auto_exec": bool(ctrl.get("auto_exec", False)),
            },
            "blocked": {
                "exec": [{"id": k, "reason": (v or {}).get("reason")}
                         for k, v in list(exec_blocked_stuck.items())[:20]],
                "exec_waiting": list(overlap_holds),
                "failures": failures,
                "resolve": list(blocked),
            },
        }),
        # PM 简报：只读聚合「预期磨到哪、谁给谁让路、哪条该追进度」。
        # 与项目健康同源运行面数据，但不新增台账、不派活、不自动追问。
        "pm_briefing": _pm_briefing_summary({
            "blocked": {
                "exec": [{"id": k, "reason": (v or {}).get("reason")}
                         for k, v in list(exec_blocked_stuck.items())[:20]],
                "exec_waiting": list(overlap_holds),
                "failures": failures,
                "resolve": list(blocked),
            },
        }),
        # 追问授权账本与 PM 候选分开：候选只读，授权逐项显式，daemon 再校验后投递。
        "authorized_followups": _authorized_followup_summary(),
        # 关系层：跨项目长期目标是显式账本，不属于某个项目真相；这里只读摘要，
        # 不把目标自动派给 worker，也不把「需要人」变成自动追问。
        "relationship_goals": _relationship_goals_summary(),
        "outcome_score": _outcome_score_summary(),
        "harness_tune": _harness_tune_summary(),
        "constraint_candidates": _constraint_candidates_summary(),
        "context_archive": _context_archive_summary(),
        "acceptance_criteria": _acceptance_criteria_summary(),
        "autonomy": _autonomy_summary(st),
        "mainline": _mainline_summary(),
        # fast=True 是面板/开关那条路：只读缓存，不连宿主，绝不因此变慢。
        "route": _route_summary(timeout=0.0 if fast else None),
        "roadmap": _roadmap(),
        "project_folders": _project_folders(),
        "doing_items": board["doing"],
        "todo_items": board["todo"],
        "todo_count": board["todo_count"],
        "human_acceptance": board["human"],
        "pre_acceptance": board["pre_acceptance"],
        "pre_acceptance_blocked": board["pre_acceptance_blocked"],
        "pre_acceptance_blocked_count": board["pre_acceptance_blocked_count"],
        # 「还没自验完的人工验收项」：先自己跑一遍，再拿来打扰用户。
        "self_check_acceptance": board["self_check"],
        "self_check_count": board["self_check_count"],
        "machine_verified": board["machine"],
        "human_accepted_count": board["human_done_count"],
        # 全局待路由箱是跨项目的，不属于当前绑定项目；状态摘要与面板复核区同源。
        "global_routing": global_routing,
        # stage_table 的同一个结论：面板拿它填格子与状态，不必再自己算一遍。
        "stage": stage["stage"],
        "stage_owner": stage["owner"],
        "counts": counts,
        # 工单状态表：控制层把「来源 → 项目 → 泳道 → 分支 → stage → 证据」拼好，
        # 面板只渲染，不再自己算 stage（唯一判据仍是 ops/stage_table.py）。
        "work_rows": _work_rows(Path(current_project).name),
    }
    # 路线与效能是状态管家的内部只读检测器，不新增管家、不新增卡片。
    payload["route_health"] = _route_health_summary(payload, st)
    # 源码 / 已装版本 / 运行代码是否同一代，仍是状态管家内部判据。只提示
    # 受控换代或重载，绝不自动重启、改配置、派工单。
    payload["runtime_freshness"] = _runtime_freshness_summary(health)
    # 七管家健康条 + 全局项目健康：一眼看出哪个管家断了、项目整体是否还健康。
    # 必须等 payload 构造完再算——判据全来自上面这些字段，不能引用还在构造的自己。
    payload["stewards"] = _steward_health(payload)
    payload["__text__"] = _status_text(payload)
    return payload


def _daemon_switch_label(enabled, daemon):
    """后台常驻这一格反映「是不是真在跑」，不是照念配置位。

    2026-11-11 实测：control.json 写着 daemon_enabled=true，launchd 里那个 job 根本
    不存在、ps 里没有 central_daemon，状态面却仍打印「后台常驻=开」。用户看到的是
    「开着」，实际一步没动 —— 这正是「感觉不到增益」的机械根因。

    三态：关 / 开 / 开但没在跑。没在跑时点明「没进程接活」，让配置位和现实分开。
    """
    if not enabled:
        return "关"
    if (daemon or {}).get("live"):
        return "开"
    return "开（但没进程接活）"


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
        # 自动实现排在最前：它是「需求会不会被真的写出来」这一格。少了它，
        # 一行全绿却一步没实现——和「守护没在跑却显示开着」是同一个坑。
        "开关：自动实现=%s  自动合入=%s  自动拆冲突=%s  自动移植=%s  后台常驻=%s"
        % ("开" if sw["auto_exec"] else "关（只记账，不实现）",
           "开" if sw["auto_land"] else "关",
           "开" if sw["auto_resolve"] else "关",
           "开" if sw["auto_port"] else "关",
           _daemon_switch_label(sw.get("daemon_enabled"),
                                payload.get("daemon"))),
        "队列：可合 %s · 已合待回收 %s · 冲突 %s"
        % (q.get("clean"), q.get("merged"), q.get("conflict")),
        "累计：收到反馈 %s 条 · 自动合入 %s 个 · 拆解冲突 %s 条"
        % (payload["counters"]["received"], payload["counters"]["landed"],
           payload["counters"]["resolved"]),
    ]
    _dp = (payload.get("daemon") or {}).get("problem")
    if _dp:
        # 静默死亡排在最前：开关全绿而进程不在，是用户最容易被骗的一格。
        lines.append("常驻守护：" + _dp.split("：", 1)[-1])
    en = payload.get("exec_now")
    if en:
        _m = int((en.get("elapsed_sec") or 0) // 60)
        lines.append("执行中：%s %s（已 %d 分钟，独立工作区改代码，自测通过后自动合入）"
                     % (en.get("id") or "", en.get("title") or "", _m))
    rh = payload.get("route_health") or {}
    if rh.get("level") in ("attention", "unknown"):
        lines.append("路线与效能：%s（仅提复核，不自动纠偏）"
                     % (rh.get("headline") or "取不到"))
    rf = payload.get("runtime_freshness") or {}
    if rf.get("level") in ("attention", "unknown"):
        lines.append("运行态新鲜度：%s（只提示受控换代，不自动重启）"
                     % (rf.get("headline") or "取不到"))
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
        daemon = _live_ops() / "central_daemon.py"
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
        daemon = _live_ops() / "central_daemon.py"
        want = action == "enable_daemon"
        out, rc = _run([str(daemon), "--install" if want else "--uninstall"], timeout=120)
        if rc == 0:
            control_state.set_keys(daemon_enabled=want)
            control_state.append_event("daemon", enabled=want)
        return _with_status(out or ("已%s常驻" % ("开启" if want else "关闭"))), rc
    return "未知 action：%s" % action, 2


def project_autonomy_action(action, reason=""):
    modes = {"enable": "enabled", "run": "running", "pause": "paused",
             "wait": "waiting_human", "disable": "disabled"}
    if action not in modes:
        return "project_autonomy_control 需要 enable/run/pause/wait/disable", 2
    repo = os.environ.get("KG_REPO") or str(REPO)
    if action == "disable":
        reason = reason or "项目自主运行已关闭"
    elif action == "run":
        reason = reason or "用户已授权项目持续推进"
    elif action == "pause":
        reason = reason or "用户已暂停项目自主运行"
    elif action == "wait":
        reason = reason or "等待用户确认"
    try:
        state = project_autonomy.set_mode(repo, modes[action], reason)
        control_state.append_event("project_autonomy", project=state["project"],
                                   mode=state["mode"], reason=state["reason"])
        return _with_status("当前项目已%s。" % {
            "disabled": "关闭自主运行", "enabled": "启用但未运行",
            "running": "开始持续运行", "paused": "暂停",
            "waiting_human": "停在等待确认"}[state["mode"]]), 0
    except (OSError, ValueError) as exc:
        return "项目自主运行状态未更新：%s" % exc, 2


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
        "harness_tune_list", "constraint_candidates_list", "context_archive",
        "acceptance_criteria_list", "route_preview",
        "followup_list",
        "routing_review_list", "pm_research_status", "pm_execution_gate",
    }
    # 预演不写盘，但确认指派是可写动作；因此它走自己的显式 guard，不能塞进
    # 上面的 read_only_tools，也不能让通用 guard 连预演一起拦掉。
    explicit_guard_tools = {"routing_review_assign"}
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
            item = _ledger_item(ledger_id)
            if not item:
                return {
                    "code": "AUTOPILOT_LEDGER_ITEM_NOT_DISPATCHABLE",
                    "message": "只能给自动挡台账中仍未收口的问题派活。",
                }, 2
            cwd = (args.get("cwd") or "").strip()
            result, error = codex_native.dispatch_task(
                cwd,
                _native_thread_prompt(item, args.get("prompt"), cwd),
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
    if name not in read_only_tools | explicit_guard_tools | control_tools:
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
               "KG_INBOX_KIND": args.get("kind") or "unknown",
               "KG_INBOX_CWD": (args.get("cwd") or "").strip() or None,
               "KG_INBOX_PROJECT": (args.get("project") or "").strip() or None}
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
    if name == "routing_review_list":
        summary = _global_routing_summary()
        return {"status": summary["status"], "count": summary["count"],
                "items": summary["items"], "projects": summary["projects"],
                "path": summary.get("path"), "error": summary.get("error"),
                "__text__": summary["text"]}, 0
    if name == "routing_review_assign":
        fingerprint = str(args.get("fingerprint") or "").strip()
        project = str(args.get("project") or "").strip()
        reason = str(args.get("reason") or "").strip()
        if not fingerprint or not project:
            return {"code": "ROUTING_REVIEW_FIELDS_REQUIRED",
                    "message": "指派必须同时给出 fingerprint 和 project；"
                               "先用 routing_review_list 看指纹。",
                    "__text__": "指派缺少指纹或项目；没有写任何账本。"}, 2
        confirmed = bool(args.get("confirm"))
        argv = ["route-inbox", "--fingerprint", fingerprint,
                "--project", project, "--reason",
                reason or "面板复核确认归属", "--json"]
        if confirmed:
            guard = control_state.mutation_guard()
            if guard:
                return guard, 2
            argv.append("--go")
        raw, code = kgctl(*argv)
        data = _json_line(raw)
        if code == 0 and isinstance(data, dict):
            written = bool(data.get("written"))
            return {**data,
                    "__text__": ("已追加复核凭证；原待路由记录未删未改。"
                                 if written else
                                 "预演完成，未写任何账本；确认后才追加。")}, 0
        return {"code": "ROUTING_REVIEW_ASSIGN_FAILED",
                "message": raw, "__text__": "指派未完成：" + str(raw)[:300]}, code or 2
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
    if name == "constraints_add":
        statement = (args.get("statement") or "").strip()
        if not statement:
            return "constraints_add 需要 statement", 2
        env = {"KG_CONSTRAINT_KIND": args.get("kind") or None,
               "KG_CONSTRAINT_SCOPE": args.get("scope") or None,
               "KG_CONSTRAINT_ENFORCEABLE": args.get("enforceable") or None,
               "KG_CONSTRAINT_CMD": args.get("cmd") or None,
               "KG_CONSTRAINT_CWD": args.get("cwd") or None,
               "KG_CONSTRAINT_POLICY": args.get("policy_version") or None,
               "KG_CONSTRAINT_SUPERSEDES": args.get("supersedes") or None}
        return truthctl("constraints", "add", statement, extra_env=env)
    if name == "constraints_list":
        argv = ["constraints"]
        files = args.get("files") or []
        if files:
            argv += ["--files", ":".join(files)]
        return truthctl(*argv)
    if name == "close_item":
        rid = (args.get("id") or "").strip()
        if not rid:
            return "close_item 需要 id（如 L-0003）", 2
        env = {"KG_TRUTH_COMMIT": args.get("commit") or None,
               "KG_TRUTH_EVIDENCE": args.get("evidence") or None}
        if args.get("confirm_human"):
            env["KG_TRUTH_HUMAN_CONFIRMED"] = "1"
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
               "KG_TRUTH_EVIDENCE": args.get("evidence") or None,
               "KG_TRUTH_SHOTS": _screenshots_env(args.get("screenshots"))}
        return truthctl(*argv, extra_env=env)
    if name == "resolve_acceptance":
        rid = (args.get("id") or "").strip()
        status = (args.get("status") or "").strip()
        if not rid:
            return "resolve_acceptance 需要 id（如 A-0030）", 2
        if status not in ("todo", "wip", "blocked", "verified", "void"):
            return "resolve_acceptance 需要 status（todo/wip/blocked/verified/void）", 2
        argv = ["accept", "--id", rid, "--status", status]
        if args.get("note"):
            argv += ["--note", args["note"]]
        env = {"KG_TRUTH_EVIDENCE": args.get("evidence") or None,
               "KG_TRUTH_SHOTS": _screenshots_env(args.get("screenshots")),
               "KG_TRUTH_SOURCE": "user"}
        return truthctl(*argv, extra_env=env)
    if name == "capture_acceptance_screenshot":
        # 这是人点按钮后才允许进来的动作。confirm 不是装饰：模型/守护/探针
        # 即使拿到的工具参数里没有它，也不能借这条链路调系统截图器。
        if not bool(args.get("confirm")):
            return {"code": "SCREENSHOT_CONFIRM_REQUIRED",
                    "message": "补图必须由人点按钮触发；请带 confirm=true。"}, 2
        rid = (args.get("id") or "").strip()
        if not rid:
            return {"code": "SCREENSHOT_ID_REQUIRED",
                    "message": "capture_acceptance_screenshot 需要验收 id（如 A-0030）。"}, 2
        record = _acceptance_record(rid)
        if record is None:
            return {"code": "SCREENSHOT_ACCEPTANCE_NOT_FOUND",
                    "message": "验收台账里没有 %s；先登记验收项。" % rid}, 2
        if not _capture_ready_acceptance(record):
            return {"code": "SCREENSHOT_NOT_HUMAN_READY",
                    "message": "这条还没到人工复核：必须有机器自验证据且仍待人工确认；"
                               "截图不能代替自验。"}, 2
        try:
            import acceptance_screenshot
            shot = acceptance_screenshot.capture(rid)
        except Exception as exc:
            return {"code": "SCREENSHOT_CAPTURE_FAILED",
                    "message": "截图失败：%s" % exc}, 2
        if shot.get("error"):
            return {"code": "SCREENSHOT_CAPTURE_FAILED",
                    "message": shot["error"]}, 2
        status = record.get("status") or "todo"
        out, code = truthctl(
            "accept", "--id", rid, "--status", status,
            extra_env={"KG_TRUTH_SHOTS": shot["path"],
                       "KG_TRUTH_SOURCE": "panel-screenshot"})
        if code != 0:
            return {"code": "SCREENSHOT_APPEND_FAILED",
                    "message": "截图已生成，但没写进验收台账：%s" % out}, 2
        control_state.append_event("acceptance_screenshot", id=rid, path=shot["path"])
        return {"__text__": "已给 %s 补上复核截图；验收状态仍是 %s，没有替你收口。" % (rid, status),
                "id": rid, "path": shot["path"], "status": status}, 0
    if name == "reopen_item":
        rid = (args.get("id") or "").strip()
        if not rid:
            return "reopen_item 需要 id（如 L-0003）", 2
        return truthctl("open", rid, args.get("reason") or "")
    if name in ("harness_tune_list", "harness_tune_apply", "harness_tune_rollback"):
        import harness_tune
        if name == "harness_tune_list":
            pending = harness_tune.pending()
            if not pending:
                return ("状态管家：当前没有待你点头的 harness 调整。（config.toml 一个字都没动）",
                        0)
            return harness_tune.human(), 0
        rid = (args.get("id") or "").strip()
        if not rid:
            return f"{name} 需要 id（如 T-0001）", 2
        if name == "harness_tune_apply":
            # confirm 就是「人已明确同意」的语义；不给就一律拒绝，绝不自动改配置。
            res = harness_tune.apply(rid, confirm=bool(args.get("confirm")))
        else:
            res = harness_tune.rollback(rid)
        return json.dumps(res, ensure_ascii=False, indent=1), (
            0 if res.get("status") != "error" else 2)
    if name == "context_archive":
        import context_archive
        data = context_archive.collect()
        return context_archive.human(data), (0 if data.get("status") == "ok" else 1)
    if name in ("constraint_candidates_list", "constraint_candidate_promote",
                "constraint_candidate_dismiss"):
        import constraint_candidates
        if name == "constraint_candidates_list":
            rows = constraint_candidates.pending()
            if not rows:
                return ("上下文管家：当前没有待你确认的约束候选。"
                        "（生效约束台账一个字都没动）", 0)
            return constraint_candidates.human(), 0
        cid = (args.get("id") or "").strip()
        if not cid:
            return f"{name} 需要 id（如 CC-0001）", 2
        if name == "constraint_candidate_promote":
            # confirm 就是「人已明确同意」的语义；不给就一律拒绝，绝不自动写生效约束。
            if not bool(args.get("confirm")):
                return ("升成生效约束必须由人明确点头：请带 confirm=true（或直接在面板上点同意）。"
                        "候选本身不影响任何门禁。", 2)
            res = constraint_candidates.promote(cid)
        else:
            res = constraint_candidates.dismiss(cid, args.get("reason") or "")
        return json.dumps(res, ensure_ascii=False, indent=1), (
            0 if res.get("status") != "error" else 2)
    if name in ("acceptance_criteria_list", "acceptance_criteria_promote",
                "acceptance_criteria_dismiss"):
        import acceptance_criteria
        if name == "acceptance_criteria_list":
            rows = acceptance_criteria.pending()
            if not rows:
                return ("验收管家：当前没有待确认的验收标准候选。"
                        "（验收台账一个字都没动）", 0)
            return acceptance_criteria.human(), 0
        cid = (args.get("id") or "").strip()
        if not cid:
            return f"{name} 需要 id（如 AC-0001）", 2
        if name == "acceptance_criteria_promote":
            # confirm 就是「人已明确同意」的语义；不点头绝不登记验收项，
            # 更不会把候选直接标成 verified 或塞进「需要你验收」。
            if not bool(args.get("confirm")):
                return ("登记验收项必须由人明确点头：请带 confirm=true（或直接在面板上点确认）。"
                        "候选本身不写验收台账，也不伪造证据。", 2)
            res = acceptance_criteria.promote(
                cid, confirm=True,
                needs_human=args.get("needs_human"),
                steps=args.get("steps") or "", level=args.get("level") or "")
        else:
            res = acceptance_criteria.dismiss(cid, args.get("reason") or "")
        return json.dumps(res, ensure_ascii=False, indent=1), (
            0 if res.get("status") != "error" else 2)
    if name in ("followup_authorize", "followup_cancel", "followup_list"):
        import authorized_followup as followup
        root = Path(os.environ.get("KG_REPO") or _fallback_repo())
        if name == "followup_list":
            return followup.summary(root), 0
        if name == "followup_authorize":
            if not bool(args.get("confirm")):
                return {"code": "FOLLOWUP_CONFIRM_REQUIRED",
                        "message": "逐项追问必须由人明确点头：请带 confirm=true；候选本身不会发消息。"}, 2
            try:
                res = followup.approve({
                    "ticket_id": args.get("ticket_id"),
                    "thread_id": args.get("thread_id"),
                    "prompt": args.get("prompt"),
                    "reason": args.get("reason"),
                    "authorization": "explicit",
                    "effect": "reversible",
                    "source": "panel",
                }, root)
            except followup.AuthorizationError as exc:
                return {"code": "FOLLOWUP_AUTHORIZATION_REJECTED", "message": str(exc)}, 2
            return {"__text__": "已登记这条追问授权；后台发送前还会再校验一次窗口和工作项。",
                    **res}, 0
        try:
            res = followup.cancel(
                (args.get("request_id") or "").strip(),
                args.get("reason"),
                root,
            )
        except followup.AuthorizationError as exc:
            return {"code": "FOLLOWUP_CANCEL_REJECTED", "message": str(exc)}, 2
        return {"__text__": "已撤销未发送的追问授权；历史授权记录保留。", **res}, 0
    if name in {"pm_research_status", "pm_execution_gate", "pm_research_request",
                "pm_research_evidence", "pm_research_option", "pm_product_decide"}:
        import pm_decision
        import research
        root = Path(os.environ.get("KG_REPO") or _fallback_repo())
        try:
            if name == "pm_research_status":
                rid = (args.get("research_id") or "").strip()
                result = (pm_decision.brief(root) if not rid else {
                    "research": research.summary(root, rid),
                    "recommendation": pm_decision.recommendation(root, rid),
                    "gate": pm_decision.execution_gate(root, research_id=rid),
                })
                return result, 0
            if name == "pm_execution_gate":
                return pm_decision.execution_gate(
                    root, intent_id=args.get("intent_id"), research_id=args.get("research_id")), 0
            if name == "pm_research_request":
                result = research.create_request(
                    root, intent_id=args.get("intent_id"), question=args.get("question"),
                    scope=args.get("scope") or [], research_level=args.get("research_level") or "L2",
                    risk_level=args.get("risk_level") or "medium", source="mcp")
                return result, 0
            if name == "pm_research_evidence":
                result = research.add_evidence(
                    root, research_id=args.get("research_id"), source_url=args.get("source_url"),
                    source_kind=args.get("source_kind") or "unknown", title=args.get("title") or "",
                    claims=args.get("claims") or [], license_status=args.get("license_status") or "unknown",
                    maintenance_signal=args.get("maintenance_signal") or "unknown",
                    relevance=args.get("relevance") or "unknown", confidence=args.get("confidence") or "unknown",
                    notes=args.get("notes") or "", source="mcp")
                return result, 0
            if name == "pm_research_option":
                result = research.add_option(
                    root, research_id=args.get("research_id"), name=args.get("name"),
                    disposition=args.get("disposition") or "adapt", rationale=args.get("rationale") or "",
                    capabilities=args.get("capabilities") or [], gaps=args.get("gaps") or [],
                    license_status=args.get("license_status") or "unknown",
                    confidence=args.get("confidence") or "unknown", selected=bool(args.get("selected")),
                    source="mcp")
                return result, 0
            recommendation = pm_decision.recommendation(
                root, args.get("research_id"))
            needs_confirmation = recommendation.get("approval_mode") == "user_required"
            if needs_confirmation and not bool(args.get("confirm")):
                return {"code": "PM_DECISION_CONFIRM_REQUIRED",
                        "message": "产品决策需要 confirm=true；未确认不能进入施工。"}, 2
            result = pm_decision.decide(
                root, research_id=args.get("research_id"), choice=args.get("choice"),
                selected_option=args.get("selected_option") or "", why=args.get("why") or "",
                alternatives=args.get("alternatives") or [], approved=bool(args.get("confirm")), source="mcp")
            return result, 0
        except (TypeError, ValueError) as exc:
            return {"code": "PM_RESEARCH_INVALID", "message": str(exc)}, 2
    if name == "suggest_branch":
        return kgctl("suggest")
    if name == "route_preview":
        text = (args.get("text") or "").strip()
        if not text:
            return "route_preview 需要 text", 2
        files = args.get("files") or []
        env = {"KG_INBOX_FILES": ":".join(files) if files else None,
               "KG_INBOX_CWD": (args.get("cwd") or "").strip() or None,
               "KG_INBOX_PROJECT": (args.get("project") or "").strip() or None}
        raw, code = kgctl("route-preview", text, extra_env=env)
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {"decision": "unknown", "project": None,
                    "__text__": "落点取不到；不会假装正常", "detail": raw}, 2
        decision = data.get("decision")
        project = data.get("project")
        reasons = [str(r) for r in (data.get("reasons") or []) if r]
        if decision == "resolved" and project:
            message = "将落到：" + str(project)
        elif decision == "ambiguous":
            message = "判不清落点；提交会进全局待路由箱，请在原话里点名项目"
        elif decision == "none":
            if project:
                message = ("默认落到当前绑定项目：" + str(project) +
                           "；如不是，请在原话里点名项目")
            else:
                message = "落点判不清：没有可用线索；请点名项目"
        else:
            message = "落点取不到；不会假装正常"
        result = dict(data)
        result["__text__"] = message
        if reasons:
            result["reasons"] = reasons
        return result, (0 if code == 0 else 2)
    if name == "intake_request":
        guard = control_state.mutation_guard()
        if guard:
            return guard, 2
        text = (args.get("text") or "").strip()
        if not text:
            return "intake_request 需要 text", 2
        files = args.get("files") or []
        env = {"KG_INBOX_FILES": ":".join(files) if files else None,
               "KG_INBOX_KIND": args.get("kind") or "unknown",
               "KG_INBOX_CWD": (args.get("cwd") or "").strip() or None,
               "KG_INBOX_PROJECT": (args.get("project") or "").strip() or None}
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
    if name == "project_autonomy_status":
        repo = os.environ.get("KG_REPO") or str(REPO)
        try:
            payload = project_autonomy.summary(repo)
            payload["__text__"] = (
                "项目自主运行：%s；排队 %d，运行中 %d，阻塞 %d，等待确认 %d，已完成 %d"
                % (payload.get("mode"), payload["counts"].get("queued", 0),
                   payload["counts"].get("running", 0), payload["counts"].get("blocked", 0),
                   payload["counts"].get("waiting_human", 0), payload["counts"].get("done", 0)))
            return payload, 0
        except Exception as exc:
            return "项目自主运行状态读取失败：%s" % exc, 2
    if name == "project_autonomy_control":
        return project_autonomy_action(args.get("action"), args.get("reason") or "")
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
