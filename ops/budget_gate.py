#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""budget_gate —— 长驻窗口的预算闸门（自动轮换 / 派活熔断 / 用量账）。stdlib only。

为什么需要它（本插件的不变式）
------------------------------
长驻对话窗口的上下文只会单调变肥：

  * **越肥越贵**：每拍把整段上下文重发一次（缓存命中也要钱；压缩 / 切模型之后的
    那一轮按「无缓存」全价重发，实测单次 18~22 万 input 全价）。
  * **越肥越懒**：实测单轮 input 从 ~90k 涨到 ~180k 后，模型退化成「用最短的
    一句话结束轮次」，于是每拍只剩汇报。
  * **越肥越不可控**：它不是坏了，而是要人盯着才发现——于是「换窗口」变成了
    用户的手工活。这条账不该由用户来付。

本模块的要求是：**预算被测量 → 越线自动处理 → 处理过程自己记账**。用户只做方向决策。

它做三件事（全部 fail-open；判定只读，动作显式）
------------------------------------------------
  1) 测量：每个 heartbeat 自动化的目标线程 → rollout 体积 / 累计 token / 最近一轮
     input / 空闲时长。口径复用 `cockpit_pulse.thread_budget`，不另立第二套真相。
  2) 越线处理：`warn` 只记账；`over` 且「窗口空闲 ≥ grace」且「距上次轮换 > cooldown」
     → 后台调 `cockpit_rotate.py --go` 换一代（它自带备份 + 原子替换 + 回读校验），
     本模块自己**不写** automation.toml。
  3) 派活熔断：读代理 `stats.db` 最近一小时的用量，越过硬上限就暂停 `auto_exec` 派活
     （只停「派活」，不停 tick / land / resolve —— 不丢在途工作），降回来自动恢复。

一键关（任一即可）
------------------
  * 文件 `~/.codex-autopilot/BUDGET_GATE_OFF`（与 EXEC_VIA_OPENCODE_OFF 同族）
  * 文件 `~/.codex/budget-gate.OFF`（与 cockpit-rotate.OFF 同族）
  * 环境变量 `KG_BUDGET_GATE=off`

可选配置：`~/.codex/budget-gate.config.json`（只覆盖你想改的键）
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path.home()
CODEX_HOME = HOME / ".codex"
AUTOMATIONS = CODEX_HOME / "automations"
CODEX_DB = CODEX_HOME / "state_5.sqlite"
PROXY_DB = HOME / ".codex-proxy" / "stats.db"

RUNTIME = Path(os.environ.get("KG_DAEMON_HOME",
                              str(HOME / ".codex-autopilot"))).expanduser()
STATE = RUNTIME / "budget-gate.json"
LOG = RUNTIME / "budget-gate.log"
ROTATE_LOG = RUNTIME / "budget-rotate.log"

OFF_FILES = (RUNTIME / "BUDGET_GATE_OFF", CODEX_HOME / "budget-gate.OFF")
OFF_ENV = "KG_BUDGET_GATE"

# 阈值：默认值与 cockpit_pulse / cockpit_rotate 的「轮换线」保持一致（单一口径）。
DEFAULTS = {
    "enabled": True,
    "rotate": True,               # 越线且空闲时自动换代（关掉就只记账 + 通知）
    "grace_minutes": 5.0,         # 窗口最近活动距今 < 这个值就认为「有人在用」，不打扰
    "cooldown_minutes": 60.0,     # 两次自动换代之间的最短间隔
    "rotate_timeout_minutes": 30.0,  # 换代进程超过这么久还没结束就当它挂了
    # 越线线（warn）
    "last_input_warn": 120_000,
    "tokens_used_warn": 150_000_000,
    "rollout_mb_warn": 80.0,
    "compactions_warn": 2,        # 近 compaction_window_minutes 分钟内的压缩次数
    # 严重越线（over → 可以换代）
    "last_input_over": 165_000,
    "tokens_used_over": 250_000_000,
    "rollout_mb_over": 120.0,
    "compactions_over": 3,        # 短时间反复压缩 = 上下文已饱和，必须换代
    "compaction_window_minutes": 120.0,
    # 派活熔断（近 60 分钟用量）
    "pause_input_per_hour": 300_000_000,
    "pause_fresh_per_hour": 60_000_000,
    "resume_input_per_hour": 180_000_000,
    "resume_fresh_per_hour": 30_000_000,
}
CONFIG_PATH = CODEX_HOME / "budget-gate.config.json"


