#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PM 施工闭环节气：十条真实输入必须走完原话->施工->依赖->路线->追问->关闭。"""

from __future__ import annotations

import importlib.util
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

OPS = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("pm_closure_loop_under_test",
                                              OPS / "pm_closure_loop.py")
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def check(label, cond, detail=""):
    if not cond:
        raise AssertionError(f"{label}: {detail}")
    print(f"  ok   {label}")


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")


REAL_INPUTS = [
    ("L-0001", "自动挡不是脚手架，是项目运行层", "arch", ["ops/kgctl.py"]),
    ("L-0002", "用户不该自己选对话、分支或角色", "ux", ["ops/central_mcp.py"]),
    ("L-0003", "上下文档案要能看到自动收录了什么", "visual", ["plugin/ui/panel.html"]),
    ("L-0004", "面板首屏只回答三问", "visual", ["plugin/ui/panel.html"]),
    ("L-0005", "unknown 不能写成 0、无或正常", "gate", ["ops/selftest.py"]),
    ("L-0006", "验收候选先确认，机器项先跑真证据", "gate", ["ops/test-acceptance-criteria.py"]),
    ("L-0007", "worker 的 start/status/stop/event 要可审计", "arch", ["ops/worker_lifecycle.py"]),
    ("L-0008", "harness 健康取不到就如实说，不能自动改配置", "bug", ["ops/central_daemon.py"]),
    ("L-0009", "插件要能安装、运行、被用户找到", "ux", ["install.sh"]),
    ("L-0010", "发布动作必须由用户拍板", "arch", ["ops/kgctl.py"]),
]


