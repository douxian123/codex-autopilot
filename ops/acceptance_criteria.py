#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""acceptance_criteria —— 验收管家的「标准候选」（stdlib only）。

用户提出的完整链路是：先确认预期，再把预期编译成可判定验收标准，自己跑完
机器能跑的部分，最后才把确实需要人看的部分截图提醒用户。此前仓库只有
`acceptance.jsonl` 的登记与「先自验再请人看」闸门，缺的是前半截：模糊预期
不会自动变成可判定标准。

为什么只出候选
---------------
「这句话到底算不算验收标准、要不要人看」是语义判断。直接写进验收台账会把
一句随口的话变成项目门禁，也会制造假证据。本模块沿用 `constraint_candidates`
的候选制：只扫已路由的入口原话，命中明确验收信号才提名；人显式确认后才调用
`truthctl accept` 登记验收项。候选阶段不写 `acceptance.jsonl`，promote 时也不
写 evidence / screenshots，机器自证项仍要真门禁补证据。

它同时提供路由层的确定性编译：机器门禁必须落到
`name + cmd + cwd + pass_condition=exit 0`，人工复核必须写清打开哪里、看什么、什么算通过；
推不出任何可判定条件时返回 `unknown`，绝不冒充完成。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

OPS = Path(__file__).resolve().parent

# 命中的是「怎么算完成」的明确信号；不使用单独的「必须」，避免与约束候选滥收重叠。
SIGNALS = [
    ("验收标准", "machine"), ("验收项", "machine"),
    ("门禁", "machine"), ("测试通过", "machine"), ("跑通", "machine"),
    ("自证", "machine"), ("算过", "machine"),
    ("必须能", "human"), ("要能", "human"), ("能看到", "human"),
    ("看到", "human"), ("点一下", "human"), ("可用", "human"),
    ("不卡住", "human"), ("出来后", "human"), ("效果", "human"),
    ("确认", "human"), ("验收", "human"),
]

# 描述现状的句子不是验收标准。
SUPPRESS = ("曾达", "实测", "复现", "报错原文", "日志如下")

MACHINE_SIGNALS = {word for word, kind in SIGNALS if kind == "machine"}


def truth_dir() -> Path:
    repo = os.environ.get("KG_REPO") or ""
    if repo:
        return Path(repo).expanduser() / "ops" / "truth"
    return OPS / "truth"


def inbox_path() -> Path:
    repo = os.environ.get("KG_REPO") or ""
    base = Path(repo).expanduser() if repo else OPS.parent
    return base / "ops" / "tasks" / "inbox.jsonl"


