#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate the product's explicit research adoption strategy."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
STRATEGY_PATH = ROOT / "docs" / "research" / "adoption-strategy.json"
CHOICES = {"adopt", "adapt", "build", "defer", "reject"}


class StrategyError(ValueError):
    pass


def load(path=None):
    return json.loads(Path(path or STRATEGY_PATH).read_text(encoding="utf-8"))


def validate(strategy):
    if not isinstance(strategy, dict):
        raise StrategyError("采用策略必须是 JSON object")
    if strategy.get("schema_version") != "1":
        raise StrategyError("不支持的 schema_version")
    policy = strategy.get("runtime_dependency_policy")
    if not isinstance(policy, dict) or policy.get("external_tools_required") is not False:
        raise StrategyError("外部竞品不得成为运行时前置依赖")
    decisions = strategy.get("decisions")
    if not isinstance(decisions, list) or len(decisions) < 6:
        raise StrategyError("decisions 不能为空且至少覆盖主要调研对象")
    ids, sources = set(), set()
    for row in decisions:
        if not isinstance(row, dict):
            raise StrategyError("decision 必须是 object")
        for key in ("id", "source", "decision", "rationale"):
            if not str(row.get(key) or "").strip():
                raise StrategyError("decision 缺少字段: %s" % key)
        if not isinstance(row.get("landed_in"), list):
            raise StrategyError("decision.landed_in 必须是数组: %s" % row["id"])
        if row["id"] in ids or row["source"] in sources:
            raise StrategyError("decision id/source 不得重复")
        ids.add(row["id"])
        sources.add(row["source"])
        if row["decision"] not in CHOICES:
            raise StrategyError("非法采用决策: %s" % row["decision"])
        if row["decision"] in {"adopt", "adapt", "build"} and not row.get("landed_in"):
            raise StrategyError("采用类决策必须有落点: %s" % row["id"])
        if row.get("runtime_dependency") is True and row["source"] not in {
                "Codex CLI / App Server / Skills / Plugins"}:
            raise StrategyError("外部竞品不得声明为运行时依赖: %s" % row["source"])
    expected = {
        "Oh My Codex",
        "DeepSeek Harness Super-agent",
        "Lauren Tan 合并方法论",
        "@openai/apps-sdk-ui 与 MCP Apps",
        "Codex 桌面面板 app.asar",
    }
    missing_sources = sorted(expected - sources)
    if missing_sources:
        raise StrategyError("缺少必要调研对象的采用决策: " + ", ".join(missing_sources))
    landings = set(strategy.get("required_landings") or [])
    for path in landings:
        if not (ROOT / path).exists():
            raise StrategyError("采用策略落点不存在: %s" % path)
    return True


def validate_repo(root=None):
    root = Path(root or ROOT)
    validate(load(root / "docs" / "research" / "adoption-strategy.json"))
    contract = (root / "docs" / "PRODUCT_CONTRACT.md").read_text(encoding="utf-8")
    if "adoption-strategy.json" not in contract:
        raise StrategyError("PRODUCT_CONTRACT 没有引用采用策略")
    return True


def main(argv):
    try:
        if len(argv) > 1 and argv[1] == "--repo":
            validate_repo(Path(argv[2]))
        else:
            validate_repo(ROOT)
    except (OSError, ValueError, StrategyError) as exc:
        print("research-adoption: FAIL: %s" % exc, file=sys.stderr)
        return 1
    print("research-adoption: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
