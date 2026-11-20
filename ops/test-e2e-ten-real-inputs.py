#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端门禁：十条真实输入必须走完归一、施工、验证、运行、收口与面板可见。

这不是把已有单测再跑一遍。它在临时状态根里把十条原始入口原话真正送进
`truthctl route --go`，再用 PM、上下文档案、验收编译、worker 生命周期、
wake/resume、`truthctl close` 和面板静态契约逐段核对：

  * 原话保留、路由落账、落盘可复核；
  * 验收标准先编译，候选不自动变成已验；
  * 上下文读失败写 unknown，不写 0；
  * worker start/status/stop/event/resume 可审计，唤醒只投递一次；
  * 关闭必须带命令、cwd、退出码；发布动作仍然不自动执行。

退出码：0 全过；非 0 为未通过项数。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
REPO = OPS.parent

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


def check(label: str, condition: bool, detail="") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        raise AssertionError(f"{label}: {detail}")


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, OPS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def latest_by_id(rows: list) -> dict:
    out = {}
    for row in rows:
        if row.get("id"):
            out[row["id"]] = row
    return out


def run_truthctl(env: dict, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(OPS / "truthctl.py"), *args],
                          cwd=str(REPO), env=env, capture_output=True, text=True)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="e2e-ten-real-inputs-") as tmp:
        root = Path(tmp)
        repo = root / "repo"
        truth = repo / "ops" / "truth"
        tasks = repo / "ops" / "tasks"
        truth.mkdir(parents=True)
        tasks.mkdir(parents=True)

        inbox_rows = [
            {"ts": "2026-11-19 12:00:00", "text": text, "files": files, "kind": kind}
            for _rid, text, kind, files in REAL_INPUTS
        ]
        write_jsonl(tasks / "inbox.jsonl", inbox_rows)
        write_jsonl(truth / "ledger.jsonl", [])
        write_jsonl(truth / "progress.jsonl", [])
        write_jsonl(truth / "acceptance.jsonl", [])
        write_jsonl(truth / "constraints.jsonl", [])
        write_jsonl(truth / "constraint_candidates.jsonl", [{
            "id": "CC-E2E-0001",
            "status": "proposed",
            "text": "上下文档案要能看到自动收录了什么",
            "source": "intake",
            "signals": ["自动收录"],
            "scope": ["plugin/ui/panel.html"],
            "evidence": [{"thread": "thread-e2e-0003", "text": "上下文档案要能看到自动收录了什么"}],
        }])
        write_jsonl(truth / "native_routes.jsonl", [])
        (truth / "mainline.json").write_text(json.dumps({
            "stage": "闭环验证",
            "current_focus": ["十条真实需求端到端击穿"],
            "roadmap": [
                {"step": "项目运行层", "expected": "自动挡接住零散需求", "status": "done"},
                {"step": "十条真实需求端到端击穿", "expected": "逐条可复核", "status": "doing"},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        (repo / "ops" / "central.config.json").write_text(json.dumps({
            "main_branch": "main",
            "verify": [],
            "lanes": [
                {"name": "design", "title": "体验与视觉", "gate": "面板门禁", "status": "active"},
                {"name": "bug", "title": "缺陷修复", "gate": "复现门禁", "status": "active"},
                {"name": "arch", "title": "架构与重构", "gate": "回归门禁", "status": "active"},
                {"name": "gate", "title": "门禁与验证", "gate": "真门禁", "status": "active"},
            ],
        }, ensure_ascii=False), encoding="utf-8")

        env = {
            **os.environ,
            "KG_REPO": str(repo),
            "KG_TRUNK": str(repo),
            "KG_TRUTH_ID_STATE_ROOT": str(root / "truth-ids"),
            "KG_TRUTH_SOURCE": "e2e-test",
            "KG_DAEMON_HOME": str(root / "runtime"),
            "KG_WAKE_RESUME_ROOT": str(root / "wake"),
            "KG_WORKER_LIFECYCLE_ROOT": str(root / "lifecycle"),
        }
        os.environ.update({k: env[k] for k in (
            "KG_REPO", "KG_TRUNK", "KG_TRUTH_ID_STATE_ROOT", "KG_TRUTH_SOURCE",
            "KG_DAEMON_HOME", "KG_WAKE_RESUME_ROOT", "KG_WORKER_LIFECYCLE_ROOT")})

        # 1. 唯一入口 -> 固定泳道 -> 问题台账，十条原话必须逐条落账。
        routed = run_truthctl(env, "route", "--go")
        check("truthctl route --go 成功", routed.returncode == 0,
              routed.stdout + routed.stderr)
        ledger = latest_by_id(read_jsonl(truth / "ledger.jsonl"))
        check("十条真实输入全部落账", len(ledger) == 10, len(ledger))
        by_text = {str(row.get("detail") or ""): row for row in ledger.values()}
        for _rid, text, _kind, _files in REAL_INPUTS:
            row = by_text.get(text) or {}
            check(text + " 原话写入台账", bool(row), row)
            plan = row.get("acceptance_plan") or {}
            check(text + " 路由时编译验收标准",
                  plan.get("status") in ("ok", "unknown"), plan)
        check("inbox 全部标为已路由",
              all(row.get("routed") is True for row in read_jsonl(tasks / "inbox.jsonl")),
              read_jsonl(tasks / "inbox.jsonl"))

        # 2. 验收标准编译：机器项与人工项分开，候选不自动变成已验。
        compiler = load_module("acceptance_compiler_under_e2e", "acceptance_criteria_compiler.py")
        for rid, text, kind, files in REAL_INPUTS:
            criteria = compiler.compile_criteria(text, kind, files, lane="gate")
            check(rid + " 验收标准可判定", criteria.get("status") == "ok", criteria)
        unknown_criteria = compiler.compile_criteria("", "arch", [], lane="arch")
        check("没有可判定条件时写 unknown",
              unknown_criteria.get("status") == "unknown", unknown_criteria)
        check("验收候选没有自动写进验收台账",
              read_jsonl(truth / "acceptance.jsonl") == [],
              read_jsonl(truth / "acceptance.jsonl"))

        # 3. 上下文档案：可见、有来源、读失败写 unknown。
        context = load_module("context_archive_under_e2e", "context_archive.py")
        archive = context.collect(truth)
        check("上下文档案可读", archive.get("status") == "ok", archive)
        check("自动收录原话可见且可追溯",
              any(item.get("text") == "上下文档案要能看到自动收录了什么"
                  and (item.get("evidence") or [{}])[0].get("thread") == "thread-e2e-0003"
                  for item in archive.get("items") or []), archive)
        check("档案说明确认入口", "需要你决策" in str(archive.get("note") or ""),
              archive.get("note"))
        missing = context.collect(root / "missing-truth")
        check("档案读取失败写 unknown 而非 0",
              missing.get("status") == "unknown"
              and "不把不知道当成 0 条" in str(missing.get("reason") or ""), missing)

        # 4. PM 施工路径：原话、依赖、路线、追问、关闭入口都能从账本投影。
        pm = load_module("pm_closure_under_e2e", "pm_closure_loop.py")
        paths = pm.compute(repo, runtime={"blocked": {"exec_waiting": []}})
        check("PM 施工路径可读", paths.get("status") == "attention", paths.get("status"))
        check("PM 投影覆盖十条输入", len(paths.get("paths") or []) == 10,
              len(paths.get("paths") or []))
        pm_by_text = {p.get("source_text"): p for p in paths.get("paths") or []}
        for _rid, text, _kind, _files in REAL_INPUTS:
            item = pm_by_text.get(text) or {}
            check(text + " PM 保留原话与施工项",
                  item.get("work_item", {}).get("title") == text, item.get("work_item"))
            check(text + " 有依赖与关闭路径",
                  item.get("dependencies", {}).get("status") in ("ok", "unknown")
                  and bool(item.get("close", {}).get("command")
                           or item.get("close", {}).get("note")), item.get("close"))
        no_files = []
        for row in read_jsonl(truth / "ledger.jsonl"):
            clone = dict(row)
            clone["files"] = []
            no_files.append(clone)
        write_jsonl(truth / "ledger.jsonl", no_files)
        unknown_pm = pm.compute(repo, runtime={"blocked": {"exec_waiting": []}})
        check("没有文件声明不假装不重叠",
              all(p["dependencies"]["status"] == "unknown"
                  for p in unknown_pm.get("paths") or []), unknown_pm)
        write_jsonl(truth / "ledger.jsonl", list(ledger.values()))

        # 5. worker 生命周期：事件直达对应 worker，resume 保留 generation。
        wl = load_module("worker_lifecycle_under_e2e", "worker_lifecycle.py")
        workers = wl.WorkerLifecycle(root / "lifecycle")
        spec = {
            "worker_id": "e2e-L-0007",
            "ticket_id": "L-0007",
            "lane": "arch",
            "worktree": str(root / "wt-e2e"),
            "command": [sys.executable, "-c", "pass"],
            "resources": ["ops/worker_lifecycle.py"],
            "tools": ["read"],
            "authorization": "explicit",
            "effect": "reversible",
            "authorization_reason": "用户已允许端到端验证",
            "expected_evidence": "python3 ops/test-worker-lifecycle.py exit 0",
        }
        started = workers.start(spec, launch=lambda: "session-e2e-L-0007")
        check("worker 按工单启动",
              started.get("state") == "running"
              and (started.get("spec") or {}).get("ticket_id") == "L-0007", started)
        progressed = workers.append("e2e-L-0007", "progress", {"note": "e2e event"})
        check("progress 事件进入对应 worker 账本",
              progressed.get("event_count") == 3, progressed)
        stopped = workers.stop("e2e-L-0007", "e2e 收尾")
        workers.append("e2e-L-0007", "worker.stopped", {"reason": "e2e 已收尾"})
        resumed = workers.resume("e2e-L-0007", "e2e 恢复",
                                 launch=lambda: "session-e2e-L-0007-resume")
        check("worker stop/resume 状态机可审计",
              stopped.get("state") == "stopping"
              and resumed.get("state") == "running"
              and resumed.get("generation") == 2, (stopped, resumed))
        handed = workers.handback("e2e-L-0007", "e2e 结束", target="route")
        check("worker 终态可归档", handed.get("state") == "handed_back", handed)

        # 6. wake/resume：到期只投递到唯一入口一次，重复触发不打扰。
        wake = load_module("wake_resume_under_e2e", "wake_resume.py")
        service = wake.WakeResume(root / "runtime")
        wake_spec = {
            "kind": "wake",
            "request_id": "e2e-L-0007-wake",
            "ticket_id": "L-0007",
            "due_at": 1000.0,
            "text": "到点继续处理 worker 可审计性",
            "reason": "用户设置了提醒",
            "authorization": "explicit",
        }
        scheduled = service.schedule(wake_spec)
        check("唤醒请求排程成功", scheduled.get("state") == "scheduled", scheduled)
        check("未到点不唤醒", service.due(now=999.0) == [], service.due(now=999.0))
        delivered = []

        def deliver(spec, _current):
            delivered.append(spec["request_id"])
            write_jsonl(tasks / "inbox.jsonl", read_jsonl(tasks / "inbox.jsonl") + [{
                "ts": "2026-11-19 12:10:00",
                "text": wake.delivery_text(spec),
                "files": [],
                "kind": "wake",
            }])
            return "inbox#e2e-1"

        fired = service.fire("e2e-L-0007-wake", deliver, now=1000.0)
        check("到期唤醒投递到唯一入口",
              fired.get("state") == "delivered" and delivered == ["e2e-L-0007-wake"],
              (fired, delivered))
        inbox_after_wake = read_jsonl(tasks / "inbox.jsonl")
        check("投递文案带幂等 marker 与原工单",
              any("[wake-request:e2e-L-0007-wake]" in str(r.get("text") or "")
                  and "L-0007" in str(r.get("text") or "")
                  for r in inbox_after_wake), inbox_after_wake)
        try:
            service.fire("e2e-L-0007-wake", deliver, now=2000.0)
        except wake.ConflictError:
            check("重复 fire 被拒绝", True)
        else:
            raise AssertionError("重复 fire 没有被拒绝")
        check("重复 fire 没有再次投递", delivered == ["e2e-L-0007-wake"], delivered)

        # 7. 关闭：必须带命令、cwd、退出码；发布输入不自动关闭。
        evidence = ("python3 ops/test-e2e-ten-real-inputs.py cwd=%s exit 0"
                    % str(REPO))
        closed = run_truthctl(
            {**env, "KG_TRUTH_EVIDENCE": evidence},
            "close", "L-0005", "十条输入端到端门禁已跑通")
        check("truthctl close 带证据成功", closed.returncode == 0,
              closed.stdout + closed.stderr)
        after_close = latest_by_id(read_jsonl(truth / "ledger.jsonl"))
        check("关闭项带命令和退出码",
              after_close["L-0005"].get("status") == "done"
              and "exit 0" in str(after_close["L-0005"].get("evidence") or ""),
              after_close["L-0005"])
        check("关闭同时写入进展台账",
              any("关闭 L-0005" in str(r.get("title") or "")
                  for r in read_jsonl(truth / "progress.jsonl")),
              read_jsonl(truth / "progress.jsonl"))
        check("发布动作没有被自动关闭",
              after_close["L-0010"].get("status") != "done", after_close["L-0010"])
        closed_paths = pm.compute(repo, runtime={"blocked": {"exec_waiting": []}})
        closed_by_text = {p.get("source_text"): p for p in closed_paths.get("paths") or []}
        check("PM 只认带证据的关闭",
              closed_by_text["unknown 不能写成 0、无或正常"]["close"]["status"] == "closed"
              and closed_by_text["发布动作必须由用户拍板"]["close"]["status"] == "open",
              (closed_by_text["unknown 不能写成 0、无或正常"]["close"],
               closed_by_text["发布动作必须由用户拍板"]["close"]))

        # 8. 面板与合同：用户可见入口、职责边界与发布拍板口不能漂移。
        panel = (REPO / "plugin" / "ui" / "panel.html").read_text(encoding="utf-8")
        check("首屏区分三类入口",
              all(word in panel for word in (
                  "id=\"entryMap\"", "主入口", "当前 Codex 输入框", "万法归一",
                  "执行入口", "后台任务窗口", "兜底入口", "高级区输入框")), "entryMap")
        check("职责次入口都在面板里",
              all(word in panel for word in (
                  "id=\"pmBriefingFold\"", "PM 简报", "id=\"contextArchiveFold\"",
                  "上下文档案", "验收标准候选", "执行记录", "运行状态", "关系层")),
              "secondary entries")
        contract = json.loads((REPO / "docs" / "steward_contract.json").read_text(encoding="utf-8"))
        check("人的拍板边界仍只有三类",
              set(contract.get("human_decision_types") or [])
              == {"product_direction", "delete_data", "release"},
              contract.get("human_decision_types"))
        plan = json.loads((REPO / "docs" / "closure_plan.json").read_text(encoding="utf-8"))
        check("施工计划明确不含发布动作",
              "不含发布动作本身" in str(plan.get("goal") or ""), plan.get("goal"))

    print("e2e-ten-real-inputs: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