# --------------------------------------------------------------------------- #
# 基础设施：开关 / 日志 / 状态 / 通知（全部 fail-open：出错就当作「没有这个模块」）
# --------------------------------------------------------------------------- #
def off() -> tuple[bool, str]:
    """一键关。任一开关命中就整体跳过。"""
    if str(os.environ.get(OFF_ENV, "")).strip().lower() in {"off", "0", "false", "no"}:
        return True, f"环境变量 {OFF_ENV}=off"
    for p in OFF_FILES:
        try:
            if p.exists():
                return True, f"存在开关文件 {p}"
        except OSError:
            continue
    return False, ""


def config() -> dict:
    """默认值 + 可选配置文件的覆盖（坏文件一律退回默认值，不抛）。"""
    out = dict(DEFAULTS)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for k, v in data.items():
                if k in out and isinstance(v, (int, float, bool)):
                    out[k] = v
    except (OSError, ValueError):
        pass
    return out


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > 2 * 1024 * 1024:
            LOG.replace(LOG.with_suffix(".log.1"))
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass
    print(line, end="")


def _state_load() -> dict:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _state_save(state: dict) -> None:
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, STATE)
    except OSError:
        pass


def _event(kind: str, **fields) -> None:
    """把动作写进自动挡的流水（面板时间线可见）。失败不抛。"""
    try:
        import control_state
        control_state.append_event(kind, **fields)
    except Exception:
        pass


def notify(title: str, body: str, key: str = "") -> None:
    """桌面通知；同一个 key 只报一次，避免每拍刷屏。失败不抛。"""
    st = _state_load()
    seen = st.setdefault("notified", {})
    today = time.strftime("%Y-%m-%d")
    if key and st.get("notified_key") == f"{key}|{today}":
        return
    if key:
        st["notified_key"] = f"{key}|{today}"
    seen.setdefault(today, 0)
    seen[today] += 1
    _state_save(st)
    script = (f'display notification {json.dumps(body)} '
              f'with title {json.dumps(title)}')
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 测量
# --------------------------------------------------------------------------- #
def heartbeat_targets() -> list:
    """所有 status=ACTIVE 且 kind=heartbeat 的自动化及其实时目标线程 id。"""
    out = []
    if not AUTOMATIONS.is_dir():
        return out
    for d in sorted(AUTOMATIONS.iterdir()):
        f = d / "automation.toml"
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if 'kind = "heartbeat"' not in text:
            continue
        if 'status = "ACTIVE"' not in text:
            continue
        tid = ""
        for line in text.splitlines():
            if line.strip().startswith("target_thread_id"):
                tid = line.split("=", 1)[1].strip().strip('"')
                break
        if not tid:
            continue
        gen = 0
        for line in text.splitlines():
            if line.strip().startswith("# generation:"):
                try:
                    gen = int(line.split(":", 1)[1].strip())
                except ValueError:
                    gen = 0
        out.append({"automation": d.name, "thread_id": tid, "generation": gen,
                    "toml": str(f)})
    return out


def _rollout_signals(path: str, cfg: dict) -> dict:
    """从 rollout 尾部读三个更耐看的信号（只读尾部，不整读大文件）。

    为什么不能只看「最近一轮 input」：上下文是锯齿形的——涨到 ~20 万被压缩回 ~4 万，
    所以「最后一轮」可能刚好落在谷底（实测捡到 65,926，而它前十轮都在 18~20 万）。
    真正说明问题的是**峰值**和**压缩频率**。
    """
    out = {"peak_input_recent": None, "compactions_recent": 0,
           "last_compaction_minutes": None}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - 8_000_000))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return out
    window = cfg["compaction_window_minutes"] * 60
    now = time.time()
    peaks, compactions = [], []
    lines = tail.splitlines()
    for line in lines:
        if '"token_usage_record"' in line:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            try:
                v = rec["payload"]["usage"]["input_tokens"]
                ts = rec.get("timestamp") or ""
                peaks.append((ts, int(v)))
            except (KeyError, TypeError, ValueError):
                continue
    for line in lines:
        if '"compacted"' in line:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") == "compacted":
                compactions.append(rec.get("timestamp") or "")
    if peaks:
        out["peak_input_recent"] = max(v for _, v in peaks[-25:])
    # 时间戳是 UTC ISO（…Z）；换算成「距今多少分钟」
    def _ago(ts: str):
        try:
            t = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
            return (now - t) / 60
        except (ValueError, TypeError):
            return None

    recent = []
    for ts in compactions:
        a = _ago(ts)
        if a is not None:
            recent.append(a)
    recent = [a for a in recent if 0 <= a <= window]
    out["compactions_recent"] = len(recent)
    if recent:
        out["last_compaction_minutes"] = round(min(recent), 1)
    return out


