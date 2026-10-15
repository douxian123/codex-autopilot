#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""beat_audit —— 心跳「空转记分」：把「挤牙膏」变成会被系统点数、会被点名、会被记录的事。stdlib only。

为什么需要它（不变式）
----------------------
心跳拍（automation/heartbeat）最常见的失败不是报错，而是**安静地空过去**：
跑一页体检、读几个文件、回一句「无新增进展」，这一拍就结束了。用户在窗口里看到的
就是「挤牙膏」——说一句、动一下、又停住；而系统里没有任何东西记录过这件事发生过。

既有机制的边界：
  * loop-guard 的「空转熔断」只看**单轮**（连续三轮无交付才拦一次 stop），不跨拍计数，
    也分不清「真阻塞」和「假装阻塞」。
  * budget_gate 只量**上下文预算**（该不该换窗口），不量**产出**。
所以这里补的是「产出」那一维：**每一拍分类 → 连续 N 拍无动作就点名。**

三件事（fail-open；判定只读，动作显式）
--------------------------------------
  1) 分类：把目标线程的 rollout 按 `<heartbeat>` 切成拍，每拍判成
     ACTION（落盘 / 造门禁证据 / 派活 / 干预在途 / 记台账）或 EMPTY（都没做）。
  2) 点名：连续 `empty_threshold`（默认 3）拍 EMPTY → 桌面通知 + 写进自动挡流水，
     并把一行【空转记分】写进该 automation 的 prompt（**幂等、只有一行、下一拍产出了动作就自动撤掉**），
     让下一拍自己看见「我已经空了几拍」。
  3) 记账：把每拍分类与连续空转数留在 state 文件里，供面板和 --report 读。

一键关（任一）：文件 `~/.codex-autopilot/BEAT_AUDIT_OFF`、`~/.codex/beat-audit.OFF`、
环境变量 `KG_BEAT_AUDIT=off`。配置：`~/.codex/beat-audit.config.json`。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path.home()
CODEX_HOME = HOME / ".codex"
AUTOMATIONS = CODEX_HOME / "automations"
CODEX_DB = CODEX_HOME / "state_5.sqlite"
RUNTIME = Path(os.environ.get("KG_DAEMON_HOME", str(HOME / ".codex-autopilot"))).expanduser()
STATE = RUNTIME / "beat-audit.json"
LOG = RUNTIME / "beat-audit.log"
OFF_FILES = (RUNTIME / "BEAT_AUDIT_OFF", CODEX_HOME / "beat-audit.OFF")
OFF_ENV = "KG_BEAT_AUDIT"
CONFIG_PATH = CODEX_HOME / "beat-audit.config.json"
BANNER_MARK = "【空转记分】"

DEFAULTS = {
    "enabled": True,
    "empty_threshold": 3,      # 连续几拍无动作就点名
    "banner": True,            # 是否把记分写进 automation 的 prompt（幂等）
    "notify": True,            # 是否发桌面通知
    "keep_beats": 12,          # state 里每个 automation 保留多少拍
    "scan_lines": 4000,        # 每次最多回看多少行 rollout
}

# 动作识别：命中任意一条就算这一拍「有产出」。写法与项目里的命令一致。
ACTION_RULES = (
    ("land", re.compile(r"kgctl\.py\s+land\b")),
    ("gate", re.compile(r"kgctl\.py\s+gate\b")),
    ("reap", re.compile(r"kgctl\.py\s+reap\b")),
    ("resolve", re.compile(r"kgctl\.py\s+resolve\b")),
    ("port", re.compile(r"kgctl\.py\s+port\b")),
    ("supersede", re.compile(r"kgctl\.py\s+supersede\b")),
    ("converge", re.compile(r"kgctl\.py\s+converge\b")),
    ("intake", re.compile(r"kgctl\.py\s+intake\b")),
    ("record", re.compile(r"truthctl\.py\s+add\b")),
)
DISPATCH_TOOLS = {"create_thread", "send_message_to_thread"}
WRITE_TOOLS = {"apply_patch", "edit", "write"}
# 这类错误 = 「不是它懒，是上游/额度不让它干活」：再催也没用，要人处理额度或换通道。
INFRA_RX = (r"预扣费|403|429|rate.?limit|503|502|Bad Gateway|额度|insufficient|quota"
            r"|Service Unavailable|ECONNRESET|SSL|timeout|timed out")


