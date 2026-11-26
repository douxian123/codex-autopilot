#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""constraint_candidates —— 上下文管家的「自动收录」（stdlib only）。

用户要的上下文管家是：**任何入口**说过的预期/返修/决策/禁忌自动收成项目约束，
说过一次就不再犯。实测（2026-11-18）这条通路根本没接线：`truthctl route` 只写
泳道与问题台账，从不蒸馏约束；本项目 `constraints.jsonl` 是 0 条，只能人肉
手写。于是「自动整理、归档和调取」只剩最后半截。

为什么不直接写进 `constraints.jsonl`
-----------------------------------
约束一旦写进去就会**参与落盘门禁与执行者 prompt**，写错一条就是全局污染。
「从自然语言判断这是不是一条约束」属于语义判断，按本仓库的 pass_registry 纪律
只能停在 `propose`：所以本模块只产出**候选卡**，附上用户原话与命中信号作为证据；
人点头（`promote`）才真正调用 `truthctl constraints add` 升成生效约束。

这样既补上了「自动收录」，又不会让系统替用户决定「你刚才那句话算不算规范」。
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

# 只在出现明确的规范性措辞时才提名，不做「任何句子都当约束」的滥收。
# 每条 = (命中词, 建议 kind)。顺序即优先级。
SIGNALS = [
    ("绝不允许", "taboo"), ("绝对不允许", "taboo"), ("禁止", "taboo"),
    ("不许", "taboo"), ("不能", "taboo"),
    ("不要再", "rework"), ("别再", "rework"), ("返修", "rework"),
    ("必须", "expectation"), ("不得", "expectation"), ("应当", "expectation"),
    ("应该", "expectation"), ("一律", "expectation"), ("以后都", "expectation"),
    ("永远", "expectation"), ("默认", "expectation"), ("从现在起", "expectation"),
    ("从今以后", "expectation"), ("每次", "expectation"), ("确保", "expectation"),
    ("记住", "decision"), ("拍板", "decision"), ("就这么定", "decision"),
    ("按这个来", "decision"),
]

# 命中这些词但其实是「描述现状」的句子，不提名，避免把 bug 描述误收成规范。
SUPPRESS = ("曾达", "实测", "复现", "报错原文", "日志如下")

# scope 分类只影响候选卡和宪法页的呈现；真正生效时的 scope 仍由人确认。
# 「每次提交」是工作流程，不是无限扩大到所有场景的全局规则；先判明确流程词，
# 再判「每次」这种只表达重复频次的词。
EXPLICIT_GLOBAL_MARKERS = ("全局", "全项目", "所有项目", "任何入口", "任何对话",
                           "所有窗口", "永远", "一律", "从现在起", "从今以后")
REPEAT_MARKERS = ("每次", "每次都")
WORKFLOW_MARKERS = ("工作流程", "流程", "门禁", "收尾", "验收", "提交",
                    "合入", "落盘", "闭环", "台账", "宪法", "规范")
