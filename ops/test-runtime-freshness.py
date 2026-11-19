#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行态新鲜度门禁：源码 / 已装 / 运行版本不一致必须被看见，且只能给只读建议。"""

from __future__ import annotations

import sys
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))

import runtime_freshness  # noqa: E402


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        raise AssertionError(f"{label}: {detail}")


def binding(installed="0.6.0-preview.103", running="0.6.0-preview.103",
            installed_commit="a" * 40, running_commit="a" * 40,
            problems=None, warnings=None, status="ok"):
    return {
        "status": status,
        "problems": problems or [],
        "warnings": warnings or [],
        "versions": {
            "install_state": installed,
            "marketplace": installed,
            "running": running,
            "installed_commit": installed_commit,
            "running_commit": running_commit,
        },
    }


def daemon(script="codex-autopilot/releases/0.6.0-preview.103/ops/central_daemon.py",
           commit="a" * 40, live=True, repo_matches=True, installed=True,
           bound_repo="/tmp/current", target_repo="/tmp/current"):
    return {"installed": installed, "script": script, "live": live,
            "source_commit": commit, "repo_matches": repo_matches,
            "bound_repo": bound_repo, "target_repo": target_repo}


def main():
    same = runtime_freshness.evaluate(binding(), source_commit="a" * 40,
                                      daemon=daemon())
    check("同一代源码、已装、运行代码为 ok", same["level"] == "ok", same)
    check("ok 不生成动作", same["action"] == "none", same)

    stale_installed = runtime_freshness.evaluate(binding(), source_commit="b" * 40,
                                                 daemon=daemon())
    check("已装落后主干源码抬 attention", stale_installed["level"] == "attention",
          stale_installed)
    check("落后源码只建议受控换代",
          stale_installed["action"] == "recommend_stable_upgrade", stale_installed)

    stale_running = runtime_freshness.evaluate(
        binding(running="0.6.0-preview.102", running_commit="b" * 40),
        source_commit="a" * 40, daemon=daemon())
    check("运行代码落后已装版本抬 attention", stale_running["level"] == "attention",
          stale_running)
    check("落后运行代码只建议受控重载",
          stale_running["action"] == "recommend_stable_reload", stale_running)

    broken = runtime_freshness.evaluate(
        binding(problems=["marketplace 指向别处"]), source_commit="a" * 40,
        daemon=daemon())
    check("安装绑定不一致抬 attention", broken["level"] == "attention", broken)

    unknown = runtime_freshness.evaluate(
        binding(installed_commit=None, running_commit=None), source_commit="a" * 40,
        daemon=daemon())
    check("commit 判据不足写 unknown", unknown["level"] == "unknown", unknown)
    check("unknown 不冒充正常", "取不到" in unknown["headline"], unknown)
    missing_running = runtime_freshness.evaluate(
        binding(running_commit=None), source_commit="b" * 40, daemon=daemon())
    check("已装落后源码但运行 commit 缺失时不报正常",
          missing_running["level"] == "unknown", missing_running)

    dev = runtime_freshness.evaluate(binding(installed=None, running=None, status="skip"),
                                     daemon=daemon(installed=None, script=None, live=None))
    check("开发检出没有安装锚点写 unknown", dev["level"] == "unknown", dev)

    daemon_stale = runtime_freshness.evaluate(
        binding(), source_commit="a" * 40,
        daemon=daemon(script="codex-autopilot/releases/0.6.0-preview.102/ops/central_daemon.py"))
    check("daemon plist 指旧版本抬 attention", daemon_stale["level"] == "attention",
          daemon_stale)
    check("daemon 旧版本提示受控重装", "daemon 指向" in str(daemon_stale["reasons"]),
          daemon_stale)

    daemon_commit_stale = runtime_freshness.evaluate(
        binding(), source_commit="a" * 40, daemon=daemon(commit="b" * 40))
    check("daemon 源码 commit 落后抬 attention",
          daemon_commit_stale["level"] == "attention", daemon_commit_stale)

    daemon_wrong_repo = runtime_freshness.evaluate(
        binding(), source_commit="a" * 40,
        daemon=daemon(repo_matches=False, bound_repo="/tmp/other"))
    check("daemon 服务别的账本抬 attention",
          daemon_wrong_repo["level"] == "attention", daemon_wrong_repo)

    daemon_unknown = runtime_freshness.evaluate(
        binding(), source_commit="a" * 40,
        daemon=daemon(installed=None, script=None, live=None, commit=None))
    check("daemon 判据不足写 unknown", daemon_unknown["level"] == "unknown",
          daemon_unknown)
    check("daemon unknown 不冒充正常", "daemon 代际信息不完整" in daemon_unknown["headline"],
          daemon_unknown)

    for label, result in (("ok", same), ("stale_installed", stale_installed),
                          ("daemon_stale", daemon_stale),
                          ("daemon_commit_stale", daemon_commit_stale),
                          ("daemon_wrong_repo", daemon_wrong_repo),
                          ("daemon_unknown", daemon_unknown),
                          ("stale_running", stale_running), ("broken", broken),
                          ("unknown", unknown), ("missing_running", missing_running),
                          ("dev", dev)):
        for forbidden in ("restart", "upgrade_code", "kill", "patch", "config_change"):
            check(f"{label} 输出不含执行字段 {forbidden}",
                  forbidden not in result, result)

    print("runtime_freshness: 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