def main():
    now = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    old = (now - timedelta(minutes=90)).strftime("%Y-%m-%d %H:%M:%S")

    with tempfile.TemporaryDirectory(prefix="pm-closure-loop-") as tmp:
        root = Path(tmp)
        truth = root / "ops" / "truth"
        inbox = root / "ops" / "tasks" / "inbox.jsonl"
        ledger = []
        inbox_rows = []
        native = []
        for rid, text, kind, files in REAL_INPUTS:
            ledger.append({"id": rid, "ts": old, "status": "doing",
                           "title": text, "kind": kind, "lane": "design",
                           "files": files, "source": "user"})
            inbox_rows.append({"text": text, "ledger_id": rid, "routed": True,
                               "files": files})
            native.append({"ledger_id": rid, "thread_id": "thread-" + rid,
                           "ts": old})
        ledger.append({"id": "L-0001", "ts": old, "status": "done",
                       "title": "自动挡不是脚手架，是项目运行层",
                       "kind": "arch", "lane": "design",
                       "files": ["ops/kgctl.py"], "source": "user",
                       "evidence": "python3 ops/selftest.py (cwd=.) -> exit 0"})
        native.append({"ledger_id": "L-0010", "thread_id": "thread-0010"})
        write(truth / "ledger.jsonl", ledger)
        write(truth / "native_routes.jsonl", native)
        write(inbox, inbox_rows)
        (truth / "mainline.json").write_text(json.dumps({
            "stage": "内部可用",
            "current_focus": ["PM 施工闭环"],
            "roadmap": [
                {"step": "项目运行层", "expected": "自动挡接住零散需求", "status": "doing"},
                {"step": "面板首屏", "expected": "一眼看懂现在怎么样", "status": "done"},
            ],
        }, ensure_ascii=False), encoding="utf-8")

        runtime = {"blocked": {"exec_waiting": [{
            "id": "L-0003", "holders": [{"id": "L-0004"}],
            "files": ["plugin/ui/panel.html"],
        }]}}
        before = {p.name: p.read_bytes() for p in truth.glob("*") if p.is_file()}
        data = mod.compute(root, runtime=runtime, now=now)
        after = {p.name: p.read_bytes() for p in truth.glob("*") if p.is_file()}

        check("台账齐全时施工路径可读", data.get("status") == "attention", data)
        check("十条真实输入全部生成路径", len(data.get("paths") or []) == 10,
              len(data.get("paths") or []))
        by_id = {p["work_item"]["id"]: p for p in data["paths"]}
        for rid, text, _kind, files in REAL_INPUTS:
            p = by_id.get(rid) or {}
            check(rid + " 保留原话", text in p.get("source_text", ""), p.get("source_text"))
            check(rid + " 有施工项", p.get("work_item", {}).get("title") == text,
                  p.get("work_item"))
            check(rid + " 有文件占地口径",
                  p["work_item"]["occupancy"] == ("known" if files else "unknown"),
                  p["work_item"])
            check(rid + " 有依赖结论", p.get("dependencies", {}).get("status") in ("ok", "unknown"),
                  p.get("dependencies"))
            check(rid + " 有路线投影", p.get("roadmap", {}).get("status") in ("done", "doing", "next", "todo", "unknown"),
                  p.get("roadmap"))
            check(rid + " 有追问边界", "auto_sent" not in p.get("follow_up", {}).get("candidate", {})
                  or p["follow_up"]["candidate"]["auto_sent"] is False,
                  p.get("follow_up"))
            check(rid + " 有关闭路径", bool(p.get("close", {}).get("note")), p.get("close"))

        check("文件重叠生成真实依赖",
              any(d.get("id") == "L-0004" for d in by_id["L-0003"]["dependencies"]["items"]),
              by_id["L-0003"]["dependencies"])
        check("让路队列进入施工路径",
              by_id["L-0003"]["hold"]["status"] == "ok"
              and by_id["L-0003"]["hold"]["waiting_for"] == ["L-0004"],
              by_id["L-0003"]["hold"])
        check("路线只读投影命中",
              by_id["L-0001"]["roadmap"]["step"] == "项目运行层"
              and by_id["L-0001"]["roadmap"]["status"] == "doing",
              by_id["L-0001"]["roadmap"])
        check("已关闭项带真证据", by_id["L-0001"]["close"]["status"] == "closed"
              and "exit 0" in by_id["L-0001"]["close"]["evidence"],
              by_id["L-0001"]["close"])
        check("未完成项不冒充关闭",
              by_id["L-0010"]["close"]["status"] == "open"
              and "truthctl.py close" in by_id["L-0010"]["close"]["command"],
              by_id["L-0010"]["close"])
        check("long-idle 只生成候选不发送",
              by_id["L-0002"]["follow_up"]["candidate"]["needs_human"] is True
              and by_id["L-0002"]["follow_up"]["candidate"]["auto_sent"] is False,
              by_id["L-0002"]["follow_up"])
        check("compute 不改任何真相文件", before == after, (before, after))

        no_files_ledger = []
        for row in ledger:
            clone = dict(row)
            if clone.get("id") != "L-0001":
                clone["files"] = []
            no_files_ledger.append(clone)
        write(truth / "ledger.jsonl", no_files_ledger)
        no_files = mod.compute(root, runtime={"blocked": {}}, now=now)
        nf = {p["work_item"]["id"]: p for p in no_files["paths"]}
        check("没有文件声明不假装不重叠",
              any(p["work_item"]["occupancy"] == "unknown"
                  and p["dependencies"]["status"] == "unknown"
                  and "不假装不冲突" in p["dependencies"]["note"]
                  for p in nf.values()), nf)
        write(truth / "ledger.jsonl", ledger)

        direct = mod.compute(root, runtime=runtime, now=now)
        check("人话输出可复核", "L-" in mod.human(direct), mod.human(direct))

        empty = Path(tmp) / "empty"
        empty.mkdir()
        data2 = mod.compute(empty, now=now)
        check("台账缺失报 unknown", data2["status"] == "unknown", data2)
        check("unknown 不写成 0 条", "不把不知道当成 0 条" in mod.human(data2),
              mod.human(data2))

    print("pm-closure-loop: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
