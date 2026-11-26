#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for explicit research adoption decisions."""

import copy
import importlib.util
from pathlib import Path


OPS = Path(__file__).resolve().parent
ROOT = OPS.parent
spec = importlib.util.spec_from_file_location("research_adoption", OPS / "research_adoption.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def expect_fail(strategy, fragment):
    try:
        module.validate(strategy)
    except module.StrategyError as exc:
        assert fragment in str(exc), (str(exc), fragment)
        return
    raise AssertionError("expected failure: %s" % fragment)


def main():
    valid = module.load()
    assert module.validate(valid) is True
    assert module.validate_repo(ROOT) is True

    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'cp -R "$REPO/docs/research" "$STAGE/docs/"' in installer
    assert "docs/research/adoption-strategy.json" in installer
    assert "PRODUCT_CONTRACT.md" in installer

    external_dep = copy.deepcopy(valid)
    external_dep["runtime_dependency_policy"]["external_tools_required"] = True
    expect_fail(external_dep, "运行时前置依赖")

    bad_choice = copy.deepcopy(valid)
    bad_choice["decisions"][0]["decision"] = "maybe"
    expect_fail(bad_choice, "非法采用决策")

    no_landing = copy.deepcopy(valid)
    no_landing["decisions"][0]["landed_in"] = []
    expect_fail(no_landing, "落点")

    duplicate = copy.deepcopy(valid)
    duplicate["decisions"].append(copy.deepcopy(duplicate["decisions"][0]))
    expect_fail(duplicate, "不得重复")

    missing_source = copy.deepcopy(valid)
    missing_source["decisions"] = [row for row in missing_source["decisions"]
                                   if row["source"] != "Lauren Tan 合并方法论"]
    expect_fail(missing_source, "Lauren Tan")

    print("research adoption: PASS (valid + 5 negative cases + repo wiring + release packaging)")


if __name__ == "__main__":
    main()
