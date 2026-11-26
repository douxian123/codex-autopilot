#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PM 管家：把诉求分成研究等级，并把研究结论变成执行闸门。"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

import research

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


DECISIONS = Path("ops") / "truth" / "decisions.jsonl"
LEVELS = {"L0", "L1", "L2", "L3"}
HIGH_RISK = {"high", "critical"}


def _repo(repo=None) -> Path:
    return Path(repo or os.environ.get("KG_REPO") or Path.cwd()).expanduser().resolve()


def classify(text, kind="unknown", files=None) -> dict:
    text = " ".join(str(text or "").split())
    low = text.lower()
    files = [str(x) for x in (files or [])]
    risk = "low"
    level = "L0"
    reasons = []
    if any(x in low for x in ("发布", "生产", "推送", "删除", "迁移", "权限", "密钥", "支付", "重启")):
        risk, level = "critical", "L3"
        reasons.append("涉及发布、数据、权限或用户进程")
    elif kind in {"arch", "gate"} or any(x in low for x in (
            "架构", "基础设施", "插件", "agent", "多 agent", "路由", "合并", "worktree", "上下文")):
        risk, level = "high", "L2"
        reasons.append("可能引入新的工程能力或改变协作边界")
    elif kind in {"ux", "visual"} or any(x in low for x in ("新功能", "新增", "流程", "面板", "入口", "方案")):
        risk, level = "medium", "L2"
        reasons.append("用户可见行为或产品流程发生变化")
    elif kind in {"bug", "gate"} or any(x in low for x in ("bug", "报错", "修复", "拼写", "断言")):
        level = "L1"
        reasons.append("已有能力上的局部修复")
    else:
        reasons.append("未识别出需要外部研究的产品级变化")
    if files and any("ops/" in f or "plugin/" in f for f in files) and level == "L0":
        level, risk = "L1", "low"
        reasons.append("涉及自动挡控制面文件")
    return {
        "research_level": level,
        "risk_level": risk,
        "needs_research": level in {"L2", "L3"},
        "reasons": reasons,
    }