def measure_thread(thread_id: str, cfg: dict | None = None) -> dict:
    """单个窗口的预算：口径来自 cockpit_pulse.thread_budget（唯一真相），再补空闲/峰值/压缩。"""
    cfg = cfg or DEFAULTS
    info: dict = {"thread_id": thread_id, "rollout_mb": None, "tokens_used": None,
                  "last_turn_input": None, "idle_minutes": None, "rollout_path": None,
                  "peak_input_recent": None, "compactions_recent": 0,
                  "last_compaction_minutes": None}
    try:
        sys.path.insert(0, str(HERE))
        import cockpit_pulse
        info.update(cockpit_pulse.thread_budget(thread_id) or {})
    except Exception:
        info["measure_error"] = "cockpit_pulse.thread_budget 取不到"
    path = None
    try:
        c = sqlite3.connect(f"file:{CODEX_DB}?mode=ro", uri=True)
        row = c.execute("select rollout_path from threads where id=?", (thread_id,)).fetchone()
        c.close()
        path = row[0] if row else None
    except Exception:
        path = None
    info["rollout_path"] = path
    if path:
        try:
            info["idle_minutes"] = round((time.time() - os.path.getmtime(path)) / 60, 1)
        except OSError:
            pass
        info.update(_rollout_signals(path, cfg))
    return info


def verdict(info: dict, cfg: dict) -> tuple:
    """(level, reasons)：ok / warn / over。over 才允许自动换代。"""
    reasons, over = [], []
    li, tk, mb = info.get("last_turn_input"), info.get("tokens_used"), info.get("rollout_mb")
    peak = info.get("peak_input_recent")
    comp = info.get("compactions_recent") or 0
    if isinstance(li, (int, float)) and li > cfg["last_input_warn"]:
        reasons.append(f"最近一轮 input {int(li):,} > {int(cfg['last_input_warn']):,}")
    if isinstance(peak, (int, float)) and peak > cfg["last_input_warn"]:
        reasons.append(f"近 25 轮峰值 input {int(peak):,} > {int(cfg['last_input_warn']):,}")
    if isinstance(tk, (int, float)) and tk > cfg["tokens_used_warn"]:
        reasons.append(f"累计 {tk/1e6:.1f}M token > {cfg['tokens_used_warn']/1e6:.0f}M")
    if isinstance(mb, (int, float)) and mb > cfg["rollout_mb_warn"]:
        reasons.append(f"rollout {mb}MB > {cfg['rollout_mb_warn']}MB")
    if comp >= cfg["compactions_warn"]:
        reasons.append(f"近 {cfg['compaction_window_minutes']/60:.0f} 小时压缩 {comp} 次")
    if isinstance(peak, (int, float)) and peak > cfg["last_input_over"]:
        over.append(f"近 25 轮峰值 input {int(peak):,} > {int(cfg['last_input_over']):,}")
    if isinstance(li, (int, float)) and li > cfg["last_input_over"]:
        over.append(f"最近一轮 input {int(li):,} > {int(cfg['last_input_over']):,}")
    if isinstance(tk, (int, float)) and tk > cfg["tokens_used_over"]:
        over.append(f"累计 {tk/1e6:.1f}M token > {cfg['tokens_used_over']/1e6:.0f}M")
    if isinstance(mb, (int, float)) and mb > cfg["rollout_mb_over"]:
        over.append(f"rollout {mb}MB > {cfg['rollout_mb_over']}MB")
    if comp >= cfg["compactions_over"]:
        over.append(f"近 {cfg['compaction_window_minutes']/60:.0f} 小时压缩 {comp} 次"
                    f"（上下文已饱和，反复重发）")
    if over:
        return "over", over
    if reasons:
        return "warn", reasons
    return "ok", []


