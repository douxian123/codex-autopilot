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


if __name__ == "__main__":
    sys.exit(main())