@contextlib.contextmanager
def _decision_lock(repo: Path):
    lock = repo / "ops" / "truth" / ".truthctl.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
        yield
        return
    with lock.open("a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _read_decisions(repo: Path) -> list:
    target = repo / DECISIONS
    if not target.exists():
        return []
    result = []
    for line in target.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            result.append(row)
    return result


def _next_decision_id(repo: Path) -> str:
    nums = []
    for row in _read_decisions(repo):
        match = re.match(r"^D-(\d+)$", str(row.get("id") or ""))
        if match:
            nums.append(int(match.group(1)))
    return "D-%04d" % (max(nums or [0]) + 1)


def _append_decision(repo: Path, fields: dict) -> dict:
    target = repo / DECISIONS
    target.parent.mkdir(parents=True, exist_ok=True)
    with _decision_lock(repo):
        row = {"id": _next_decision_id(repo), "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
               "type": "decision", "source": "pm"}
        row.update(fields)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row


def recommendation(repo=None, research_id=None) -> dict:
    repo = _repo(repo)
    item = research.latest(repo).get(str(research_id)) if research_id else None
    if not item:
        return {"status": "unknown", "reason": "研究请求不存在", "approval_mode": "user_required"}
    evidence = item.get("evidence") or []
    options = item.get("options") or []
    if not evidence:
        return {"status": "awaiting_external_research", "choice": None,
                "approval_mode": "user_required", "reason": "还没有研究证据"}
    selected = next((x for x in options if x.get("selected")), None)
    selected = selected or (options[0] if len(options) == 1 else None)
    if not selected:
        return {"status": "decision_needed", "choice": None,
                "approval_mode": "user_required", "reason": "存在研究证据，但没有唯一候选方案"}
    choice = selected.get("disposition")
    license_unknown = (choice == "adopt" and
                       selected.get("license_status") in {None, "", "unknown", "NOASSERTION"})
    level = item.get("research_level") or "L2"
    approval = "user_required" if level == "L3" or license_unknown or item.get("risk_level") in HIGH_RISK else "automatic"
    status = "awaiting_approval" if approval == "user_required" else "decision_ready"
    reason = selected.get("rationale") or "由 PM 研究候选方案得出"
    return {"status": status, "choice": choice, "selected_option": selected,
            "approval_mode": approval, "reason": reason,
            "evidence_count": len(evidence), "option_count": len(options)}


def _approved_decision(repo: Path, research_id: str):
    decisions = [x for x in _read_decisions(repo)
                 if x.get("research_id") == str(research_id)]
    latest = decisions[-1] if decisions else None
    return latest if latest and latest.get("status") == "approved" else None


def decide(repo=None, *, research_id, choice, selected_option="", why="",
           alternatives=None, approved=False, source="pm") -> dict:
    choice = str(choice or "").lower()
    if choice not in research.CHOICES:
        raise ValueError("choice 必须是 %s" % ", ".join(sorted(research.CHOICES)))
    repo = _repo(repo)
    item = research.latest(repo).get(str(research_id))
    if not item:
        raise ValueError("研究请求不存在：%s" % research_id)
    auto = recommendation(repo, research_id)
    if not (item.get("evidence") or []) and choice in {"adopt", "adapt", "build"}:
        row = _append_decision(repo, {
            "title": item.get("question"), "decision": choice, "choice": choice,
            "why": why or "等待外部研究证据", "alternatives": [str(x) for x in (alternatives or [])],
            "status": "awaiting_external_research", "intent_id": item.get("intent_id"),
            "research_id": research_id, "selected_option": selected_option or None,
            "approval_mode": "user_required", "approved": False, "source": source,
        })
        research.update_status(repo, research_id=research_id,
                               status="awaiting_external_research",
                               note="没有证据，不能进入施工", source=source)
        return {"status": "awaiting_external_research", "decision": row,
                "recommendation": auto}
    needs_approval = item.get("research_level") == "L3" or item.get("risk_level") in HIGH_RISK
    if choice == "adopt":
        opt = next((x for x in item.get("options") or [] if x.get("name") == selected_option), None)
        if opt and opt.get("license_status") in {None, "", "unknown", "NOASSERTION"}:
            needs_approval = True
    status = "approved" if (approved or not needs_approval) else "awaiting_approval"
    row = _append_decision(repo, {
        "title": item.get("question"),
        "decision": choice,
        "choice": choice,
        "why": why or auto.get("reason") or "",
        "alternatives": [str(x) for x in (alternatives or [])],
        "status": status,
        "intent_id": item.get("intent_id"),
        "research_id": research_id,
        "selected_option": selected_option or None,
        "approval_mode": "user_required" if needs_approval else "automatic",
        "approved": bool(status == "approved"),
        "source": source,
    })
    research.update_status(repo, research_id=research_id,
                           status=status, note="产品决策 %s" % choice, source=source)
    return {"status": status, "decision": row, "recommendation": auto}


def execution_gate(repo=None, *, intent_id=None, research_id=None) -> dict:
    repo = _repo(repo)
    item = research.latest(repo).get(str(research_id)) if research_id else None
    if not item and intent_id:
        item = next((x for x in research.latest(repo).values() if x.get("intent_id") == intent_id), None)
    if not item:
        return {"status": "pass", "reason": "L0/L1 没有 PM 研究门", "research": None}
    decisions = [x for x in _read_decisions(repo) if x.get("research_id") == item.get("research_id")]
    latest_decision = decisions[-1] if decisions else None
    approved = latest_decision if latest_decision and latest_decision.get("status") == "approved" else None
    level = item.get("research_level") or "L2"
    if level in {"L2", "L3"} and (item.get("status") != "approved" or not approved):
        return {"status": "blocked", "reason": "没有已批准的产品决策，不能进入施工或合流",
                "research": item, "decision": decisions[-1] if decisions else None}
    return {"status": "pass", "reason": "产品决策已批准", "research": item, "decision": approved}


def execution_gate_for_branch(repo=None, *, branch) -> dict:
    """检查一个施工泳道挂着的全部 PM 研究门。

    一个分支可能承接多条输入，所以不能只查第一条。没有 ``pm_research`` 的历史
    工作项保持兼容并放行，新的 L2/L3 项则必须有明确 research_id 和 approved 决策。
    """
    repo = _repo(repo)
    ledger = repo / "ops" / "truth" / "ledger.jsonl"
    if not ledger.exists():
        return {"status": "unknown", "reason": "问题台账取不到，不能判断 PM 决策门", "items": []}
    latest = {}
    for line in ledger.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("id"):
            latest[row["id"]] = row
    # 路由阶段使用固定泳道（arch / design / bug），施工阶段使用真实分支名。
    # 不能把两者当成同一个字段，否则新 L2/L3 工作项会在 land 时静默漏检。
    # 优先使用显式 branch 绑定；没有绑定时，用分支相对 main 的文件占地做保守关联。
    # 多条任务命中同一分支时全部检查，任何一条未批准都 fail-closed。
    explicit = []
    for row in latest.values():
        pm = row.get("pm_research") or {}
        if (row.get("lane") == branch or row.get("branch") == branch
                or pm.get("branch") == branch):
            explicit.append(row)

    changed_files = set()
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "diff", "--name-only", f"{branch}...HEAD"],
            capture_output=True, text=True, check=False)
        if result.returncode == 0:
            changed_files = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        if not changed_files:
            result = subprocess.run(
                ["git", "-C", str(repo), "diff", "--name-only", "main...%s" % branch],
                capture_output=True, text=True, check=False)
            if result.returncode == 0:
                changed_files = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    except OSError:
        changed_files = set()

    associated = list(explicit)
    if not explicit and changed_files:
        for row in latest.values():
            pm = row.get("pm_research") or {}
            if not pm.get("research_id"):
                continue
            owned = {str(item) for item in (row.get("files") or []) if str(item).strip()}
            if owned & changed_files:
                associated.append(row)

    checks = []
    for row in associated:
        pm = row.get("pm_research") or {}
        rid = pm.get("research_id")
        if not rid:
            continue
        check = execution_gate(repo, research_id=rid)
        checks.append({"ledger_id": row.get("id"), "title": row.get("title"),
                       "association": ("explicit" if row in explicit else "files"), **check})
    blocked = [x for x in checks if x.get("status") != "pass"]
    if blocked:
        return {"status": "blocked", "reason": "施工泳道存在未批准的 PM 产品决策",
                "items": checks, "blocked": blocked, "changed_files": sorted(changed_files)}
    return {"status": "pass", "reason": "施工分支关联的 PM 决策门均已通过",
            "items": checks, "changed_files": sorted(changed_files)}


def ensure_for_ledger(repo=None, *, ledger_id, title, kind="unknown", files=None,
                      project_id=None, source="intake") -> dict:
    framing = classify(title, kind, files)
    if not framing["needs_research"]:
        return {"status": "not_required", "framing": framing}
    request = research.create_request(
        repo, intent_id=ledger_id, question=title, scope=files,
        research_level=framing["research_level"], risk_level=framing["risk_level"],
        project_id=project_id, source=source)
    return {"status": request["status"], "framing": framing, **request}


def brief(repo=None) -> dict:
    data = research.summary(repo)
    for item in data.get("items") or []:
        approved = _approved_decision(_repo(repo), item.get("research_id"))
        if approved:
            item["decision_status"] = "approved"
            item["choice"] = approved.get("choice") or approved.get("decision")
            item["approval_mode"] = approved.get("approval_mode")
            continue
        rec = recommendation(repo, item.get("research_id"))
        item["decision_status"] = rec.get("status")
        item["choice"] = rec.get("choice")
        item["approval_mode"] = rec.get("approval_mode")
    return data
