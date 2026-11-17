#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""outcome_score —— 独立结果评分（stdlib only，默认只读）。

它回答的不是「跑了多少命令 / 合了多少分支」，而是「从已有事实看，用户结果
有没有真的变好」。因此它不读产品自评结论，不复用发布认证，也不把所有命令
绿灯当成成功；只用原始台账做可复核的结果代理评分。

四类判据：
  1 首问闭环     closure_rate 的权威口径，取不到就 unknown；
  2 完成项证据   ledger 里 done 项是否有真实证据，标 done 无证据按未闭环；
  3 人工验收边界 acceptance 里 needs_human 项是否真的由人收口，机器项不能冒充；
  4 显式目标收口 relationship_ledger 里显式目标是否有 done 状态（弱代理，权重最低）。

这是**结果代理分**，不是用户内心结果真值；组件取不到时保持 unknown，不按 0 分
计入。有效权重覆盖不足 60% 时整体返回 unknown，不拿少量样本报一个好看的分数。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OPS_DIR = Path(__file__).resolve().parent
if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))

import closure_rate  # noqa: E402
from ledger_state import latest_ledger_by_id  # noqa: E402

DEFAULT_REPO = OPS_DIR.parent
MIN_COVERAGE = 0.60
# 北极星、完成项证据和人工验收边界不能由其他代理项替补：任一取不到，整体只能 unknown。
REQUIRED_COMPONENTS = ("closure", "evidence", "human")
WEIGHTS = {
    "closure": 35,
    "evidence": 20,
    "human": 20,
    "health": 15,
    "goals": 10,
}
GRADE_BANDS = ((80, "强"), (60, "有基础"), (40, "偏弱"), (0, "弱"))


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _truth_dir(repo: Path) -> Path:
    return Path(repo).expanduser() / "ops" / "truth"


def _component(key: str, label: str, status: str, detail: str,
               score: float | None = None, shortfall: list[str] | None = None) -> dict:
    return {
        "key": key,
        "label": label,
        "status": status,
        "score": None if score is None else round(float(score), 1),
        "weight": WEIGHTS[key],
        "detail": detail,
        "shortfall": shortfall or [],
    }


def _read_jsonl(path: Path) -> list[dict] | None:
    """返回 rows；文件不存在或 JSON 损坏返回 None，不把未知当 0。"""
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    rows: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            return None
        if isinstance(row, dict):
            rows.append(row)
        else:
            return None
    return rows


def _latest(rows: list[dict]) -> list[dict]:
    return list(latest_ledger_by_id(rows).values())


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _closure_component(repo: Path) -> dict:
    res = closure_rate.compute(str(repo))
    if res.get("status") != "ok":
        return _component("closure", "首问闭环", "unknown",
                          closure_rate.human_line(res))
    denominator = int(res.get("denominator") or 0)
    if denominator <= 0:
        return _component("closure", "首问闭环", "unknown",
                          "首问闭环没有有效分母，不能按 0% 计分")
    detail = "%s%%（%s/%s）" % (res.get("rate_pct"), res.get("numerator"), denominator)
    if denominator < 5:
        detail += "；样本小，分数只作早期信号"
    shortfall = []
    if res.get("done_without_evidence"):
        shortfall.append("标 done 无证据：%s" % "、".join(res["done_without_evidence"][:3]))
    return _component("closure", "首问闭环", "ok", detail,
                      _clamp(res.get("rate_pct") or 0), shortfall)


def _evidence_component(repo: Path) -> dict:
    path = _truth_dir(repo) / "ledger.jsonl"
    rows = _read_jsonl(path)
    if rows is None:
        return _component("evidence", "完成项证据", "unknown", "问题台账取不到或解析不了")
    rows = [r for r in _latest(rows) if r.get("type", "ledger") == "ledger"]
    done = [r for r in rows if str(r.get("status") or "") == "done"]
    if not done:
        return _component("evidence", "完成项证据", "unknown", "还没有 done 样本，不能假装已经闭环")
    missing = [str(r.get("id") or "?") for r in done
               if not str(r.get("evidence") or "").strip()]
    score = 100.0 * (len(done) - len(missing)) / len(done)
    shortfall = ["标 done 无证据：%s" % "、".join(missing[:5])] if missing else []
    return _component("evidence", "完成项证据", "ok",
                      "%s/%s 个 done 项有证据" % (len(done) - len(missing), len(done)),
                      score, shortfall)


def _human_component(repo: Path) -> dict:
    path = _truth_dir(repo) / "acceptance.jsonl"
    rows = _read_jsonl(path)
    if rows is None:
        return _component("human", "人工验收边界", "unknown", "验收台账取不到或解析不了")
    rows = [r for r in _latest(rows) if r.get("type", "acceptance") == "acceptance"]
    human = [r for r in rows if r.get("needs_human")]
    if not human:
        return _component("human", "人工验收边界", "unknown", "还没有人工验收样本")

    verified = [r for r in human if str(r.get("status") or "") == "verified"
                and str(r.get("evidence") or "").strip()]
    pending = [r for r in human if str(r.get("status") or "") in ("todo", "wip", "blocked")]
    invalid = [r for r in human if str(r.get("status") or "") == "verified"
               and not str(r.get("evidence") or "").strip()]
    total = len(verified) + len(pending) + len(invalid)
    if total <= 0:
        return _component("human", "人工验收边界", "unknown", "人工验收项状态不可判定")
    score = 100.0 * len(verified) / total
    shortfall = []
    if pending:
        shortfall.append("等待人判断 %s 项" % len(pending))
    if invalid:
        shortfall.append("标 verified 却无证据 %s 项" % len(invalid))
    return _component("human", "人工验收边界", "ok",
                      "%s/%s 项由人收口；机器项不计入" % (len(verified), total),
                      score, shortfall)


