#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目宪法页门禁：只投影确认过的约束，源台账坏时拒绝覆盖，synthesize 同步刷新。"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
ROOT = OPS.parent
SPEC = importlib.util.spec_from_file_location(
    "project_constitution_under_test", OPS / "project_constitution.py")
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"  ok   {label}")


def write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main():
    with tempfile.TemporaryDirectory(prefix="project-constitution-") as tmp:
        repo = Path(tmp) / "repo"
        truth = repo / "ops" / "truth"
        truth.mkdir(parents=True)
        write_text(repo / "docs" / "PROJECT_GLOBAL_RULES.md",
                   "# 项目全局规则\n\n一个任务只确认一次位置；禁止先改后搬。\n")
        write_text(repo / "docs" / "experience" / "GENERAL_EXPERIENCE_CONSTITUTION.md",
                   "# 通用体验宪法\n\n语义先于样式。\n")

        write_jsonl(truth / "constraints.jsonl", [
            {"id": "C-0001", "status": "active", "type": "constraint",
             "statement": "全局必须保留命令原文", "scope_class": "global",
             "enforceable": "gate", "cmd": "python3 ops/selftest.py"},
            {"id": "C-0002", "status": "retired", "type": "constraint",
             "statement": "旧约束不该继续生效"},
        ])
        write_jsonl(truth / "constraint_candidates.jsonl", [
            {"id": "CC-0001", "status": "proposed", "text": "从现在起所有项目都不许删数据",
             "kind": "taboo", "priority": "high", "scope_class": "global",
             "signals": ["禁止"]},
            {"id": "CC-0002", "status": "proposed", "text": "应该优化一句文案",
             "priority": "normal", "scope_class": "project"},
            {"id": "CC-0003", "status": "promoted", "text": "已经点头但还没落账",
             "constraint_id": "C-0001"},
            {"id": "CC-0004", "status": "dismissed", "text": "这条不算",
             "reason": "用户说不是那个意思"},
        ])
        write_jsonl(truth / "judgments.jsonl", [
            {"id": "J-0001", "status": "active", "scope": ["process"],
             "statement": "用户说过的规范先候选，确认后才生效",
             "why": "自然语言判断不能直接污染门禁"},
            {"id": "J-0002", "status": "retired", "statement": "旧判断不显示"},
        ])

        data = mod.collect(repo)
        check("宪法投影可读", data.get("status") == "ok", data)
        counts = data.get("counts") or {}
        check("五类数量如实保留", counts == {"active": 1, "proposed": 2,
                                              "promoted": 1, "dismissed": 1,
                                              "judgments": 1}, counts)
        check("高优先候选排序在前",
              [r.get("id") for r in data.get("proposed") or []] == ["CC-0001", "CC-0002"],
              [r.get("id") for r in data.get("proposed") or []])
        check("停用约束与判断不混入生效区",
              "旧约束不该继续生效" not in str(data.get("constraints"))
              and "旧判断不显示" not in str(data.get("judgments")))

        text = mod.render(data, "2026-11-21 00:00:00")
        active_part = text.split("## 一、生效约束", 1)[1].split("## 二、", 1)[0]
        check("生效区只放人确认过的约束",
              "全局必须保留命令原文" in active_part
              and "从现在起所有项目都不许删数据" not in active_part)
        check("候选保留优先级、范围和原话",
              "高优先/全局/禁忌" in text and "从现在起所有项目都不许删数据" in text)
        check("已驳回保留原因且不自动复活",
              "用户说不是那个意思" in text and "这条不算" in text)
        check("判断逻辑和书面宪法引用都在",
              "用户说过的规范先候选" in text
              and "一个任务只确认一次位置" in text
              and "PROJECT_GLOBAL_RULES.md" in text
              and "docs/experience/GENERAL_EXPERIENCE_CONSTITUTION.md" in text)
        check("缺口如实列出未确认高优先项，书面文档不再缺失",
              "高优先候选仍待人确认" in text
              and "书面宪法文件不存在" not in text)

        check("首次写宪法页成功", mod.write_page(repo=str(repo), generated_at="2026-11-21 00:00:00") == 0)
        page = repo / "docs" / "PROJECT_CONSTITUTION.md"
        first = page.read_text(encoding="utf-8")
        check("宪法页写到 docs", page.exists() and "## 一、生效约束" in first)
        check("--if-changed 不重写生成时间",
              mod.write_page(repo=str(repo), if_changed=True,
                             generated_at="2099-01-01 00:00:00") == 0
              and page.read_text(encoding="utf-8") == first)

        (truth / "constraints.jsonl").write_text("{", encoding="utf-8")
        check("源台账损坏报 unknown", mod.collect(repo).get("status") == "unknown")
        check("读失败拒绝覆盖旧宪法页",
              mod.write_page(repo=str(repo)) == 2
              and page.read_text(encoding="utf-8") == first)

        # synthesize 是每轮收口的统一入口：主线页必须带 PRD 修订索引，
        # 同时刷新宪法页；PRD 台账读失败不能写成「修订 0 条」。
        synth = Path(tmp) / "synth"
        (synth / "ops" / "truth").mkdir(parents=True)
        (synth / "ops" / "truth" / "prd_revisions.jsonl").write_text("{", encoding="utf-8")
        env = {**os.environ, "KG_REPO": str(synth),
               "KG_TRUTH_ID_STATE_ROOT": str(synth / "truth-ids")}
        proc = subprocess.run(
            [sys.executable, str(OPS / "truthctl.py"), "synthesize", "--if-changed"],
            cwd=str(ROOT), env=env, capture_output=True, text=True)
        check("synthesize 成功", proc.returncode == 0, (proc.stderr or proc.stdout)[-300:])
        mainline = synth / "docs" / "PRODUCT_MAINLINE.md"
        content = mainline.read_text(encoding="utf-8") if mainline.exists() else ""
        check("产品主线带 PRD 修订索引", "## PRD 修订索引" in content)
        check("PRD 读失败不冒充 0 条",
              "PRD 修订台账取不到" in content and "修订 0 条" not in content)
        check("synthesize 同步刷新宪法页",
              (synth / "docs" / "PROJECT_CONSTITUTION.md").exists())

    print("project-constitution: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