def proxy_usage(minutes: int = 60) -> dict:
    """代理近 N 分钟用量（只读 stats.db）。取不到返回 {}，绝不猜。"""
    out: dict = {}
    if not PROXY_DB.exists():
        return out
    try:
        c = sqlite3.connect(f"file:{PROXY_DB}?mode=ro", uri=True)
        since = int(time.time()) - minutes * 60
        row = c.execute(
            "select count(*), coalesce(sum(input_tokens),0), "
            "coalesce(sum(cache_hit_tokens),0), coalesce(sum(output_tokens),0) "
            "from requests where ts >= ?", (since,)).fetchone()
        out["requests"] = row[0]
        out["input"] = row[1]
        out["cache"] = row[2]
        out["fresh"] = row[1] - row[2]
        out["output"] = row[3]
        rows = c.execute(
            "select model, count(*), coalesce(sum(input_tokens),0), "
            "coalesce(sum(input_tokens)-sum(cache_hit_tokens),0) "
            "from requests where ts >= ? group by model order by 3 desc limit 8",
            (since,)).fetchall()
        out["by_model"] = [{"model": r[0], "requests": r[1], "input": r[2],
                            "fresh": r[3]} for r in rows]
        c.close()
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}"
    out["window_minutes"] = minutes
    return out


# --------------------------------------------------------------------------- #
# 动作
# --------------------------------------------------------------------------- #
def dispatch_paused() -> tuple:
    """派活是否被预算熔断暂停。给 central_daemon.auto_exec_config() 用，必须极轻、不抛。"""
    try:
        st = _state_load()
        if st.get("dispatch_paused"):
            return True, str(st.get("dispatch_paused_reason") or "预算熔断")
    except Exception:
        pass
    return False, ""


def _rotate_in_flight(cfg: dict) -> bool:
    st = _state_load()
    rot = st.get("rotate") or {}
    pid, ts = rot.get("pid"), rot.get("ts") or 0
    if not pid:
        return False
    if time.time() - float(ts) > cfg["rotate_timeout_minutes"] * 60:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def start_rotate(automation: str, thread_id: str, cfg: dict) -> dict:
    """后台换代：真正改 automation.toml 的是 cockpit_rotate.py（自带备份/原子/回读）。"""
    script = HERE / "cockpit_rotate.py"
    if not script.exists():
        log(f"× 换代跳过：找不到 {script}")
        return {"started": False, "why": "找不到 cockpit_rotate.py"}
    cmd = [sys.executable, str(script), "--automation", automation, "--go", "--force"]
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        with ROTATE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                     f"auto-rotate {automation} (target {thread_id[:12]}…)\n")
            p = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                 cwd=str(HERE), start_new_session=True)
    except Exception as exc:
        log(f"× 换代启动失败（fail-open，下轮再试）：{exc!r}")
        return {"started": False, "why": f"{type(exc).__name__}"}
    st = _state_load()
    st["rotate"] = {"ts": time.time(), "pid": p.pid, "automation": automation,
                    "target_before": thread_id, "result": "running"}
    _state_save(st)
    log(f"→ 已后台启动换代：{automation}（pid {p.pid}，日志 {ROTATE_LOG}）")
    _event("budget_rotate_start", automation=automation, target=thread_id, pid=p.pid)
    notify("自动挡 · 预算闸门",
           f"{automation} 窗口已越线，正在后台换成新一代（不打断你正在用的窗口）",
           key=f"rotate-{automation}")
    return {"started": True, "pid": p.pid}


def _rotate_result(automation: str, cfg: dict) -> None:
    """换代进程已结束：核对 automation.toml 的目标是否真的换了。"""
    st = _state_load()
    rot = st.get("rotate") or {}
    if rot.get("result") != "running":
        return
    before = str(rot.get("target_before") or "")
    after = ""
    for t in heartbeat_targets():
        if t["automation"] == automation:
            after = t["thread_id"]
    if after and before and after != before:
        rot["result"] = "ok"
        log(f"✓ 换代生效：{automation} {before[:12]}… → {after[:12]}…")
        _event("budget_rotate_done", automation=automation, before=before, after=after)
    else:
        rot["result"] = "no-effect"
        log(f"⚠️ 换代没有改变目标线程（{automation} 仍是 {before[:12]}…）；"
            f"请看 {ROTATE_LOG}，本轮不再重试，等 cooldown 之后再说")
        _event("budget_rotate_no_effect", automation=automation, target=before)
    st["rotate"] = rot
    _state_save(st)


