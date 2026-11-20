#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：全局待路由箱必须可复核、可指派、可去重，且绝不猜项目。

现场问题（2026-11-20）：待路由箱里 10 条记录混着 3 个项目，自动路由猜不出，
用户也无法在面板里把「我确认这条属于知识图谱」变成一次可审计的动作；同一句
状态管家提示重复三次，还一直占着注意力。此门禁锁住：
  1. 只读列表 / 预演不写任何账本；
  2. 显式 `--go` 后只写目标项目，原始待路由记录一字节不改；
  3. 相同原文只出现一次待复核，只落一次目标账本；
  4. 已复核项不再计入面板的「等你答一句」。
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
KGCTL = OPS / "kgctl.py"
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def make_state_root(base, name):
    root = base / name
    (root / "ops" / "tasks").mkdir(parents=True)
    (root / "ops" / "truth").mkdir(parents=True)
    (root / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
    return root


def main():
    base = Path(tempfile.mkdtemp(prefix="route-inbox-test-"))
    home = base / "home"
    home.mkdir()
    env = os.environ.copy()
    env.update({
        "KG_AUTOPILOT_HOME": str(home),
        "KG_DAEMON_HOME": str(home),
        "KG_PROJECTS_REGISTRY": str(home / "projects.json"),
        "KG_ROUTING_INBOX": str(home / "routing" / "inbox.jsonl"),
        "KG_ROUTING_REVIEW": str(home / "routing" / "reviews.jsonl"),
    })
    for key in ("KG_TRUNK", "KG_INBOX_PROJECT", "KG_INBOX_CWD", "KG_INBOX_FILES"):
        env.pop(key, None)
    old_env = {k: os.environ.get(k) for k in env}
    os.environ.update(env)

    try:
        pr = load_module("project_router_route_inbox_test", OPS / "project_router.py")
        alpha = make_state_root(base, "alpha")
        beta = make_state_root(base, "beta")
        pr.register("alpha", alpha, path=env["KG_PROJECTS_REGISTRY"])
        pr.register("beta", beta, path=env["KG_PROJECTS_REGISTRY"])

        routing = Path(env["KG_ROUTING_INBOX"])
        rows = [
            {"ts": "2026-11-20 01:00:00", "text": "alpha项目要加缓存",
             "source": "user", "files": [], "kind": "bug", "routed": False},
            {"ts": "2026-11-20 01:01:00", "text": "alpha项目要加缓存",
             "source": "user", "files": [], "kind": "bug", "routed": False},
            {"ts": "2026-11-20 02:00:00", "text": "beta项目要改面板",
             "source": "user", "files": [], "kind": "visual", "routed": False},
            {"ts": "2026-11-20 03:00:00", "text": "状态管家只提复核不改配置",
             "source": "user", "files": [], "kind": "unknown", "routed": False},
        ]
        for row in rows:
            pr.stash_ambiguous(row, path=routing)

        pending = pr.pending_routing_records(routing)
        check("重复原文只出现一条待复核", len(pending) == 3, pending)
        beta_fp = pr.routing_fingerprint(rows[2])
        alpha_fp = pr.routing_fingerprint(rows[0])

        def run(*args, repo=alpha):
            child = os.environ.copy()
            child.update({
                "KG_REPO": str(repo),
                "KG_TRUNK": str(base / "alpha-trunk"),
            })
            return subprocess.run([sys.executable, str(KGCTL), *args],
                                  cwd=str(OPS.parent), env=child,
                                  capture_output=True, text=True)

        original = routing.read_text(encoding="utf-8")
        reviews = Path(env["KG_ROUTING_REVIEW"])
        out = run("route-inbox")
        check("只读列表退出码为 0", out.returncode == 0, out.stderr)
        check("只读列表显示三条待复核", "3 条待复核" in out.stdout, out.stdout)
        check("只读列表不写原始箱", routing.read_text(encoding="utf-8") == original)
        check("只读列表不建复核台账", not reviews.exists())
        check("只读列表不写目标账本",
              (alpha / "ops" / "tasks" / "inbox.jsonl").read_text() == ""
              and (beta / "ops" / "tasks" / "inbox.jsonl").read_text() == "")

        out = run("route-inbox", "--fingerprint", beta_fp,
                  "--project", "beta", "--reason", "原话点名 beta")
        check("预演退出码为 0", out.returncode == 0, out.stderr)
        check("预演明确说未写账本", "未写任何账本" in out.stdout, out.stdout)
        check("预演不写任何账本",
              routing.read_text(encoding="utf-8") == original
              and not reviews.exists()
              and (beta / "ops" / "tasks" / "inbox.jsonl").read_text() == "")

        out = run("route-inbox", "--fingerprint", beta_fp,
                  "--project", "beta", "--reason", "原话点名 beta", "--go")
        check("显式指派退出码为 0", out.returncode == 0, out.stderr)
        beta_rows = [json.loads(x) for x in
                     (beta / "ops" / "tasks" / "inbox.jsonl").read_text(
                         encoding="utf-8").splitlines() if x.strip()]
        check("指派的原文写进目标项目", len(beta_rows) == 1
              and beta_rows[0].get("project") == "beta"
              and beta_rows[0].get("text") == rows[2]["text"], beta_rows)
        check("指派不写当前绑定项目",
              (alpha / "ops" / "tasks" / "inbox.jsonl").read_text() == "")
        check("原始待路由记录不回写、不删除",
              routing.read_text(encoding="utf-8") == original)
        review_rows = [json.loads(x) for x in reviews.read_text(
            encoding="utf-8").splitlines() if x.strip()]
        check("复核凭证可审计", len(review_rows) == 1
              and review_rows[0].get("source_fingerprint") == beta_fp
              and review_rows[0].get("project") == "beta"
              and "原话点名 beta" in review_rows[0].get("reason", ""), review_rows)
        check("已指派项不再待复核", len(pr.pending_routing_records(routing)) == 2)

        out = run("route-inbox", "--fingerprint", alpha_fp,
                  "--project", "alpha", "--go")
        check("重复诉求指派退出码为 0", out.returncode == 0, out.stderr)
        alpha_rows = [json.loads(x) for x in
                      (alpha / "ops" / "tasks" / "inbox.jsonl").read_text(
                          encoding="utf-8").splitlines() if x.strip()]
        check("重复诉求只落一次目标账本", len(alpha_rows) == 1, alpha_rows)
        check("复核后只剩真正未决项", len(pr.pending_routing_records(routing)) == 1)

        central = load_module("central_mcp_route_inbox_test", OPS / "central_mcp.py")
        check("面板等你答一句只剩未复核项",
              central._answer_needed_count() == 1,
              central._answer_needed_count())
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(base, ignore_errors=True)

    print()
    if failures:
        print(f"FAILED {len(failures)} 项：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("selftest ok: 全局待路由箱只显式指派，原始记录不回写，重复项不重复落账。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