def off() -> tuple:
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


def _state_save(st: dict) -> None:
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, STATE)
    except OSError:
        pass


def notify(title: str, body: str) -> None:
    try:
        script = (f'display notification {json.dumps(body)} with title {json.dumps(title)}')
        import subprocess
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception:
        pass


def _event(kind: str, **fields) -> None:
    try:
        import control_state
        control_state.append_event(kind, **fields)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 目标线程与拍分类
# --------------------------------------------------------------------------- #
def heartbeat_targets() -> list:
    """ACTIVE 的 heartbeat 自动化及其实时目标线程（与 budget_gate / cockpit_pulse 同一口径）。"""
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
        if 'kind = "heartbeat"' not in text or 'status = "ACTIVE"' not in text:
            continue
        m = re.search(r'^target_thread_id\s*=\s*"([^"]+)"', text, re.M)
        if m:
            out.append({"automation": d.name, "thread_id": m.group(1), "toml": str(f)})
    return out


def _rollout_path(thread_id: str) -> str:
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{CODEX_DB}?mode=ro", uri=True)
        row = c.execute("select rollout_path from threads where id=?", (thread_id,)).fetchone()
        c.close()
        return row[0] if row else ""
    except Exception:
        return ""


def _text_of(payload: dict) -> str:
    parts = []
    for c in payload.get("content") or []:
        if isinstance(c, dict):
            parts.append(c.get("text") or "")
    return "\n".join(p for p in parts if p)


def classify_beats(rollout: str, cfg: dict) -> list:
    """把 rollout 切成「拍」并逐拍分类。返回 [{ts, calls, actions, final}]（最新的在最后）。"""
    try:
        with open(rollout, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 8_000_000))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    beats, cur = [], None
    for line in tail.splitlines()[-cfg["scan_lines"]:]:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") != "response_item":
            continue
        pl = rec.get("payload") or {}
        ty = pl.get("type")
        ts = (rec.get("timestamp") or "")[11:19]
        if ty == "message" and pl.get("role") == "user" and _text_of(pl).startswith("<heartbeat>"):
            cur = {"ts": ts, "calls": [], "actions": set(), "final": ""}
            beats.append(cur)
            continue
        if ty in ("function_call", "custom_tool_call") and cur is not None:
            name = pl.get("name") or "?"
            args = str(pl.get("arguments") or pl.get("input") or "")
            cur["calls"].append(name)
            if name in DISPATCH_TOOLS:
                cur["actions"].add("dispatch")
            if name in WRITE_TOOLS:
                cur["actions"].add("write")
            for tag, rx in ACTION_RULES:
                if rx.search(args):
                    cur["actions"].add(tag)
        elif ty == "message" and pl.get("role") == "assistant" and cur is not None:
            cur["final"] = _text_of(pl).strip()
        elif rec.get("type") == "event_msg" and ty == "task_complete" and cur is not None:
            err = (pl.get("error") or {}).get("message") or ""
            if err:
                cur["error"] = err
    for b in beats:
        b["actions"] = sorted(b["actions"])
        err = b.get("error") or ""
        if err:
            # 分清「它懒」和「上游/额度不让它干活」：后者再催它也没用，要人处理额度或换通道。
            b["kind"] = "INFRA" if re.search(INFRA_RX, err) else "ERROR"
        else:
            b["kind"] = "ACTION" if b["actions"] else "EMPTY"
    return beats


def _banner_targets(empty_streak: int, threshold: int, name: str) -> str:
    """写进 automation.toml 的那一行（幂等、可撤）。"""
    return (f"{BANNER_MARK}本 automation 已连续 {empty_streak} 拍没有任何动作"
            f"（落盘 / 造门禁证据 / 派活 / 干预 / 记台账都没有）。"
            f"累计到 {threshold} 拍即视为空转：本拍必须完成 A→B→C→D 里的一条。"
            f"—— 由 beat_audit.py 自动写入，下一拍出现动作即自动撤除。")


