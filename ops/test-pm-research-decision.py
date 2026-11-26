#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PM 研究与产品决策门的最小真实契约回归。"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(OPS))
import pm_decision
import pm_briefing
import research


failures = []


def check(label, condition, detail=""):
    if condition:
        print("  ok   " + label)
    else:
        failures.append(label)
        print("  FAIL " + label + ("（%s）" % detail if detail else ""))


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True)


def main():
    old = os.environ.get("KG_REPO")
    with tempfile.TemporaryDirectory(prefix="autopilot-pm-research-") as tmp:
        repo = Path(tmp)
        truth = repo / "ops" / "truth"
        truth.mkdir(parents=True)
        os.environ["KG_REPO"] = str(repo)

        framing = pm_decision.classify(
            "为自动挡增加跨 Agent 项目级研究和决策流程", "arch", ["ops/central_mcp.py"])
        check("架构型新能力进入 L2 研究", framing["research_level"] == "L2", framing)
        check("研究等级不是把所有 bug 都升级", pm_decision.classify("修复一个拼写 bug", "bug")["research_level"] == "L1")
        high = pm_decision.classify("删除旧账本并发布到生产", "arch")
        check("删除/发布进入 L3", high["research_level"] == "L3" and high["risk_level"] == "critical", high)

        first = research.create_request(
            repo, intent_id="L-0001", question="选择 PM 研究决策的实现方式",
            scope=["ops/central_mcp.py"], research_level="L2", risk_level="high")
        check("创建研究请求", first["status"] == "created", first)
        second = research.create_request(
            repo, intent_id="L-0001", question="选择 PM 研究决策的实现方式",
            scope=["ops/central_mcp.py"], research_level="L2", risk_level="high")
        check("相同意图和问题不重复建请求", second["status"] == "existing", second)
        rid = first["request"]["research_id"]

        blocked = pm_decision.execution_gate(repo, research_id=rid)
        check("没有研究证据时执行闸门阻塞", blocked["status"] == "blocked", blocked)
        try:
            research.add_evidence(repo, research_id=rid,
                                  source_url="https://github.com/example/reference",
                                  source_kind="github", claims=["有可参考的编排状态模型"],
                                  license_status="MIT", maintenance_signal="active",
                                  relevance="high", confidence="medium")
            check("宿主来源可以写入证据", True)
        except Exception as exc:
            check("宿主来源可以写入证据", False, str(exc))
        research.add_option(repo, research_id=rid, name="复用现有编排能力",
                            disposition="adapt", rationale="减少重复造轮子",
                            license_status="MIT", selected=True)
        recommendation = pm_decision.recommendation(repo, rid)
        check("有证据和候选后形成推荐", recommendation["choice"] == "adapt", recommendation)
        decision = pm_decision.decide(repo, research_id=rid, choice="adapt",
                                      selected_option="复用现有编排能力", why="优先复用成熟实践")
        check("高风险决策默认等待确认", decision["status"] == "awaiting_approval", decision)
        gate = pm_decision.execution_gate(repo, research_id=rid)
        check("未批准决策不能进入施工", gate["status"] == "blocked", gate)
        approved = pm_decision.decide(repo, research_id=rid, choice="adapt",
                                      selected_option="复用现有编排能力", why="用户确认后采用",
                                      approved=True)
        check("显式批准后决策落地", approved["status"] == "approved", approved)
        gate = pm_decision.execution_gate(repo, research_id=rid)
        check("批准后执行闸门放行", gate["status"] == "pass", gate)

        rows = [json.loads(line) for line in (truth / "research.jsonl").read_text().splitlines()]
        check("研究历史采用追加式记录", len(rows) >= 5, len(rows))
        briefing = pm_briefing.compute(repo)
        check("PM 简报包含研究摘要", "research" in briefing and briefing["research"]["count"] == 1, briefing)

        # 通过现有 route 写入一个新 ledger 时，PM 研究请求会自动挂上；这里不调用
        # truthctl 的全局 CLI，避免测试污染真实项目，只验证同一接线契约的结果形状。
        routed = pm_decision.ensure_for_ledger(
            repo, ledger_id="L-ROUTE", title="为项目新增跨入口路由控制能力",
            kind="arch", files=["ops/project_router.py"], source="route-test")
        check("路由产生 L2/L3 研究请求", routed["status"] == "created", routed)
        check("研究请求绑定到 ledger intent", routed["request"].get("intent_id") == "L-ROUTE", routed)
        fast = pm_decision.ensure_for_ledger(
            repo, ledger_id="L-BUG", title="修复一个局部报错", kind="bug", files=[])
        check("局部 bug 不创建外部研究", fast["status"] == "not_required", fast)

        # land 的关联不能依赖固定泳道名：route 写入 arch，施工分支却叫 codex/...。
        # 用真实 git 分支和文件占地锁住这个闭环，防止 PM gate 看不见新 L2/L3 项。
        gate_repo = repo / "branch-gate"
        (gate_repo / "ops" / "truth").mkdir(parents=True)
        (gate_repo / "ops" / "control.py").write_text("baseline\n", encoding="utf-8")
        (gate_repo / "ops" / "truth" / "ledger.jsonl").write_text("", encoding="utf-8")
        git(gate_repo, "init", "-b", "main")
        git(gate_repo, "config", "user.email", "test@example.invalid")
        git(gate_repo, "config", "user.name", "PM test")
        git(gate_repo, "add", "ops")
        git(gate_repo, "commit", "-m", "baseline")
        git(gate_repo, "switch", "-c", "codex/pm-gate")
        (gate_repo / "ops" / "control.py").write_text("changed\n", encoding="utf-8")
        git(gate_repo, "add", "ops/control.py")
        git(gate_repo, "commit", "-m", "施工改动")
        gate_request = research.create_request(
            gate_repo, intent_id="L-GATE", question="选择控制面实现方式",
            scope=["ops/control.py"], research_level="L2", risk_level="medium")
        gate_rid = gate_request["request"]["research_id"]
        (gate_repo / "ops" / "truth" / "ledger.jsonl").write_text(
            json.dumps({"id": "L-GATE", "kind": "arch", "title": "控制面实现",
                        "lane": "arch", "files": ["ops/control.py"],
                        "pm_research": {"research_id": gate_rid}}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        branch_blocked = pm_decision.execution_gate_for_branch(gate_repo, branch="codex/pm-gate")
        check("分支 PM gate 按文件占地关联固定泳道任务",
              branch_blocked["status"] == "blocked" and
              branch_blocked["items"][0]["association"] == "files", branch_blocked)
        research.add_evidence(gate_repo, research_id=gate_rid,
                              source_url="repo:local-prior",
                              claims=["已有可复用控制面实现"], license_status="MIT",
                              maintenance_signal="active", relevance="high", confidence="high")
        research.add_option(gate_repo, research_id=gate_rid, name="适配现有控制面",
                            disposition="adapt", rationale="保留既有边界", license_status="MIT",
                            selected=True)
        research_id_decision = pm_decision.decide(
            gate_repo, research_id=gate_rid, choice="adapt",
            selected_option="适配现有控制面", why="测试批准")
        branch_pass = pm_decision.execution_gate_for_branch(gate_repo, branch="codex/pm-gate")
        check("批准后分支 PM gate 放行", research_id_decision["status"] == "approved"
              and branch_pass["status"] == "pass", branch_pass)

    if old is None:
        os.environ.pop("KG_REPO", None)
    else:
        os.environ["KG_REPO"] = old
    print()
    if failures:
        print("pm research decision 门禁：%d 项未通过" % len(failures))
        for item in failures:
            print("  - " + item)
        return len(failures)
    print("pm research decision 门禁：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