PROJECT_MARKERS = ("本项目", "这个项目", "项目")
STRONG_SIGNALS = {"绝不允许", "绝对不允许", "禁止", "不许", "不能", "必须",
                  "不得", "应当", "一律", "永远", "每次", "记住", "拍板",
                  "就这么定", "按这个来"}


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
    return truth_dir() / "constraint_candidates.jsonl"


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
        m = re.match(r"^CC-(\d+)$", str(rid))
        if m:
            nums.append(int(m.group(1)))
    return "CC-%04d" % ((max(nums) + 1) if nums else 1)


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def text_hash(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()[:16]


def signals(text: str) -> list:
    """返回命中的 (词, kind)；命不中就是空列表（这条不是规范，不提名）。"""
    t = text or ""
    if any(s in t for s in SUPPRESS):
        return []
    hits = []
    for word, kind in SIGNALS:
        if word in t:
            hits.append((word, kind))
    return hits


def suggested_scope(files) -> list:
    """把 inbox 的文件线索当 scope：只在那块代码改动时才带上这条约束。"""
    out = []
    for f in files or []:
        f = str(f).strip()
        if f and f not in out:
            out.append(f)
    return out


def classify_scope(text, files=None) -> tuple:
    """给候选卡打 scope 分类：global / workflow / module / project。

    全局与工作流程是对所有改动都该生效的规则；文件线索则是精准调取用的模块范围。
    这里只分类，不决定是否生效，也不把分类写成机器门禁。
    """
    t = str(text or "")
    if any(marker in t for marker in EXPLICIT_GLOBAL_MARKERS):
        return "global", []
    if any(marker in t for marker in WORKFLOW_MARKERS):
        return "workflow", suggested_scope(files)
    if any(marker in t for marker in REPEAT_MARKERS):
        return "global", []
    scoped = suggested_scope(files)
    if scoped:
        return "module", scoped
    if any(marker in t for marker in PROJECT_MARKERS):
        return "project", []
    # 没写文件、没写流程词，但明确是规范：按项目范围收，不私自扩大成全局。
    return "project", []


def candidate_priority(hits, scope_class) -> str:
    """明确红线、全局/流程规范优先进入候选区；弱措辞仍保留为普通候选。"""
    words = {word for word, _kind in hits}
    if scope_class in ("global", "workflow") or words & STRONG_SIGNALS:
        return "high"
    return "normal"


def propose() -> dict:
    """扫描已路由的入口原话，为命中规范性措辞的条目提名候选卡。只读 inbox。

    去重按**原话**，且不看候选当前状态：promoted / dismissed 过的同一句话永远不再
    提名。否则用户每否掉一次，下一拍又弹回来，就从「自动收录」变成了「反复烦人」。
    """
    inbox = read_rows(inbox_path())
    cand_path = candidates_path()
    live = latest_by_id(cand_path)
    known = {r.get("source_hash") for r in live.values()}
    rows = read_rows(cand_path)
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
        kind = hits[0][1]
        scope_class, scope = classify_scope(text, it.get("files"))
        row = {
            "id": _next_id(rows), "ts": _now(), "type": "constraint_candidate",
            "status": "proposed", "text": text,
            "title": text[:80],
            "kind": kind,
            "scope": scope,
            "scope_class": scope_class,
            "priority": candidate_priority(hits, scope_class),
            "signals": [h[0] for h in hits],
            "source_ts": it.get("ts"),
            "source_lane": it.get("lane"),
            "source": "intake",
            "source_hash": h,
            # 候选不参与门禁：真正生效要人 promote。
            "enforceable": "none",
        }
        _append(cand_path, row)
        rows.append(row)
        known.add(h)
        created.append(row)
    return {"status": "ok", "created": created,
            "pending": [r for r in latest_by_id(cand_path).values()
                        if r.get("status") == "proposed"]}


def propose_learned(key, statement, scope=None, evidence=None, priority="normal",
                    mentions=0, threads=0, sample="") -> dict:
    """上下文管家自学习提名：把反复出现的共性诉求送进同一张候选闸。

    它与 `propose()` 的区别只在来源和证据形态：这里不是「一句话命中规范词」，
    而是「同主题跨会话反复出现」。候选卡结构保持一致，因此面板、promote、
    dismiss 三处都不需要第二套机制。候选仍未写进 `constraints.jsonl`。
    """
    text = " ".join(str(statement or "").split())
    if not text:
        return {"status": "error", "reason": "学习候选没有可确认的语句"}
    topic = " ".join(str(key or "").split()) or text_hash(text)
    scope_class, scoped = classify_scope(text, scope)
    h = "learned:" + text_hash(topic + "\n" + text)
    path = candidates_path()
    live = latest_by_id(path)
    for row in live.values():
        if row.get("source_hash") == h and row.get("status") == "proposed":
            return {"status": "ok", "created": [], "id": row.get("id")}
    rows = read_rows(path)
    row = {
        "id": _next_id(rows), "ts": _now(), "type": "constraint_candidate",
        "status": "proposed", "text": text, "title": text[:80],
        # 学习候选是共性规范建议，kind 统一走 expectation；原话证据仍保留在卡里。
        "kind": "expectation",
        "scope": scoped,
        "scope_class": scope_class,
        "signals": ["context_learning", "跨会话重复"],
        "source": "context_learning",
        "source_hash": h,
        "source_topic": topic,
        "source_sample": " ".join(str(sample or "").split())[:240],
        "evidence": evidence or [],
        "priority": "high" if scope_class in ("global", "workflow") else priority,
        "mentions": int(mentions or 0),
        "threads": int(threads or 0),
        # 候选不参与门禁；确认后由人决定 enforceable，绝不自动标 gate。
        "enforceable": "none",
    }
    _append(path, row)
    return {"status": "ok", "created": [row], "id": row["id"]}


def promote(cid: str, kind=None, scope=None, enforceable=None, cmd=None, cwd=None,
            env_extra=None) -> dict:
    """人点头后，把候选卡升成真正生效的约束（走 truthctl，不另造第二套真相）。"""
    path = candidates_path()
    row = latest_by_id(path).get(cid)
    if not row:
        return {"status": "error", "reason": f"没有这张候选卡：{cid}"}
    if row.get("status") != "proposed":
        return {"status": "error", "reason": f"{cid} 当前状态是 {row.get('status')}，不是候选"}
    kind = (kind or row.get("kind") or "expectation").strip()
    scope = scope if scope is not None else (row.get("scope") or [])
    if isinstance(scope, (list, tuple)):
        scope = ":".join(str(s) for s in scope)
    scope_class = row.get("scope_class") or classify_scope(row.get("text"), row.get("scope"))[0]
    enforceable = (enforceable or "none").strip()
    env = {
        "KG_CONSTRAINT_KIND": kind,
        "KG_TRUTH_SOURCE_HASH": str(row.get("source_hash") or "") or None,
        "KG_CONSTRAINT_SCOPE": scope or None,
        "KG_CONSTRAINT_SCOPE_CLASS": scope_class,
        "KG_CONSTRAINT_ENFORCEABLE": enforceable,
        "KG_CONSTRAINT_CMD": cmd or None,
        "KG_CONSTRAINT_CWD": cwd or None,
        "KG_TRUTH_SOURCE": "user",
    }
    proc = subprocess.run(
        [sys.executable, str(OPS / "truthctl.py"), "constraints", "add", row["text"]],
        capture_output=True, text=True,
        env={**os.environ, **{k: v for k, v in env.items() if v},
             **{k: str(v) for k, v in (env_extra or {}).items() if v}})
    if proc.returncode != 0:
        return {"status": "error",
                "reason": (proc.stderr or proc.stdout or "").strip()[-300:]}
    m = re.search(r"\[(C-\d+)\]", proc.stdout or "")
    done = dict(row)
    done.update({"status": "promoted", "promoted_ts": _now(),
                 "constraint_id": m.group(1) if m else None,
                 "kind": kind, "scope": suggested_scope(scope.split(":")),
                 "scope_class": scope_class,
                 "enforceable": enforceable, "truthctl": proc.stdout.strip()[-300:]})
    _append(path, done)
    return {"status": "promoted", "candidate": done}


def dismiss(cid: str, reason: str = "") -> dict:
    path = candidates_path()
    row = latest_by_id(path).get(cid)
    if not row:
        return {"status": "error", "reason": f"没有这张候选卡：{cid}"}
    if row.get("status") != "proposed":
        return {"status": "error", "reason": f"{cid} 当前状态是 {row.get('status')}，不是候选"}
    done = dict(row)
    done.update({"status": "dismissed", "dismissed_ts": _now(), "reason": reason})
    _append(path, done)
    return {"status": "dismissed", "candidate": done}


def pending() -> list:
    return [r for r in latest_by_id(candidates_path()).values()
            if r.get("status") == "proposed"]


def daemon_hook(dry: bool = False, log_fn=None) -> dict:
    """守护每拍调这里：只提名候选卡，绝不写生效约束。异常由调用方吞掉（fail-open）。"""
    res = propose()
    if log_fn and res.get("created"):
        log_fn(f"[constraint] 提名 {len(res['created'])} 条候选约束："
               + "、".join(f"{c['id']}" for c in res["created"]))
    return res


def human() -> str:
    rows = pending()
    if not rows:
        return "上下文管家：当前没有待你确认的约束候选。"
    out = [f"上下文管家：{len(rows)} 条候选约束待你确认", ""]
    for r in sorted(rows, key=lambda x: x["id"]):
        scope = "、".join(r.get("scope") or []) or "全局"
        out.append(f"  {r['id']}  [{r.get('kind')}|{scope}] {r.get('title')}")
        out.append(f"        命中信号：{'、'.join(r.get('signals') or [])}")
        out.append(f"        确认：python3 ops/constraint_candidates.py promote {r['id']}")
        out.append(f"        不算：python3 ops/constraint_candidates.py dismiss {r['id']}")
    return "\n".join(out)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(description="上下文管家：约束候选的提名 / 确认 / 丢弃")
    ap.add_argument("cmd", nargs="?", default="list",
                    choices=["propose", "list", "promote", "dismiss"])
    ap.add_argument("id", nargs="?", default="")
    ap.add_argument("--kind")
    ap.add_argument("--scope")
    ap.add_argument("--enforceable")
    ap.add_argument("--cmd")
    ap.add_argument("--cwd")
    ap.add_argument("--reason")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "propose":
        res = propose()
    elif args.cmd == "promote":
        if not args.id:
            print("用法: constraint_candidates.py promote <CC-id>")
            return 2
        res = promote(args.id, kind=args.kind, scope=args.scope,
                      enforceable=args.enforceable, cmd=args.cmd, cwd=args.cwd)
    elif args.cmd == "dismiss":
        if not args.id:
            print("用法: constraint_candidates.py dismiss <CC-id>")
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