def _maybe_pause_dispatch(usage: dict, cfg: dict) -> None:
    """派活熔断：超硬上限暂停 auto_exec 派活，降回来自动恢复。只动「派活」。"""
    if not usage or usage.get("error"):
        return
    st = _state_load()
    if usage["input"] >= cfg["pause_input_per_hour"] or \
       usage["fresh"] >= cfg["pause_fresh_per_hour"]:
        if not st.get("dispatch_paused"):
            why = (f"近 {usage['window_minutes']} 分钟 input {usage['input']/1e6:.0f}M"
                   f"（其中新增 {usage['fresh']/1e6:.0f}M）、{usage['requests']} 次请求，"
                   f"越过硬上限")
            st["dispatch_paused"] = True
            st["dispatch_paused_reason"] = why
            st["dispatch_paused_ts"] = time.time()
            _state_save(st)
            log(f"⏸ 暂停派活（auto_exec）：{why}；land / resolve 不受影响")
            _event("budget_pause_dispatch", reason=why)
            notify("自动挡 · 用量熔断",
                   f"近一小时用量越线，已暂停自动派活（落盘不受影响）。{why}",
                   key="pause-dispatch")
    else:
        if st.get("dispatch_paused") and \
           usage["input"] <= cfg["resume_input_per_hour"] and \
           usage["fresh"] <= cfg["resume_fresh_per_hour"]:
            st["dispatch_paused"] = False
            st["dispatch_paused_reason"] = ""
            _state_save(st)
            log("▶️ 用量已回落，恢复派活（auto_exec）")
            _event("budget_resume_dispatch")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(dry: bool = False, do_rotate: bool | None = None) -> dict:
    """一次闸门：测量 → 记账 → （越线且空闲时）换代 → 派活熔断。返回结构化结果。"""
    cfg = config()
    is_off, why_off = off()
    report: dict = {"ts": time.time(), "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "off": is_off, "off_reason": why_off, "dry": dry,
                    "threads": [], "usage": {}, "actions": []}
    if is_off:
        log(f"预算闸门已关（{why_off}）：只记一行，不做任何动作")
        report["actions"].append({"action": "skip", "why": why_off})
        return report
    if not cfg.get("enabled", True):
        report["actions"].append({"action": "disabled", "why": "config.enabled=false"})
        return report

    rotate_allowed = cfg["rotate"] if do_rotate is None else bool(do_rotate)
    rotating = _rotate_in_flight(cfg)

    for t in heartbeat_targets():
        info = measure_thread(t["thread_id"], cfg)
        level, reasons = verdict(info, cfg)
        idle = info.get("idle_minutes")
        row = {"automation": t["automation"], "generation": t["generation"],
               "thread_id": t["thread_id"], "level": level, "reasons": reasons,
               "last_turn_input": info.get("last_turn_input"),
               "peak_input_recent": info.get("peak_input_recent"),
               "compactions_recent": info.get("compactions_recent"),
               "last_compaction_minutes": info.get("last_compaction_minutes"),
               "tokens_used": info.get("tokens_used"),
               "rollout_mb": info.get("rollout_mb"),
               "idle_minutes": idle}
        report["threads"].append(row)
        if level == "ok":
            continue
        line = (f"[{level}] {t['automation']} 第{t['generation']}代 "
                f"{t['thread_id'][:12]}… | " + "、".join(reasons) +
                f" | 峰值 input {info.get('peak_input_recent')}"
                f" 累计 {(info.get('tokens_used') or 0)/1e6:.1f}M"
                f" rollout {info.get('rollout_mb')}MB 空闲 {idle} 分钟")
        log(line)
        row["logged"] = True
        if not dry:
            st = _state_load()
            st.setdefault("warn_streak", {})
            st["warn_streak"][t["automation"]] = int(st["warn_streak"].get(t["automation"], 0)) + 1
            _state_save(st)
        if level == "warn":
            if not dry:
                notify("自动挡 · 窗口预算",
                       f"{t['automation']} 第{t['generation']}代已越线：{reasons[0]}",
                       key=f"warn-{t['automation']}")
            continue
        # ---- over：条件都满足才换代 ----
        if not rotate_allowed:
            report["actions"].append({"action": "recommend-rotate",
                                      "automation": t["automation"], "why": "配置里关掉了自动换代"})
            continue
        if rotating:
            report["actions"].append({"action": "rotate-skip",
                                      "automation": t["automation"], "why": "已有换代在跑"})
            continue
        last_rotate = float((_state_load().get("rotate") or {}).get("ts") or 0)
        if time.time() - last_rotate < cfg["cooldown_minutes"] * 60:
            report["actions"].append({"action": "rotate-skip", "automation": t["automation"],
                                      "why": f"距上次换代不足 {cfg['cooldown_minutes']} 分钟"})
            continue
        if idle is None or idle < cfg["grace_minutes"]:
            report["actions"].append({"action": "rotate-wait", "automation": t["automation"],
                                      "why": f"窗口 {idle} 分钟前还在动（<{cfg['grace_minutes']} 分钟），"
                                             f"等它空闲再换，不打扰你"})
            continue
        if dry:
            report["actions"].append({"action": "rotate-would", "automation": t["automation"],
                                      "why": "预演：不真的换"})
            continue
        res = start_rotate(t["automation"], t["thread_id"], cfg)
        report["actions"].append({"action": "rotate", "automation": t["automation"], **res})
        # 一次只换一个：换代本身要起真实 codex 会话，同时换两个既互相抢重试预算，
        # 也会让状态里只留下最后一个的记录（上一个的结果没人核对）。下一个留给下一拍。
        rotating = True

    # 派活熔断（近一小时看得见的那部分用量）
    usage = proxy_usage(60)
    report["usage"] = usage
    if dry:
        report["actions"].append({"action": "usage-dry", "usage_input": usage.get("input")})
    else:
        _maybe_pause_dispatch(usage, cfg)

    # 换代进程收尾核对（要写状态，所以只在真跑时做；预演保持只读）
    if not dry:
        st = _state_load()
        if (st.get("rotate") or {}).get("result") == "running" and not rotating:
            _rotate_result(str((st.get("rotate") or {}).get("automation") or ""), cfg)
        st = _state_load()
        st["last_run"] = report["iso"]
        st["last_report"] = {"threads": report["threads"], "usage": usage}
        _state_save(st)
    return report