def _health_component(repo: Path) -> dict:
    try:
        import project_health
    except Exception as exc:
        return _component("health", "全局健康", "unknown", "健康模块不可用：%s" % exc)
    try:
        res = project_health.compute(str(repo), runtime={
            "auto_exec": True,
            "exec_stuck": 0,
            "land_failures": 0,
            "resolve": 0,
            "overlap_waiting": 0,
        })
    except Exception as exc:
        return _component("health", "全局健康", "unknown", "全局健康取不到：%s" % exc)
    if res.get("status") != "ok" or res.get("level") not in ("ok", "attention", "unknown"):
        return _component("health", "全局健康", "unknown",
                          "全局健康取不到：%s" % res.get("headline"))
    if res.get("level") == "unknown":
        return _component("health", "全局健康", "unknown", res.get("headline") or "取不到")
    score = 100.0 if res.get("level") == "ok" else 40.0
    shortfall = [] if res.get("level") == "ok" else [res.get("headline") or "项目健康需要注意"]
    return _component("health", "全局健康", "ok",
                      res.get("headline") or "全局健康结论", score, shortfall)


def _goals_component() -> dict:
    try:
        import relationship_ledger
    except Exception as exc:  # 模块坏了本身要上报，不能静默当没目标
        return _component("goals", "显式目标收口", "unknown", "关系账本模块不可用：%s" % exc)
    path = relationship_ledger.ledger_path()
    if not path.is_file():
        return _component("goals", "显式目标收口", "unknown", "尚未显式登记跨项目目标")
    try:
        # 已落盘的关系账本公开的是事件投影 `project`，没有 `list_goals`。
        # 直接读事件并投影，避免评分器假装有一个不存在的 API。
        goals = relationship_ledger.project(relationship_ledger._read_events(path))
    except Exception as exc:
        return _component("goals", "显式目标收口", "unknown", "关系账本读不到：%s" % exc)
    if not goals:
        return _component("goals", "显式目标收口", "unknown", "显式目标账本为空")
    done = [r for r in goals if str(r.get("status") or "") == "done"]
    waiting = [r for r in goals if str(r.get("status") or "") in
               ("proposed", "active", "waiting_human", "paused")]
    score = 100.0 * len(done) / len(goals)
    shortfall = ["仍在推进或等待 %s 项目标" % len(waiting)] if waiting else []
    return _component("goals", "显式目标收口", "ok",
                      "%s/%s 项目标状态收口（账本不含结果证据，仅作弱代理）"
                      % (len(done), len(goals)),
                      score, shortfall)


def compute(repo: Any = None) -> dict:
    """只读聚合四类结果判据；任何判据取不到都保持 unknown。"""
    repo = Path(repo or DEFAULT_REPO).expanduser()
    components = [
        _closure_component(repo),
        _evidence_component(repo),
        _human_component(repo),
        _health_component(repo),
        _goals_component(),
    ]
    known = [c for c in components if c["status"] == "ok" and c["score"] is not None]
    unknown = [c for c in components if c not in known]
    total_weight = sum(WEIGHTS.values())
    covered_weight = sum(c["weight"] for c in known)
    coverage = round(covered_weight / total_weight, 2)

    result = {
        "status": "unknown",
        "score": None,
        "grade": "未知",
        "coverage": coverage,
        "headline": "结果代理分取不到：有效判据覆盖 %s%%" % int(coverage * 100),
        "note": "基于台账的独立结果代理分；机器命令通过不等于用户结果变好，取不到不计 0 分。",
        "components": components,
        "missing": [c["label"] for c in unknown],
        "shortfalls": [],
        "generated_at": _now(),
        "sources": ["closure_rate", "ledger.jsonl", "acceptance.jsonl", "project_health", "relationship_ledger"],
    }
    missing_required = [c for c in unknown if c["key"] in REQUIRED_COMPONENTS]
    if coverage < MIN_COVERAGE or not known or missing_required:
        if missing_required:
            result["headline"] = "结果代理分取不到：北极星判据「%s」取不到" % (
                "、".join(c["label"] for c in missing_required))
        result["shortfalls"] = [c["detail"] for c in unknown]
        return result

    score = round(sum(c["score"] * c["weight"] for c in known) / covered_weight)
    grade = next(label for threshold, label in GRADE_BANDS if score >= threshold)
    shortfalls = []
    for component in components:
        for item in component["shortfall"]:
            shortfalls.append("%s：%s" % (component["label"], item))
    result.update({
        "status": "ok",
        "score": score,
        "grade": grade,
        "headline": "结果代理分 %s/100（%s；覆盖 %s%%）"
                    % (score, grade, int(coverage * 100)),
        "shortfalls": shortfalls[:6],
    })
    return result


def human_line(res: dict) -> str:
    if res.get("status") != "ok":
        return "结果代理分取不到：有效判据覆盖 %s%%" % int((res.get("coverage") or 0) * 100)
    line = res.get("headline") or "结果代理分取不到"
    if res.get("missing"):
        line += "；未覆盖：" + "、".join(res["missing"])
    return line


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="独立结果代理评分（只读）")
    parser.add_argument("--repo", default=str(DEFAULT_REPO))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = compute(args.repo)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else human_line(result))
    return 0 if result.get("status") == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
