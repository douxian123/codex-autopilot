#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：落盘门禁的配置必须来自「正在被验证的那棵树」，不是状态根。

为什么有这条（2026-11-17 本轮 land 实测）：

  `kgctl` 启动时用状态根解析出的 CFG 去跑落盘门禁。状态根是 git 主工作树
  （共享检出），而它经常被另一个 worktree 占着、甚至停在**完全不同历史**的分支上；
  那棵树里可能根本没有 `ops/central.config.json`。于是门禁退化成内置默认：

      build = npm run build, cwd = frontend

  而本仓没有 frontend，每次 land 都报

      [build] cwd 不存在：…/codex-autopilot-trunk/frontend
      验证门禁 × 失败 → 丢弃，main 不动

  合并因此永远落不了盘，而看起来像是「门禁真的失败」。真正原因不是门禁失败，是
  配置来源错了 —— 门禁描述的是「这棵树怎么验」，配置就该从这棵树读。

本门禁锁死两件事：
  1. trunk 自带 central.config.json 时，用它自己的门禁（哪怕状态根的配置不同）；
  2. trunk 缺配置时，退回状态根 CFG，不抛异常、不把老项目打挂。
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

OPS = Path(__file__).resolve().parent
failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True)


def seed(root: Path, name: str, gate_cmd: str):
    repo = root / name
    (repo / "ops" / "tasks").mkdir(parents=True)
    (repo / "ops" / "truth").mkdir(parents=True)
    (repo / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "truth" / "progress.jsonl").write_text("", encoding="utf-8")
    (repo / "ops" / "central.config.json").write_text(
        '{"build": {"cmd": "", "cwd": "."}, '
        '"verify": [{"name": "trunk-gate", "cmd": "%s", "cwd": "."}]}\n' % gate_cmd,
        encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def load_kgctl(repo, trunk):
    os.environ["KG_REPO"] = str(repo)
    os.environ["KG_TRUNK"] = str(trunk)
    spec = importlib.util.spec_from_file_location("kgctl_gate_source", OPS / "kgctl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    with tempfile.TemporaryDirectory(prefix="verify-gate-tree-") as raw:
        root = Path(raw)
        # 状态根：一个「停在别的分支、没有 central.config.json」的共享检出。
        state = root / "shared"
        (state / "ops" / "tasks").mkdir(parents=True)
        (state / "ops" / "truth").mkdir(parents=True)
        (state / "ops" / "tasks" / "inbox.jsonl").write_text("", encoding="utf-8")
        (state / "ops" / "truth" / "progress.jsonl").write_text("", encoding="utf-8")
        git(state, "init", "-q", "-b", "main")
        git(state, "config", "user.email", "t@example.com")
        git(state, "config", "user.name", "t")
        git(state, "add", "-A")
        git(state, "commit", "-q", "-m", "init")

        kg = load_kgctl(state, state)
        kg.REPO = state
        kg.MAIN = "main"
        # 状态根这份 CFG 就是内置默认：npm run build + frontend（本仓不存在）。
        kg.CFG = {"build": {"cmd": "npm run build", "cwd": "frontend"}, "verify": None}

        # trunk 自带一份可跑的真门禁，且它的 cwd=frontend 不存在 ——
        # 只要门禁读到 trunk 配置，就该跑 trunk-gate 而不是默认 build。
        trunk = seed(root, "trunk", "echo TRUNK_GATE_RAN")
        ok, results, out = kg._run_verify_gates(trunk)
        names = [r.get("name") for r in results]
        check("门禁来自 trunk 自己的配置",
              names == ["experience-contract", "trunk-gate"], f"(names={names})")
        check("trunk 门禁真的被执行", "TRUNK_GATE_RAN" in out, f"(out={out[-200:]!r})")
        check("没有退回状态根的默认 build", "frontend" not in out, f"(out={out[-200:]!r})")
        check("门禁整体通过", ok is True, f"(ok={ok})")

        # trunk 缺配置时退回状态根 CFG：不抛异常，结果里能看到状态根的门禁名。
        bare = root / "trunk-bare"
        (bare / "ops").mkdir(parents=True)
        git(bare, "init", "-q", "-b", "main")
        git(bare, "config", "user.email", "t@example.com")
        git(bare, "config", "user.name", "t")
        (bare / "x.txt").write_text("x\n", encoding="utf-8")
        git(bare, "add", "-A")
        git(bare, "commit", "-q", "-m", "init")
        kg.CFG = {"build": {"cmd": "echo FALLBACK_CFG", "cwd": "."}, "verify": None}
        ok2, results2, out2 = kg._run_verify_gates(bare)
        check("trunk 缺配置时退回状态根 CFG（不抛异常）",
              [r.get("name") for r in results2] == ["experience-contract", "build"],
              f"(results={results2})")
        check("退回后的门禁真的执行了", "FALLBACK_CFG" in out2, f"(out={out2[-200:]!r})")

    if failures:
        print(f"\nverify gates use target tree unit: FAIL ({len(failures)})")
        return 1
    print("\nverify gates use target tree unit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