def daemon_hook(dry: bool = False, log_fn=None) -> dict:
    """自动挡每周期调这里。任何异常都由调用方吞掉（fail-open）。"""
    rep = run(dry=dry)
    if log_fn:
        lines = []
        for t in rep["threads"]:
            lines.append(f"{t['automation']}·{t['generation']}代 level={t['level']} "
                         f"峰值input={t['peak_input_recent']} "
                         f"累计={((t['tokens_used'] or 0)/1e6):.1f}M "
                         f"rollout={t['rollout_mb']}MB 压缩={t['compactions_recent']}次 "
                         f"空闲={t['idle_minutes']}min")
        u = rep.get("usage") or {}
        if u.get("input") is not None:
            lines.append(f"近{u['window_minutes']}分钟 input={u['input']/1e6:.1f}M "
                         f"新增={u['fresh']/1e6:.1f}M req={u['requests']}"
                         + (f" model_top={u['by_model'][0]['model']}" if u.get("by_model") else ""))
        acts = [a.get("action") for a in rep.get("actions", [])]
        log_fn("[budget] " + (" | ".join(lines) if lines else "无 heartbeat 目标") +
               (f" | 动作={acts}" if acts else ""))
    return rep


def human_report(rep: dict) -> str:
    L = [f"# 预算闸门 · {rep['iso']}", ""]
    if rep.get("off"):
        L.append(f"⚠️ 已关闭：{rep['off_reason']}")
    if not rep["threads"]:
        L.append("（没有 ACTIVE 的 heartbeat 自动化）")
    for t in rep["threads"]:
        tag = {"ok": "✅", "warn": "⚠️", "over": "🔴"}[t["level"]]
        L.append(f"{tag} **{t['automation']} 第{t['generation']}代** `{t['thread_id'][:12]}…`")
        L.append(f"   峰值 input {t['peak_input_recent']}（最近一轮 {t['last_turn_input']}）　"
                 f"累计 {(t['tokens_used'] or 0)/1e6:.1f}M token　rollout {t['rollout_mb']}MB")
        L.append(f"   近 2 小时压缩 {t['compactions_recent']} 次"
                 + (f"（最近一次 {t['last_compaction_minutes']} 分钟前）"
                    if t.get("last_compaction_minutes") else "")
                 + f"　空闲 {t['idle_minutes']} 分钟")
        if t["reasons"]:
            L.append(f"   越线：{'、'.join(t['reasons'])}")
    u = rep.get("usage") or {}
    if u.get("input") is not None:
        L += ["", f"## 近 {u['window_minutes']} 分钟代理用量",
              f"- 总 input {u['input']/1e6:.1f}M（其中缓存 {u['cache']/1e6:.1f}M、"
              f"新增 {u['fresh']/1e6:.2f}M），output {u['output']/1e6:.2f}M，"
              f"{u['requests']} 次请求"]
        for m in u.get("by_model", [])[:5]:
            L.append(f"  - {m['model']}: {m['requests']} 次 / input {m['input']/1e6:.1f}M"
                     f"（新增 {m['fresh']/1e6:.2f}M）")
    st = _state_load()
    if st.get("dispatch_paused"):
        L += ["", f"⏸ 派活已暂停：{st.get('dispatch_paused_reason')}"]
    if rep.get("actions"):
        L += ["", "## 本轮动作"]
        for a in rep["actions"]:
            L.append(f"- {a.get('action')} {a.get('automation', '')} {a.get('why', '')}".rstrip())
    L += ["", "一键关：`touch ~/.codex-autopilot/BUDGET_GATE_OFF`"]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="长驻窗口预算闸门（测量 / 自动换代 / 派活熔断）")
    ap.add_argument("--go", action="store_true", help="允许动作（默认只预演，不动任何东西）")
    ap.add_argument("--report", action="store_true", help="打印中文一页（含近一小时用量）")
    ap.add_argument("--json", action="store_true", help="打印结构化结果")
    ap.add_argument("--no-rotate", action="store_true", help="本次不换代，只测量/熔断")
    ap.add_argument("--selftest", action="store_true", help="纯内存自检（不碰任何文件）")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    if args.report:
        rep = run(dry=True)
        print(human_report(rep))
        return 0

    rep = run(dry=not args.go, do_rotate=False if args.no_rotate else None)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(human_report(rep))
    return 0