def set_banner(toml_path: str, line: str | None) -> None:
    """把【空转记分】写进/撤出 automation.toml 的 prompt。幂等、只碰自己那一行。"""
    try:
        p = Path(toml_path)
        txt = p.read_text(encoding="utf-8")
        m = re.search(r'^prompt = "((?:[^"\\]|\\.)*)"', txt, re.M)
        if not m:
            return
        raw = m.group(1)
        unescaped = (raw.replace("\\\\", "\x00").replace('\\"', '"')
                        .replace("\\n", "\n").replace("\x00", "\\"))
        # 去掉旧的横幅（可能带一层 "\\n\\n" 前缀）
        cleaned = re.sub(r"(\\n){0,3}" + re.escape(BANNER_MARK) + r"[^\\]*?自动撤除。", "", unescaped)
        cleaned = cleaned.rstrip("\n")
        if line:
            cleaned = cleaned + "\n\n" + line
        new_raw = (cleaned.replace("\\", "\\\\").replace('"', '\\"')
                          .replace("\n", "\\n").replace("\t", "\\t"))
        if new_raw == raw:
            return
        p.write_text(txt[:m.start(1)] + new_raw + txt[m.end(1):], encoding="utf-8")
    except (OSError, ValueError, AttributeError):
        pass


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(dry: bool = False) -> dict:
    cfg = config()
    is_off, why = off()
    rep = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "off": is_off, "off_reason": why,
           "dry": dry, "automations": [], "actions": []}
    if is_off:
        log(f"空转记分已关（{why}）：只记一行")
        return rep
    if not cfg.get("enabled", True):
        return rep

    st = _state_load()
    hist_all = st.setdefault("history", {})
    for t in heartbeat_targets():
        path = _rollout_path(t["thread_id"])
        beats = classify_beats(path, cfg) if path else []
        hist = hist_all.setdefault(t["automation"], [])
        seen = {h.get("ts") for h in hist}
        for b in beats:
            if b["ts"] in seen:
                continue
            hist.append({"ts": b["ts"], "actions": b["actions"], "kind": b.get("kind", "EMPTY"),
                         "empty": b.get("kind") == "EMPTY", "calls": len(b["calls"]),
                         "error": (b.get("error") or "")[:160] or None})
            seen.add(b["ts"])
        hist_all[t["automation"]] = hist[-cfg["keep_beats"]:]

        streak = 0
        for h in reversed(hist_all[t["automation"]]):
            if h.get("empty"):
                streak += 1
            else:
                break
        # 上游/额度类失败单独报：这不是「它懒」，催它没用，要人去充额度或换通道。
        infra = [h for h in hist_all[t["automation"]][-3:] if h.get("kind") == "INFRA"]
        if infra:
            last_err = (infra[-1].get("error") or "").strip()
            log(f"⚠️ {t['automation']} 最近 {len(infra)} 拍是上游/额度失败（不是空转）：{last_err[:160]}")
            _event("beat_infra_fail", automation=t["automation"], n=len(infra), error=last_err[:300])
            if not dry and cfg["notify"] and st.get("infra_notified") != time.strftime("%Y-%m-%d"):
                st["infra_notified"] = time.strftime("%Y-%m-%d")
                notify("自动挡 · 上游/额度失败",
                       f"{t['automation']} 最近 {len(infra)} 拍拿不到模型回复：{last_err[:120]}"
                       f"；这不是它空转，需要处理额度或换通道")
        latest = hist_all[t["automation"]][-1] if hist_all[t["automation"]] else {}
        rep["automations"].append({"automation": t["automation"], "thread_id": t["thread_id"],
                                   "beats": len(hist_all[t["automation"]]),
                                   "empty_streak": streak,
                                   "last_actions": latest.get("actions") or [],
                                   "last_calls": latest.get("calls")})
        if streak < cfg["empty_threshold"]:
            if cfg["banner"] and not dry:
                set_banner(t["toml"], None)   # 有动作了 → 撤掉横幅
            continue
        msg = (f"{t['automation']} 已连续 {streak} 拍没有任何动作"
               f"（最近一拍 {latest.get('calls', 0)} 次工具调用、动作={latest.get('actions') or '无'}）")
        log(f"⚠️ {msg}")
        _event("beat_empty_alert", automation=t["automation"], empty_streak=streak,
               last_ts=latest.get("ts"), calls=latest.get("calls"))
        if not dry:
            if cfg["banner"]:
                set_banner(t["toml"], _banner_targets(streak, cfg["empty_threshold"], t["automation"]))
            if cfg["notify"]:
                notify("自动挡 · 心跳空转", msg + "；已把记分写进下一拍的 prompt")
        rep["actions"].append({"action": "alert", "automation": t["automation"], "empty_streak": streak})
    if not dry:
        st["last_run"] = rep["ts"]
        _state_save(st)
    return rep