def candidates_path() -> Path:
    return truth_dir() / "acceptance_criteria_candidates.jsonl"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def read_rows(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def latest_by_id(path: Path) -> dict:
    latest = {}
    for row in read_rows(path):
        rid = row.get("id")
        if rid:
            latest[rid] = row
    return latest


def _next_id(rows: list) -> str:
    nums = []
    for rid in {r.get("id") for r in rows}:
        m = re.match(r"^AC-(\d+)$", str(rid))
        if m:
            nums.append(int(m.group(1)))
    return "AC-%04d" % ((max(nums) + 1) if nums else 1)


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def text_hash(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()[:16]


def _source_hashes_from_acceptance(row: dict) -> set:
    """从已登记验收项反推原话 hash，保证候选文件丢失后同一句话不复发。"""
    direct = str(row.get("source_hash") or "").strip()
    titles = []
    title = str(row.get("title") or "")
    if title.startswith("验收："):
        titles.append(title[len("验收："):])
    titles.extend([str(row.get("text") or ""), str(row.get("source_text") or "")])
    hashes = {text_hash(t) for t in titles if t}
    if direct:
        hashes.add(direct)
    return hashes


def signals(text: str) -> list:
    t = text or ""
    if any(s in t for s in SUPPRESS):
        return []
    return [(word, kind) for word, kind in SIGNALS if word in t]


def suggested_title(text: str) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) > 96:
        clean = clean[:96] + "…"
    return "验收：" + clean


def suggested_steps(text: str, needs_human: bool) -> str:
    if needs_human:
        return ("1) 找到对应的实现或界面；2) 按原话逐项复核：" + str(text or "")[:160]
                + "；3) 记录实际看到的结果，需要人看时补截图。")
    return ("1) 找到对应门禁；2) 跑真实验证并把命令 / cwd / 退出码写进 evidence；"
            "3) evidence 缺失时不得标 verified。原话：" + str(text or "")[:160])


def propose() -> dict:
    """扫描已路由入口原话，提名可判定验收标准候选。只读 inbox，不写验收台账。"""
    inbox = read_rows(inbox_path())
    cand_path = candidates_path()
    rows = read_rows(cand_path)
    known = {r.get("source_hash") for r in rows}
    # 已经 promote 的候选会以标题写进验收台账；同一句话即使候选文件丢失也不重提。
    for r in read_rows(truth_dir() / "acceptance.jsonl"):
        known |= _source_hashes_from_acceptance(r)
    created = []
    for it in inbox:
        if not it.get("routed"):
            continue
        text = (it.get("text") or "").strip()
        if not text:
            continue
        hits = signals(text)
        if not hits:
            continue
        h = text_hash(text)
        if h in known:
            continue
        machine = any(word in MACHINE_SIGNALS for word, _ in hits)
        needs_human = not machine
        title = suggested_title(text)
        row = {
            "id": _next_id(rows), "ts": _now(),
            "type": "acceptance_criteria_candidate", "status": "proposed",
            "text": text, "title": title,
            "needs_human": needs_human,
            "steps": suggested_steps(text, needs_human),
            "machine_gate_suggested": machine,
            "signals": [word for word, _ in hits],
            "files": [str(f) for f in (it.get("files") or []) if str(f).strip()],
            "source_ts": it.get("ts"), "source_lane": it.get("lane"),
            "source": "intake", "source_hash": h,
        }
        _append(cand_path, row)
        rows.append(row)
        known.add(h)
        created.append(row)
    return {"status": "ok", "created": created,
            "pending": [r for r in latest_by_id(cand_path).values()
                        if r.get("status") == "proposed"]}


def promote(cid: str, confirm: bool = False, needs_human=None,
            steps: str = "", level: str = "") -> dict:
    """人点头后才登记验收项；任何 evidence / screenshots 都不在这里伪造。"""
    if not confirm:
        return {"status": "error",
                "reason": "验收标准必须由人明确确认：请带 confirm=true（面板上点「确认成验收项」）。"}
    path = candidates_path()
    row = latest_by_id(path).get(cid)
    if not row:
        return {"status": "error", "reason": f"没有这张候选卡：{cid}"}
    if row.get("status") != "proposed":
        return {"status": "error",
                "reason": f"{cid} 当前状态是 {row.get('status')}，不是候选"}
    human = row.get("needs_human") if needs_human is None else bool(needs_human)
    argv = [sys.executable, str(OPS / "truthctl.py"), "accept", str(row["title"])]
    if human:
        argv.append("--human")
    # 不让调用方环境里的旧证据 / 旧截图穿进新验收项：这是「先自验」闸门的入口保护。
    env = os.environ.copy()
    for key in ("KG_TRUTH_EVIDENCE", "KG_TRUTH_SHOTS", "KG_TRUTH_STATUS",
                "KG_TRUTH_NEEDS_HUMAN", "KG_TRUTH_LANE"):
        env.pop(key, None)
    env["KG_TRUTH_STEPS"] = steps or str(row.get("steps") or "")
    env["KG_TRUTH_SOURCE"] = "acceptance_criteria"
    env["KG_TRUTH_SOURCE_HASH"] = str(row.get("source_hash") or "")
    files = row.get("files") or []
    if files:
        env["KG_TRUTH_FILES"] = ":".join(str(f) for f in files)
    if level:
        env["KG_TRUTH_LEVEL"] = str(level)
    proc = subprocess.run(argv, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        return {"status": "error",
                "reason": (proc.stderr or proc.stdout or "").strip()[-300:]}
    m = re.search(r"\[(A-\d+)\]", proc.stdout or "")
    done = dict(row)
    done.update({"status": "promoted", "promoted_ts": _now(),
                 "acceptance_id": m.group(1) if m else None,
                 "needs_human": bool(human), "truthctl": proc.stdout.strip()[-300:]})
    _append(path, done)
    return {"status": "promoted", "candidate": done}


def dismiss(cid: str, reason: str = "") -> dict:
    path = candidates_path()
    row = latest_by_id(path).get(cid)
    if not row:
        return {"status": "error", "reason": f"没有这张候选卡：{cid}"}
    if row.get("status") != "proposed":
        return {"status": "error",
                "reason": f"{cid} 当前状态是 {row.get('status')}，不是候选"}
    done = dict(row)
    done.update({"status": "dismissed", "dismissed_ts": _now(), "reason": reason})
    _append(path, done)
    return {"status": "dismissed", "candidate": done}


def pending() -> list:
    return [r for r in latest_by_id(candidates_path()).values()
            if r.get("status") == "proposed"]


def human() -> str:
    rows = pending()
    if not rows:
        return "验收管家：当前没有待你确认的验收标准候选。"
    out = [f"验收管家：{len(rows)} 条验收标准候选待你确认", ""]
    for r in sorted(rows, key=lambda x: x["id"]):
        who = "需要你看" if r.get("needs_human") else "先跑门禁自证"
        out.append(f"  {r['id']}  [{who}] {r.get('title')}")
        out.append(f"        原话：{r.get('text')}")
        out.append(f"        命中信号：{'、'.join(r.get('signals') or [])}")
        out.append(f"        确认：python3 ops/acceptance_criteria.py promote {r['id']} --confirm")
        out.append(f"        不算：python3 ops/acceptance_criteria.py dismiss {r['id']}")
    return "\n".join(out)


def daemon_hook(dry: bool = False, log_fn=None) -> dict:
    """守护每拍只提名候选；登记验收项必须先经人确认。异常由调用方 fail-open。"""
    res = propose()
    if log_fn and res.get("created"):
        log_fn(f"[acceptance] 提名 {len(res['created'])} 条验收标准候选："
               + "、".join(f"{c['id']}" for c in res["created"]))
    return res


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(description="验收管家：验收标准候选的提名 / 确认 / 丢弃")
    ap.add_argument("cmd", nargs="?", default="list",
                    choices=["propose", "list", "promote", "dismiss"])
    ap.add_argument("id", nargs="?", default="")
    ap.add_argument("--confirm", action="store_true", help="人已明确同意登记验收项")
    ap.add_argument("--steps")
    ap.add_argument("--level")
    ap.add_argument("--reason")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "propose":
        res = propose()
    elif args.cmd == "promote":
        if not args.id:
            print("用法: acceptance_criteria.py promote <AC-id> --confirm")
            return 2
        res = promote(args.id, confirm=args.confirm, steps=args.steps or "",
                      level=args.level or "")
    elif args.cmd == "dismiss":
        if not args.id:
            print("用法: acceptance_criteria.py dismiss <AC-id>")
            return 2
        res = dismiss(args.id, args.reason or "")
    else:
        res = {"status": "ok"}

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print(human())
        if res.get("status") not in {None, "ok"}:
            print()
            print(f"[{res.get('status')}] {res.get('reason') or res.get('status')}")
    return 2 if res.get("status") == "error" else 0

import shlex
from pathlib import Path
from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def _dedup(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for check in checks:
        key = (_text(check.get("name")), _text(check.get("cmd")), _text(check.get("cwd")))
        if key in seen:
            continue
        seen.add(key)
        out.append(check)
    return out


def _machine(name: str, cmd: str, cwd: str, why: str) -> dict[str, str]:
    return {
        "name": _text(name) or "gate",
        "cmd": _text(cmd),
        "cwd": _text(cwd) or ".",
        "pass_condition": "exit 0",
        "why": _text(why),
    }


def _configured_gates(cfg: dict[str, Any]) -> list[dict[str, str]]:
    try:
        import central_config
    except ImportError:
        return []
    gates = central_config.verify_gates(cfg or {})
    return [
        _machine(gate.get("name"), gate.get("cmd"), gate.get("cwd"), "项目配置的验证门禁")
        for gate in gates
        if _text(gate.get("cmd"))
    ]


def _file_gates(files: list[str]) -> list[dict[str, str]]:
    paths = [_text(path) for path in (files or []) if _text(path)]
    checks: list[dict[str, str]] = []
    py_files = [path for path in paths if path.endswith(".py")]
    if py_files:
        quoted = " ".join(shlex.quote(path) for path in py_files[:8])
        checks.append(_machine(
            "py-compile", f"python3 -m py_compile {quoted}", ".",
            "这次改动包含 Python 文件，至少要保证可编译"))
    if any("panel.html" in path or path.endswith((".tsx", ".ts", ".jsx", ".js", ".css"))
           for path in paths):
        checks.append(_machine(
            "panel-logic", "python3 ops/panel_logic_check.py", ".",
            "界面代码改动必须过面板首屏逻辑门禁"))
        checks.append(_machine(
            "panel-render", "python3 ops/panel_render_check.py --no-shot", ".",
            "界面代码改动必须真实渲染"))
    if any(Path(path).name in ("central.config.json", "AGENTS.md") for path in paths):
        checks.append(_machine(
            "selftest", "python3 ops/selftest.py", ".",
            "控制面/全局约定改动必须过总闸"))
    return checks


def _human_checks(text: str, kind: str) -> list[dict[str, str]]:
    low = f"{text} {kind}".lower()
    visual = kind in ("visual", "ux") or any(
        key in low for key in (
            "视觉", "排版", "样式", "颜色", "字号", "间距", "按钮位置", "遮挡", "裁切",
            "手感", "顺手", "体验", "交互", "点击", "跳转", "入口", "操作路径", "看起来", "观感"))
    if not visual:
        return []
    return [
        {
            "step": "打开真实面板/页面，按需求描述的路径操作一遍",
            "pass_condition": "用户看到预期结果，且没有遮挡、错位或多余提示",
            "why": "视觉/交互结果不能由命令退出码代替人眼确认",
        },
        {
            "step": "刷新后再看一次，确认状态不是缓存假象",
            "pass_condition": "刷新后结果保持，关键文字和状态没有回退",
            "why": "防止只在单次渲染里看起来正确",
        },
    ]


def compile_criteria(text: str, kind: str, files: list[str] | None,
                     lane: str = "", cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """把一条需求编译成可判定验收标准。纯函数，不写文件、不执行命令。"""
    machine = _dedup(_configured_gates(cfg or {}) + _file_gates(list(files or [])))
    human = _human_checks(_text(text), _text(kind))
    status = "ok" if machine or human else "unknown"
    parts: list[str] = []
    if machine:
        parts.append(f"机器门禁 {len(machine)} 条（每条 exit 0）")
    if human:
        parts.append(f"人工复核 {len(human)} 条")
    summary = "；".join(parts) or "没有推导出可判定条件"
    return {
        "status": status,
        "summary": summary,
        "lane": _text(lane),
        "machine_checks": machine,
        "human_checks": human,
        "needs_human": bool(human),
        "source": "acceptance-criteria-compiler",
    }


def render_criteria(criteria: Any, limit: int = 4) -> str:
    """执行 prompt / 面板工单表的紧凑只读投影。"""
    if not isinstance(criteria, dict):
        return ""
    parts: list[str] = []
    for check in (criteria.get("machine_checks") or [])[:limit]:
        if not isinstance(check, dict):
            continue
        parts.append("门禁 %s: `%s` (cwd=%s) -> exit 0" % (
            check.get("name") or "gate", check.get("cmd") or "", check.get("cwd") or "."))
    for check in (criteria.get("human_checks") or [])[:max(0, limit - len(parts))]:
        if not isinstance(check, dict):
            continue
        parts.append("人工 %s（通过：%s）" % (
            check.get("step") or "", check.get("pass_condition") or ""))
    return "；".join(parts)


def summary_label(criteria: Any) -> str:
    """工单表用的一行摘要：只报条数，不把内部命令堆到首屏。"""
    if not isinstance(criteria, dict):
        return ""
    machine = criteria.get("machine_checks") or []
    human = criteria.get("human_checks") or []
    if not isinstance(machine, list):
        machine = []
    if not isinstance(human, list):
        human = []
    if not machine and not human:
        return ""
    return f"验收标准：机器 {len(machine)} 条 / 人工 {len(human)} 条"


def render_for_prompt(criteria: Any) -> list[str]:
    """把验收标准转成执行 prompt 能照抄的硬约束。"""
    if not isinstance(criteria, dict):
        return []
    lines = ["", "本工单的可判定验收标准（不是建议，做完必须逐条自证）："]
    checks = criteria.get("machine_checks") or []
    for check in checks:
        if not isinstance(check, dict):
            continue
        lines.append(
            f"- [{check.get('name') or 'gate'}] `{check.get('cmd')}` "
            f"(cwd={check.get('cwd') or '.'}) 必须真跑，通过条件是 "
            f"{check.get('pass_condition') or 'exit 0'}；在总结里写命令、cwd 和退出码。")
    for i, check in enumerate(criteria.get("human_checks") or [], 1):
        if not isinstance(check, dict):
            continue
        lines.append(
            f"- 人工复核 {i}：{check.get('step') or ''}；"
            f"通过条件：{check.get('pass_condition') or ''}。")
    if lines and len(lines) == 1:
        lines.append("- 当前没有推导出可判定条件；先补文件线索或验收标准，不许自己宣布完成。")
    return lines

if __name__ == "__main__":
    sys.exit(main())