def _selftest() -> int:
    """阈值/空闲/冷却判定的纯函数自检：不读文件、不写文件、不发通知。"""
    cfg = dict(DEFAULTS)
    ok = True

    def _chk(cond, msg):
        nonlocal ok
        print(("  ✓ " if cond else "  ✗ ") + msg)
        ok = ok and bool(cond)

    print("budget_gate 自检")
    _chk(verdict({"last_turn_input": 50_000, "tokens_used": 1_000_000,
                  "rollout_mb": 1.0}, cfg)[0] == "ok", "正常窗口判 ok")
    _chk(verdict({"last_turn_input": 130_000, "tokens_used": 1_000_000,
                  "rollout_mb": 1.0}, cfg)[0] == "warn", "越过 120k 判 warn")
    _chk(verdict({"last_turn_input": 190_000, "tokens_used": 1_000_000,
                  "rollout_mb": 1.0}, cfg)[0] == "over", "越过 165k 判 over")
    _chk(verdict({"tokens_used": 300_000_000}, cfg)[0] == "over", "累计 token 越线判 over")
    _chk(verdict({}, cfg)[0] == "ok", "取不到数据不误判（ok，不动手）")
    _chk(verdict({"last_turn_input": None, "rollout_mb": 200.0}, cfg)[0] == "over",
         "None 不参与比较，只看真实越线的指标")
    _chk(verdict({"peak_input_recent": 190_000, "last_turn_input": 46_000}, cfg)[0] == "over",
         "「最后一轮刚从谷底回升」也能被峰值抓住（这正是只看 last 会漏的 case）")
    _chk(verdict({"compactions_recent": 3}, cfg)[0] == "over", "近 2 小时压缩 3 次判 over")
    _chk(verdict({"compactions_recent": 2}, cfg)[0] == "warn", "压缩 2 次判 warn")
    _chk("compactions_over" in cfg and "compaction_window_minutes" in cfg,
         "配置里带压缩频率阈值（可覆盖）")
    print("（离线自检：把数据源换成坏值时 run() 走 fail-open，见 --report）")
    print("自检", "通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