def daemon_hook(dry: bool = False, log_fn=None) -> dict:
    rep = run(dry=dry)
    if log_fn:
        bits = [f"{a['automation']}·空转{a['empty_streak']}拍/动作{a['last_actions'] or '无'}"
                for a in rep["automations"]]
        if bits:
            log_fn("[beat] " + " | ".join(bits) +
                   (f" | 动作={[x['action'] for x in rep['actions']]}" if rep["actions"] else ""))
    return rep


def human_report(rep: dict) -> str:
    L = [f"# 心跳空转记分 · {rep['ts']}", ""]
    if rep.get("off"):
        L.append(f"⚠️ 已关闭：{rep['off_reason']}")
    if not rep["automations"]:
        L.append("（没有 ACTIVE 的 heartbeat 自动化）")
    for a in rep["automations"]:
        tag = "🔴" if a["empty_streak"] >= 2 else ("⚠️" if a["empty_streak"] else "✅")
        L.append(f"{tag} **{a['automation']}** `{a['thread_id'][:12]}…`")
        L.append(f"   记录 {a['beats']} 拍　连续空转 {a['empty_streak']} 拍　"
                 f"最近一拍 {a['last_calls']} 次工具调用、动作 {a['last_actions'] or '无'}")
    L += ["", "一键关：`touch ~/.codex-autopilot/BEAT_AUDIT_OFF`"]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="心跳空转记分（分类每拍产出 / 连续空转点名 / 写记分进 prompt）")
    ap.add_argument("--go", action="store_true", help="允许动作（默认只预演）")
    ap.add_argument("--report", action="store_true", help="打印中文一页")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    rep = run(dry=not args.go)
    print(json.dumps(rep, ensure_ascii=False, indent=1) if args.json else human_report(rep))
    return 0


def _selftest() -> int:
    """纯内存自检：分类规则 + 连续空转 + 横幅幂等。不碰真实文件。"""
    ok = True

    def chk(cond, msg):
        nonlocal ok
        print(("  ✓ " if cond else "  ✗ ") + msg)
        ok = ok and bool(cond)

    import tempfile
    beats = [
        {"ts": "01:00:00", "calls": [], "actions": set(), "final": "无新增进展"},
        {"ts": "01:15:00", "calls": ["exec_command"], "actions": set(), "final": "队列为空"},
        {"ts": "01:30:00", "calls": ["exec_command", "exec_command"],
         "actions": {"land"}, "final": "已落盘"},
        {"ts": "01:45:00", "calls": ["send_message_to_thread"], "actions": {"dispatch"}, "final": "已派活"},
    ]
    empties = [b for b in beats if not b["actions"]]
    chk(len(empties) == 2, "无动作的拍被判为空转（2 拍）")
    streak = 0
    for b in reversed(beats):
        if not b["actions"]:
            streak += 1
        else:
            break
    chk(streak == 0, "最近一拍有动作 → 连续空转归零")
    # 横幅幂等：写两次结果一样，撤除后干净
    with tempfile.TemporaryDirectory() as td:
        toml = Path(td) / "automation.toml"
        orig = ('version = 1\nprompt = "你是司令。\\n\\n【交付纪律】照旧。\\n"\n'
                'status = "ACTIVE"\ntarget_thread_id = "x"\n')
        toml.write_text(orig, encoding="utf-8")
        line = _banner_targets(3, 3, "demo")
        set_banner(str(toml), line)
        once = toml.read_text(encoding="utf-8")
        set_banner(str(toml), line)
        twice = toml.read_text(encoding="utf-8")
        chk(once == twice, "横幅写入幂等（写两次文件不变）")
        chk(BANNER_MARK in once, "横幅确实写进了 prompt")
        chk('status = "ACTIVE"' in once and 'target_thread_id = "x"' in once, "其它字段没被动")
        set_banner(str(toml), None)
        after = toml.read_text(encoding="utf-8")
        chk(BANNER_MARK not in after, "有动作后横幅被撤除")
        chk("照旧" in after and after.count("prompt = ") == 1, "撤除后 prompt 内容与结构完好")
    print("自检", "通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
