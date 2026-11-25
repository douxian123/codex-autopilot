#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：`kgctl gate` 必须看见分支相对 main 的真实 UI 改动。

为什么需要这条（2026-11-24 实测）：
  `cmd_gate()` 旧实现调用 `_run_verify_gates(wt)` 时不传基准，
  `_run_verify_gates()` 便退化成 `git diff HEAD HEAD`。
  一个改了 `plugin/ui/panel.html` 却没人签收体验证据的分支，
  `gate` 会给绿灯并写“可以直接 land”；真正 `land` 用 main 当基准，
  同一条分支马上被体验契约门禁拦下。预检绿灯比没有预检更危险，
  因为它诱导心跳拍直接 land，并把假绿证据写进验收台账。

本门禁用真实 git 仓库 + 临时工作树复现这个差异：
  1. 静态确认 `cmd_gate()` 不再无基准调用 `_run_verify_gates()`；
  2. 动态确认 `_run_verify_gates(wt, base)` 能识别 main..branch 的 UI 改动；
  3. 动态确认无基准调用看不到同一改动（锁住根因，防止测试写成永远真）。
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
ROOT = OPS.parent
sys.path.insert(0, str(OPS))


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        raise AssertionError(f"{label} {detail}")


def run(cmd, cwd, env=None, check_rc=True):
    p = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True,
                       text=True, errors="replace")
    if check_rc and p.returncode != 0:
        raise AssertionError(f"{' '.join(cmd)} -> {p.returncode}: {p.stderr[-800:]}")
    return p


def git(repo, *args):
    return run(["git", "-C", str(repo), *args], repo).stdout.strip()


spec = importlib.util.spec_from_file_location("kgctl_gate_base_probe", OPS / "kgctl.py")
kg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kg)

src = (OPS / "kgctl.py").read_text(encoding="utf-8")
start = src.index("def cmd_gate(")
end = src.index("\ndef ", start + 1)
body = src[start:end]
check("cmd_gate 不再无基准调用 _run_verify_gates",
      "_run_verify_gates(wt)" not in body, body[-500:])
check("cmd_gate 用 merge-base 计算 diff 基准",
      "merge-base" in body and "_run_verify_gates(wt, base)" in body, body[-500:])

with tempfile.TemporaryDirectory(prefix="gate-experience-base-") as tmp:
    repo = Path(tmp) / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "gate@example.invalid")
    git(repo, "config", "user.name", "Gate Test")
    (repo / "docs").mkdir()
    (repo / "docs" / "experience").mkdir()
    (repo / "ops").mkdir()
    contract = {
        "schema_version": "1",
        "experience_required": True,
        "scope": ["visual_hierarchy", "interaction_states"],
        "primary_task": "让用户看懂当前工作与下一步",
        "attention_order": ["当前工作", "下一步"],
        "states": [{"name": "default", "applicable": True, "evidence_required": True}],
        "surfaces": ["host_panel"],
        "accessibility": {
            "wcag_level": "AA-applicable-checks",
            "keyboard_required": True,
            "contrast_required": True,
            "non_color_status_required": True,
        },
        "machine_checks": ["no-overflow"],
        "human_checks": ["attention-order"],
        "exceptions": [],
        "evidence_policy": {
            "real_content_required": True,
            "real_state_required": True,
            "screenshots_required": True,
        },
    }
    (repo / "docs" / "experience" / "experience-contract.json").write_text(
        json.dumps(contract, ensure_ascii=False), encoding="utf-8")
    (repo / "ops" / "central.config.json").write_text(
        json.dumps({"verify": [], "verify_by_change": []}), encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    main_commit = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", "feature/ui")
    (repo / "plugin").mkdir()
    (repo / "plugin" / "ui").mkdir()
    (repo / "plugin" / "ui" / "panel.html").write_text("<h1>changed</h1>\n",
                                                       encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "ui change")

    base = git(repo, "merge-base", main_commit, "feature/ui")
    with_base = kg._run_verify_gates(repo, base)
    without_base = kg._run_verify_gates(repo, "")

    check("带 main 基准时体验契约门禁识别 UI 改动并失败",
          with_base[0] is False and "experience-contract" in with_base[1][0]["name"],
          with_base[2][:500])
    check("无基准时体验契约门禁误判为无需验证（根因仍在）",
          without_base[0] is True,
          without_base[2][:500])

print("gate experience base unit: PASS（gate 与 land 使用同一体验改动基准）")
